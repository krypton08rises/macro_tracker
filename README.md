# India Macro Tracker

Autonomous macro-event monitor that watches global news 24/7, filters for anything that could move Indian equity markets (Nifty/Sensex), and emails a dark-themed HTML digest via `sendmail`. Runs on a 15-minute cron. During NSE market hours (Mon–Fri, 09:15–15:30 IST) it sends an hourly digest regardless; outside market hours it only sends when new relevant headlines appear.

## Features

- **Global RSS coverage** — Indian financial press (ET, Moneycontrol, Mint, Business Standard) plus global macro (Reuters, CNBC, Bloomberg, WSJ, FT, Yahoo Finance, MarketWatch)
- **Keyword filter** — headlines must match at least one filter keyword (Fed, RBI, crude, war, tariff, GDP, rupee, Nifty …) to appear
- **Signal tagging** — each headline is tagged Risk-Off / Risk-On / Commodity / India Macro with colour-coded borders
- **Market dashboard** — Nifty 50 (primary), Sensex, USD/INR, Brent Crude, Gold, DXY, S&P 500 pulled from Stooq (Yahoo Finance fallback)
- **Smart send logic**
  - New headlines detected → email immediately (any time of day)
  - No new headlines + NSE market open → hourly digest
  - No new headlines + market closed → silent, no email
- **Urgent alerts** — subject line becomes `URGENT: …` and header turns red on crash/war/rate-shock keywords
- **Dedup** — seen GUIDs stored in `.config_state.json`; capped at 500

## Setup

No pip dependencies — pure Python standard library (`json`, `re`, `subprocess`, `urllib`, `xml.etree.ElementTree`).

```bash
git clone <repo>
cd macro_tracker
cp .env.example .env          # set TRACKER_EMAIL=you@gmail.com
```

Ensure `sendmail` (or a compatible MTA such as `msmtp`) is configured and reachable at the path in `config.json → mailer_path`.

## Running

```bash
# Single run (respects send logic — may print "Skip" if nothing new)
python3 tracker.py

# Force a run against a different config
python3 tracker.py --config fed.json

# Watch cron output
tail -f tracker.log
```

## Cron

```cron
*/15 * * * * TRACKER_EMAIL=you@gmail.com /usr/bin/python3 /path/to/macro_tracker/tracker.py >> /path/to/macro_tracker/tracker.log 2>&1
```

The 15-minute poll is intentional: it detects breaking news quickly while the hourly-digest logic inside the script prevents inbox spam during quiet market sessions.

## Configuration (`config.json`)

| Field | Purpose |
|---|---|
| `theme` | Display name shown in email header |
| `mailer_path` | Path to `sendmail` binary |
| `max_items_per_feed` | Max headlines pulled per RSS source |
| `feeds` | List of `{name, url}` RSS sources; add `enabled: false` to disable one |
| `filter_keywords` | Headlines must match at least one keyword (case-insensitive substring) |
| `urgent_keywords` | Match triggers URGENT subject and red header |
| `signals` | Signal categories with colour config and matching keywords |
| `markets` | Market instruments — Stooq ticker, optional Yahoo fallback, cache TTL |
| `key_levels` | Static reference table shown in every email |
| `deadline` | Optional countdown timer (remove key to hide) |

### Signal categories

| Tag | What it means |
|---|---|
| `risk_off` | Bearish for India — war, rate hike, crash, sanctions |
| `risk_on` | Bullish for India — rate cut, stimulus, peace deal, rally |
| `commodity` | Crude / gold / metals — direct impact on India's CAD and inflation |
| `macro_india` | India-specific macro — RBI, Nifty, Sensex, rupee, SEBI |

### Market instruments

| ID | Instrument | Primary |
|---|---|---|
| `nifty` | Nifty 50 | Yes — shown large |
| `sensex` | BSE Sensex | — |
| `usdinr` | USD / INR | — |
| `brent` | Brent Crude | — |
| `gold` | Gold (XAU/USD) | — |
| `dxy` | Dollar Index (DXY) | — |
| `spx` | S&P 500 | — |

## State file

`.config_state.json` persists across runs:

| Key | Purpose |
|---|---|
| `seen_ids` | GUIDs of already-emailed items (capped at 500) |
| `last_hourly_send` | UTC ISO timestamp of last hourly digest |
| `mkt_<id>_data` / `mkt_<id>_time` | Per-instrument price cache |

Delete the state file to reset everything (next run re-fetches all prices and re-alerts all current headlines).

## Adding a new feed

```json
{ "name": "My Source", "url": "https://example.com/rss" }
```

Add to `feeds` in `config.json`. Optionally add `"url2"` as a fallback URL and `"enabled": false` to disable without deleting.

## Key levels

Edit `key_levels` in `config.json` weekly to keep Nifty support/resistance current. These are static display labels — the tracker does not compute them automatically.
