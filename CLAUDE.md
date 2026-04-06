# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Python script (`tracker.py`) that monitors Iran/Trump war-risk headlines from RSS feeds, fetches Brent crude prices from Yahoo Finance, and emails an HTML digest via `sendmail`. Intended to run every 15 minutes via cron:

```
*/15 * * * * /usr/bin/python3 ~/iran_tracker/tracker.py >> ~/iran_tracker/tracker.log 2>&1
```

## Running

```bash
python3 tracker.py          # run once (sends email, updates state)
tail -f tracker.log         # watch cron output
```

No dependencies beyond the Python standard library (`json`, `re`, `subprocess`, `urllib`, `xml.etree.ElementTree`).

Python version is pinned to `iran_tracker` (see `.python-version` — managed by pyenv).

## Key configuration (top of tracker.py)

| Variable | Purpose |
|---|---|
| `EMAIL_FROM` / `EMAIL_TO` | Sender/recipient for sendmail |
| `MAILER_PATH` | Path to sendmail binary |
| `MAX_ITEMS_PER_FEED` | Max RSS items pulled per source |
| `DEADLINE_UTC` | Trump deadline datetime for the countdown timer |
| `NEWS_FEEDS` | List of RSS feed dicts (`name`, `url`, optional `url2` fallback) |
| `FILTER_KEYWORDS` | Headlines must match at least one to appear in digest |
| `URGENT_KEYWORDS` | Match triggers URGENT subject line and red header |

## State

`.iran_tracker_state.json` persists across runs:
- `seen_ids` — GUIDs of already-emailed items (capped at 200); prevents re-alerting on old news
- `last_crude` — last Brent price float

## Architecture / data flow

```
main()
  ├─ load_state()              # read seen_ids from JSON
  ├─ fetch_brent()             # Yahoo Finance v8 API → price/change/color
  ├─ fetch_all_news(seen_ids)  # RSS feeds → keyword filter → new vs seen split
  │    └─ fetch_feed(url)      # urllib GET → XML parse
  ├─ is_urgent(new_items)      # check URGENT_KEYWORDS → bool + matched kw list
  ├─ tag_items(items)          # calls classify() — see known bug below
  ├─ build_email(...)          # inline HTML string → subject + html body
  ├─ send_email(subject, html) # subprocess sendmail
  └─ save_state(state)         # update seen_ids + last_crude
```

## Known bug

`tag_items()` calls `classify(item)` (line 171) but `classify` is never defined. This causes a `NameError` at runtime. `tag_items` itself is also never called from `main()` (the call was likely removed but the function wasn't), so the bug is currently dormant — items render without tags. If you add tagging back, implement `classify(item) -> list[str]` returning a subset of `["attack", "iran_reject", "bear"]` based on keyword matching against `item["title"]` and `item["desc"]`.

## Email rendering

`build_email` produces a dark-themed HTML email (max-width 620px). Signal categories and their colors:

| Tag | Border | Label |
|---|---|---|
| `attack` | `#c0392b` | ATTACK SIGNALS |
| `iran_reject` | `#d35400` | IRAN REJECTION SIGNALS |
| `bear` | `#d4ac0d` | BEAR / MARKET STRUCTURE |
| `neutral` | `#444` | GENERAL IRAN NEWS |

Items are sorted by signal priority (`attack` → `iran_reject` → `bear` → `neutral`), then new-first within each group. New items get a red "NEW" badge.
