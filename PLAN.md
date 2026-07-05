# PriceMonitorApp — Build Plan

A personal price-monitoring app. Enter a product URL; the app checks the price
on a schedule and keeps history so you can see price trends until you remove the
item. Phase 1 is scoped to **one user, no auth, unique-URL items, in-app
dashboard only**.

## Decisions (locked for phase 1)

| Area | Decision |
|------|----------|
| Notifications | In-app dashboard only (no email/push infra) |
| Target sites | General mix / unknown → tiered extractor |
| Price detection | Fully automatic; cheap OpenRouter LLM as fallback brain |
| Stack | Lightweight single service |
| Bot-protected sites | Stay light + **flag as "blocked"** on the dashboard (don't fight aggressive protection). Headless-browser fallback deferred to phase 2. Confirmed live: Amazon.ca ✅, pastimesports.ca ✅, Arc'teryx ❌ (429 anti-bot on product pages). |
| Deploy target | pi5-ai2 (Raspberry Pi 5), public via noBGP |
| OpenRouter key | **Dedicated key** for this app (separate from the Sharks app) so spend is tracked independently |

## Architecture

One Python service does API + scheduler + UI, backed by a single SQLite file.
One container, one volume.

```
FastAPI app (one container)
  - Jinja2 + Chart.js   -> dashboard UI
  - REST endpoints      -> add / list / remove items
  - APScheduler         -> periodic "due" sweep
  - Extraction engine   -> heuristics -> LLM fallback
        -> SQLite (data/prices.db, on a volume)
  outbound: httpx to target sites; OpenRouter (fallback only)
```

**Stack:** Python 3.12 · FastAPI + Uvicorn · SQLite via SQLModel · APScheduler ·
`httpx` · `extruct` + `selectolax` · `price-parser` · OpenRouter (fallback) ·
Jinja2 + Chart.js · Docker.

**Deferred to phase 2:** Playwright/headless browser (heavy on a Pi; many "JS"
sites still ship the price in the initial HTML).

## Data model

- **item** — `id, url, title, image_url, currency, target_price?,
  interval_minutes (default 1440), extraction_method, extraction_hint, active,
  created_at, last_checked_at, next_check_at, last_status`
- **price_point** — `id, item_id, price, currency, checked_at, method_used,
  http_status, ok, raw_value`

`extraction_method` + `extraction_hint` remember *how* a price was found so
future checks are deterministic and free — the LLM never runs on the happy path.

## Extraction engine (the core)

Cascade, stopping at the first confident hit:

0. **Site-specific handlers** — for sites whose prices aren't in the HTML at
   all (e.g. Agoda hotel pages: fully JS-rendered, price comes from the site's
   own JSON API). The Agoda handler tracks the **lowest room price** for the
   stay encoded in the URL.
1. **JSON-LD** `Product`/`Offer` price (via `extruct`)
2. **Microdata** schema.org Product/Offer
3. **Meta / Open Graph** — `product:price:amount`, `og:price:amount`, `itemprop=price`
4. **Embedded JSON** — `__NEXT_DATA__` / inline state (conservative)
5. **Regex heuristics** — `price-parser` on price-flagged elements
6. **LLM fallback (only if 1–5 miss)** — reduced page text → OpenRouter → strict
   `{"price", "currency"}` JSON, then cache the result

**Cost control:** LLM fires only on a heuristic miss → method cached per item →
input truncated → free/cheap model → monthly call cap that fails safe (flag item
"needs attention" instead of spending). Steady state: most items cost $0.

**Trust guardrail:** low-confidence or wildly-off-vs-history prices are flagged
on the dashboard rather than silently recorded.

## Scheduler

APScheduler runs one **sweep** every few minutes: find items where
`next_check_at <= now`, check them, record a price point, set the next check.
Sweep-based (vs. one job per item) survives restarts cleanly.

## Dashboard UI

- **List:** thumbnail, title, current price, sparkline, last-checked, status
- **Add:** paste URL → preview detected price → confirm → save (+ optional target/interval)
- **Detail:** full history chart + "check now"
- **Remove:** deletes item and history (retention = keep until removed)

## Milestones

| # | Deliverable | Status |
|---|-------------|--------|
| M0 | Repo scaffold: structure, Dockerfile, compose, gitignore, README, config | ✅ |
| M1 | Extraction engine (heuristics + gated LLM) + CLI to test URLs | ✅ |
| M2 | SQLite persistence + add/list/remove API | ✅ |
| M3 | APScheduler sweep + price-history recording | ✅ |
| M4 | Dashboard UI (list, add-with-preview, history chart, remove) | ✅ |
| M5 | LLM fallback hardening + cost caps + usage tracking | ✅ |
| M6 | Dockerize + deploy to pi5-ai2 via noBGP + smoke test | ✅ |
| M7 | Hotel pricing: Agoda site handler (lowest room via property API) + paid LLM default | ✅ |
| Later | Playwright fallback · variant (size/color) verification · email alerts | ☐ |

## Risks / caveats

- **Big retailers (Amazon/Walmart/Best Buy) actively block bots** — may need the
  phase-2 Playwright path or stay flaky.
- **Site ToS** — scraping some sites violates terms; low-risk for a personal,
  low-frequency, single-user tool, but noted.
- **Currency/locale** parsing edge cases — handled by `price-parser`, flagged
  when ambiguous.
