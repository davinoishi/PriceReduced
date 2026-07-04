<img src="app/static/logo.png" width="84" align="left" style="margin-right:16px">

# PriceReduced

Personal price monitor. Add a product URL, and the app checks its price on a
schedule and keeps history so you can watch price trends until you remove the
item. Phase 1 is single-user, with an in-app dashboard and optional HTTP basic
auth for public exposure.

<br clear="left">

See [PLAN.md](PLAN.md) for the full design and milestones.

## Features

- **Automatic price extraction** from arbitrary product URLs — a tiered cascade
  (JSON-LD → microdata → Open Graph/meta → embedded JSON → price-flagged
  elements), with a cheap **LLM fallback** only when heuristics miss.
- **Scheduled checks** (per-item interval, default daily) via an in-process
  background sweep — no external scheduler.
- **Price history** retained until you remove the item, with a chart per item
  and a trend sparkline in the list.
- **Honest statuses** — sites that actively block bots are flagged
  `Site blocks bots` rather than silently failing.
- **LLM cost control** — the fallback is gated, token-truncated, and capped per
  month; usage is tracked and shown in the dashboard.
- **Dashboard** (server-rendered) + a **JSON API**.

## Tech stack

Python 3.12 · FastAPI · SQLite (SQLModel) · APScheduler · httpx · extruct +
selectolax + price-parser · Jinja2 + Chart.js · OpenRouter (LLM fallback) ·
Docker. Single service, single container, one SQLite file.

## Quick start (local)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env          # optionally add an OpenRouter key for the LLM fallback

uvicorn app.main:app --reload --port 8010
```

Open http://localhost:8010. On startup it creates the SQLite DB
(`data/prices.db`) and starts the background sweep. With no `BASIC_AUTH_*` set,
the dashboard is open (fine for local dev).

### Try the extractor without the app

```bash
python -m app.cli "https://example.com/some-product"
python -m app.cli --json "https://a.com/x" "https://b.com/y"
```

Prints the detected price, currency, method (json-ld, microdata, meta,
embedded-json, regex, or llm), and a confidence score — without touching the DB.

### REST API (also drives the dashboard)

| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health` | Health check (always open) |
| GET  | `/api/items` | List tracked items |
| POST | `/api/items` | Add `{url, target_price?, interval_minutes?}` (checks immediately) |
| GET  | `/api/items/{id}` | Get one item |
| GET  | `/api/items/{id}/history` | Price history (`?ok_only=true` for successful checks) |
| POST | `/api/items/{id}/check` | Check now |
| DELETE | `/api/items/{id}` | Remove item + its history |
| GET  | `/api/llm-usage` | LLM calls/tokens this month vs. the cap |

## Configuration

All via environment / `.env` (see [.env.example](.env.example)):

| Var | Purpose |
|-----|---------|
| `DATABASE_URL` | SQLite path (default `sqlite:///./data/prices.db`) |
| `USER_AGENT`, `REQUEST_TIMEOUT_SECONDS` | HTTP fetching |
| `OPENROUTER_API_KEY` | Enables the LLM fallback (blank = heuristics only) |
| `OPENROUTER_MODEL` | Fallback model (default a free Gemma) |
| `LLM_EXTRACTION_ENABLED`, `LLM_MAX_INPUT_CHARS`, `LLM_MONTHLY_CALL_CAP` | LLM gating + cost control |
| `SCHEDULER_INTERVAL_SECONDS`, `DEFAULT_CHECK_INTERVAL_MINUTES` | Scheduling |
| `BASIC_AUTH_USER`, `BASIC_AUTH_PASS` | HTTP basic auth (enforced only when **both** set) |

> **Never commit `.env`.** It's gitignored. Keep the OpenRouter key and the
> basic-auth password out of the repo.

## Deployment (Docker)

```bash
docker compose up -d --build      # build + (re)start
docker compose logs -f app        # tail logs
docker compose down               # stop
```

The container runs uvicorn on port 8000; compose publishes it on the host and
stores SQLite on the `app_data` volume (survives rebuilds). `restart:
unless-stopped` brings it back after reboots. To change the OpenRouter key or
the basic-auth password, edit `.env` and run `docker compose up -d` to recreate.

## Exposing a local server as a production URL with noBGP

[noBGP](https://nobgp.com) gives a service running on a machine behind
NAT/CGNAT/a firewall a **public HTTPS URL** — no port-forwarding, static IP, or
reverse proxy required. This is how the app is served from a home Raspberry Pi.

### 1. Prerequisites

- The **noBGP agent** installed on the host and the node joined to one of your
  networks (install via the noBGP dashboard or `nobgp` CLI; see noBGP's docs).
- Your service **listening locally** on the host — for this app,
  `docker compose up -d` puts it on `127.0.0.1:8000`.

### 2. Publish the local port

Create a **proxy** service that maps a public URL to your local port. Either:

- **Dashboard:** node → *Services* → *Add service* → *Proxy*, with target
  `http://127.0.0.1:8000`. noBGP returns a URL like
  `https://<random-id>.nobgp.com`.
- **Programmatically** (e.g. the noBGP MCP `service_publish`): publish
  `proxy_target_url = http://127.0.0.1:8000` on the node.

### 3. Decide how access is protected

You have two independent auth layers — use **one**:

| Option | How | When |
|--------|-----|------|
| **App basic auth** (used here) | Publish the proxy with its own auth **disabled** (pass-through), and set `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` in the app's `.env`. The public URL then prompts for the app's username/password. | You want to reach it from any browser with a shared password. |
| **noBGP auth** | Publish with noBGP auth **enabled** and an authorized-email allowlist; leave the app's basic auth unset. | You want identity-based access tied to your noBGP account. |

`/health` is intentionally left open so the proxy can health-check without
credentials.

### 4. Verify

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://<your-id>.nobgp.com/health   # 200
curl -s -o /dev/null -w '%{http_code}\n' https://<your-id>.nobgp.com/         # 401 (no creds)
curl -s -o /dev/null -w '%{http_code}\n' -u user:pass https://<your-id>.nobgp.com/  # 200
```

### Notes

- The public URL persists across restarts; the **daily price checks keep running
  on the host** whether or not anyone is viewing the dashboard.
- With basic auth, treat the URL as semi-private (there's no lockout on repeated
  guesses beyond the app itself).
- Because outbound fetches originate from the **host's** network, some retailers
  that block datacenter IPs may work from a home connection that would fail from
  a cloud server (and vice-versa).

## Tests

```bash
pytest
```

## Roadmap (phase 2+)

- Headless-browser (Playwright) fallback for bot-protected sites.
- Variant verification (confirm size/color still matches before trusting price).
- Email alerts on price drops / target hits.
