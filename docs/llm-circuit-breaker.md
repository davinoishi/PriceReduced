# Proposal: per-item circuit breaker on the LLM fallback

Status: **proposed, not implemented.** Deliberately kept out of the
whitespace-guard fix — that one closes a correctness hole, this one changes
spend policy and needs a decision from the operator first.

## The problem

The only brake on LLM spend today is `LLM_MONTHLY_CALL_CAP` (500), enforced in
`services._check_item` via `llm_cap_reached`. It is a *global* cap, and it has
never been reached — peak month was 115 calls.

That cap does not model the failure it needs to catch. Observed over eight
weeks: **137 of 225 model calls (61% of all spend) went to three URLs that
have never once returned a price.** They are on a daily schedule, so each
contributes ~30 calls a month, forever, at a 0% success rate. The cap only
notices the total; nothing notices that a specific URL is a known dead end.

A cap answers "have we spent too much?" The question that matters here is
"is this particular URL worth asking about again?"

## Why this is cheap to build

No schema change is needed. `LlmCall` (`app/models.py`) already records both
things the breaker needs:

- `item_id` — foreign key to the item, already set by `record_llm_call`
- `ok` — whether that call yielded a price, already set from `result.found`

So the breaker is a query over existing rows plus one condition alongside the
existing cap check in `_check_item`:

```python
allow_llm = (
    use_llm
    and settings.llm_available
    and not llm_cap_reached(session)
    and not llm_breaker_open(session, item.id)   # new
)
```

`llm_breaker_open` = "the last N calls for this item all have `ok = False`,
and there are at least N of them."

## Decisions the operator needs to make

1. **Threshold N.** N=5 stops the three known-bad URLs after five days and
   would have cut ~120 of the 137 wasted calls. N=3 is tighter but more
   likely to trip on a URL that is merely flaky (a transient block, a
   sold-out window).

2. **Reset policy — the important one.** A breaker that never closes turns
   into a silent permanent opt-out, which is worse than the current waste
   because it is invisible. Options:
   - Reset when a non-LLM tier finds a price (the page started working).
   - Reset when the item's URL is edited.
   - Half-open retry: let one call through every ~30 days.
   Recommendation: all three. The 30-day probe costs 12 calls/year per dead
   URL and keeps the breaker self-healing.

3. **Scope.** The breaker should suppress *only the LLM call*, not the check
   itself. The heuristics are free and the site may start server-rendering
   prices at any time. Suppressing the whole check would also stop recording
   the `unreachable` status the dashboard relies on.

4. **Visibility.** An open breaker must be surfaced — at minimum a counter in
   `llm_usage_summary`, ideally a badge on the item. Spend suppression that
   the operator cannot see is a support burden later ("why did this item stop
   using the LLM?").

## Note on the three URLs

Worth checking whether they *should* be LLM targets at all before tuning
thresholds. A URL with a 0% success rate over eight weeks is usually a page
the generic cascade fundamentally cannot read (fully client-rendered, like the
Agoda pages that motivated the site-handler path), not one the model is merely
unlucky on. A site handler, or deactivating the item, may be the better fix —
the breaker is the safety net for the ones nobody notices.
