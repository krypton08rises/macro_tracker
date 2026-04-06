#!/usr/bin/env python3
"""
Generic Macro Market Tracker
All configuration lives in config.json — swap themes by pointing at a different file.

Usage:
    python3 tracker.py                        # uses config.json in same dir
    python3 tracker.py --config fed.json      # different theme

Cron (per theme):
    */15 * * * * TRACKER_EMAIL=you@gmail.com python3 ~/tracker/tracker.py >> ~/tracker/tracker.log 2>&1
"""

import argparse
import csv
import gzip
import io
import json
import os
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default=str(Path(__file__).parent / "config.json"))
    return p.parse_args()


def load_config(path):
    return json.loads(Path(path).read_text())


def state_file_for(config_path):
    p = Path(config_path)
    return p.parent / f".{p.stem}_state.json"


def load_state(path):
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"seen_ids": []}


def save_state(state, path):
    state["seen_ids"] = state["seen_ids"][-500:]
    Path(path).write_text(json.dumps(state, default=str))


# ── Utilities ─────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


def ist_now():
    ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    return ist.strftime("%d %b %Y, %I:%M %p IST")


def countdown(deadline_cfg):
    if not deadline_cfg:
        return ""
    utc   = datetime.fromisoformat(deadline_cfg["utc"])
    delta = utc - datetime.now(timezone.utc)
    if delta.total_seconds() <= 0:
        return f"⚠️ {deadline_cfg['label']} — PASSED"
    h = int(delta.total_seconds()) // 3600
    m = (int(delta.total_seconds()) % 3600) // 60
    icon = "🚨" if h < deadline_cfg.get("warning_hours", 6) else "⏰"
    return f"{icon} {h}h {m}m until {deadline_cfg['label']}"


# ── News ──────────────────────────────────────────────────────────────────────

def fetch_feed(url):
    req = urllib.request.Request(
        url, headers={**HEADERS, "Accept-Encoding": "gzip, deflate"}
    )
    with urllib.request.urlopen(req, timeout=12) as r:
        data = r.read()
        if r.info().get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return ET.fromstring(data)


def fetch_all_news(feeds, filter_keywords, max_per_feed, seen_ids):
    all_items, new_items = [], []

    for feed in feeds:
        if not feed.get("enabled", True):
            continue
        urls = [feed["url"]] + ([feed["url2"]] if "url2" in feed else [])
        root = None
        for url in urls:
            try:
                root = fetch_feed(url)
                break
            except Exception as e:
                print(f"[{feed['name']}] {url.split('/')[2]} — {e}")

        if root is None:
            continue

        channel = root.find("channel")
        channel = channel if channel is not None else root
        count   = 0
        for item in channel.findall("item"):
            if count >= max_per_feed:
                break
            title = item.findtext("title", "").strip()
            link  = item.findtext("link",  "").strip()
            pub   = item.findtext("pubDate", "").strip()
            guid  = item.findtext("guid", link).strip()
            desc  = re.sub(r"<[^>]+>", "", item.findtext("description", "")).strip()

            if not any(kw in (title + " " + desc).lower() for kw in filter_keywords):
                continue

            entry = {
                "id":     guid,
                "title":  title,
                "desc":   desc[:200] + ("…" if len(desc) > 200 else ""),
                "link":   link,
                "pub":    pub,
                "source": feed["name"],
                "is_new": guid not in seen_ids,
            }
            all_items.append(entry)
            if entry["is_new"]:
                new_items.append(entry)
            count += 1

    all_items.sort(key=lambda x: (not x["is_new"], x["source"]))
    return all_items, new_items


def classify(item, signals):
    text = (item["title"] + " " + item["desc"]).lower()
    tags = [s["id"] for s in signals if any(kw in text for kw in s["keywords"])]
    return tags if tags else ["neutral"]


def tag_items(items, signals):
    for item in items:
        item["tags"] = classify(item, signals)
    return items


def is_urgent(items, urgent_keywords):
    hits = set()
    for item in items:
        text = (item["title"] + " " + item["desc"]).lower()
        hits |= {kw for kw in urgent_keywords if kw in text}
    return bool(hits), sorted(hits)


def is_market_open():
    """NSE market hours: Mon–Fri, 09:15–15:30 IST."""
    from datetime import time as dtime
    now_ist = datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)
    if now_ist.weekday() >= 5:  # Saturday, Sunday
        return False
    t = now_ist.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def should_send(new_items, state):
    """Return (bool, reason). Send on new items or hourly during market hours."""
    if new_items:
        return True, "news"
    if is_market_open():
        last = state.get("last_hourly_send")
        if not last:
            return True, "hourly"
        elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if elapsed >= 3600:
            return True, "hourly"
    return False, "skip"


# ── Markets ───────────────────────────────────────────────────────────────────

def _stooq_fetch(ticker):
    url = f"https://stooq.com/q/l/?s={ticker}&f=sd2t2ohlcv&h&e=csv"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        rows = list(csv.DictReader(io.TextIOWrapper(r, encoding="utf-8")))
    if not rows or rows[0].get("Close", "N/D") == "N/D":
        return None
    return float(rows[0]["Close"]), float(rows[0]["Open"])


def _yahoo_fetch(ticker):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1m&range=1d"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        meta = json.loads(r.read())["chart"]["result"][0]["meta"]
    return float(meta["regularMarketPrice"]), float(meta.get("chartPreviousClose", meta["regularMarketPrice"]))


def _make_market_entry(close, prev, mcfg):
    chg   = close - prev
    pct   = (chg / prev * 100) if prev else 0
    arrow = "▲" if chg >= 0 else "▼"
    price = mcfg.get("prefix", "") + f"{close:,.{mcfg.get('decimals', 2)}f}"
    return {
        "id":      mcfg["id"],
        "label":   mcfg["label"],
        "price":   price,
        "change":  f"{arrow} {abs(chg):.2f} ({abs(pct):.2f}%)",
        "color":   "#e74c3c" if chg >= 0 else "#2ecc71",
        "note":    mcfg.get("note_up", "") if chg >= 0 else mcfg.get("note_down", ""),
        "raw":     close,
        "primary": mcfg.get("primary", False),
    }


def fetch_all_markets(markets_cfg, state):
    results  = {}
    now_utc  = datetime.now(timezone.utc)

    for mcfg in markets_cfg:
        mid        = mcfg["id"]
        cache_mins = mcfg.get("cache_minutes", 30)
        t_key, d_key = f"mkt_{mid}_time", f"mkt_{mid}_data"

        last_t = state.get(t_key)
        if last_t:
            age = (now_utc - datetime.fromisoformat(last_t)).total_seconds() / 60
            if age < cache_mins and state.get(d_key):
                print(f"[{mid}] cached ({age:.0f}m old)")
                results[mid] = state[d_key]
                continue

        entry = None

        # 1. Stooq
        try:
            pair = _stooq_fetch(mcfg["ticker"])
            if pair:
                entry = _make_market_entry(*pair, mcfg)
                print(f"[{mid}] Stooq {entry['price']}")
        except Exception as e:
            print(f"[{mid}] Stooq failed: {e}")

        # 2. Yahoo fallback (optional per-market yahoo_ticker in config)
        if entry is None and mcfg.get("yahoo_ticker"):
            try:
                pair  = _yahoo_fetch(mcfg["yahoo_ticker"])
                entry = _make_market_entry(*pair, mcfg)
                print(f"[{mid}] Yahoo {entry['price']}")
            except Exception as e:
                print(f"[{mid}] Yahoo failed: {e}")

        # 3. Stale cache
        if entry is None and state.get(d_key):
            print(f"[{mid}] stale cache")
            entry = {**state[d_key], "note": "(cached) " + state[d_key].get("note", "")}

        if entry:
            state[t_key] = now_utc.isoformat()
            state[d_key] = entry
            results[mid] = entry
        else:
            results[mid] = {
                "id": mid, "label": mcfg["label"],
                "price": "N/A", "change": "unavailable",
                "color": "#999", "note": "", "raw": 0,
                "primary": mcfg.get("primary", False),
            }

    return results


# ── Email ─────────────────────────────────────────────────────────────────────

def build_email(cfg, items, markets, new_items, urgent, urgent_kw):
    ts    = ist_now()
    cd    = countdown(cfg.get("deadline"))
    theme = cfg["theme"]

    primary     = next((m for m in markets.values() if m.get("primary")), None)
    secondaries = [m for m in markets.values() if not m.get("primary")]
    signals     = cfg.get("signals", [])
    sig_meta    = {s["id"]: s for s in signals}
    sig_order   = [s["id"] for s in signals]  # neutral falls through

    # ── Subject ───────────────────────────────────────────────────────────────
    px = primary["price"]  if primary else ""
    ch = primary["change"] if primary else ""
    if new_items and urgent:
        subject = f"URGENT: {', '.join(urgent_kw[:2]).upper()} | {px} {ch} | {ts}"
    elif new_items:
        subject = f"{len(new_items)} new headlines | {px} {ch} | {ts}"
    else:
        subject = f"{theme} | {px} {ch} | {ts}"

    # ── News rows ─────────────────────────────────────────────────────────────
    def _primary_sig(tags):
        for sid in sig_order:
            if sid in tags:
                return sid
        return "neutral"

    def _sort_key(x):
        p = _primary_sig(x.get("tags", []))
        idx = sig_order.index(p) if p in sig_order else len(sig_order)
        return (idx, not x["is_new"])

    rows = ""
    if not items:
        rows = '<p style="color:#777;font-size:13px">No matching headlines this cycle.</p>'
    else:
        last_sig = None
        for item in sorted(items, key=_sort_key):
            tags = item.get("tags", ["neutral"])
            pri  = _primary_sig(tags)

            if pri != last_sig:
                last_sig = pri
                if pri == "neutral":
                    lbl, hcol, bcol = "GENERAL NEWS", "#888", "#444"
                else:
                    s = sig_meta[pri]
                    lbl, hcol, bcol = s["label"], s["header_color"], s["border_color"]
                rows += (f'<div style="font-size:9px;letter-spacing:1.5px;color:{hcol};'
                         f'margin:14px 0 4px;font-weight:600">{lbl}</div>')

            bcol     = sig_meta.get(pri, {}).get("border_color", "#444")
            tag_html = "".join(
                f'<span style="font-size:9px;padding:1px 5px;border-radius:3px;margin-right:3px;'
                f'background:{sig_meta[t]["tag_bg"]};color:{sig_meta[t]["tag_fg"]}">'
                f'{sig_meta[t]["tag_label"]}</span>'
                for t in tags if t in sig_meta
            )
            new_b = (' <span style="background:#e74c3c;color:#fff;font-size:9px;'
                     'padding:1px 4px;border-radius:3px">NEW</span>') if item["is_new"] else ""
            lnk   = (f'<a href="{item["link"]}" style="color:#5b9bd5;font-size:11px">Read</a>'
                     if item["link"] else "")
            src   = item["source"] + " / " + item["pub"][:28]

            rows += (
                f'<div style="margin:5px 0;padding:9px 13px;background:#1e1e1e;'
                f'border-radius:4px;border-left:4px solid {bcol}">'
                f'<div style="margin-bottom:4px">{tag_html}{new_b}</div>'
                f'<div style="font-size:13px;color:#e8e8e8;font-weight:600;line-height:1.4">{item["title"]}</div>'
                f'<div style="font-size:11px;color:#777;margin-top:2px">{src}</div>'
                f'<div style="font-size:12px;color:#aaa;margin-top:3px;line-height:1.4">{item["desc"]}</div>'
                f'<div style="margin-top:4px">{lnk}</div></div>'
            )

    # ── Blocks ────────────────────────────────────────────────────────────────
    top_color = (primary["color"] if primary and urgent else "#e67e22")

    cd_html = (
        f'<div style="background:#2a1515;border:1px solid #5c2020;border-radius:5px;'
        f'padding:10px 16px;margin-bottom:12px;text-align:center;font-size:13px;color:#f4a0a0">'
        f'{cd}</div>'
    ) if cd else ""

    primary_html = (
        f'<div style="background:#1a1a1a;border-radius:5px;padding:13px 18px;margin-bottom:12px">'
        f'<div style="font-size:10px;color:#666;margin-bottom:5px">{primary["label"].upper()}</div>'
        f'<span style="font-size:26px;font-weight:700;color:{primary["color"]}">{primary["price"]}</span>'
        f'<span style="font-size:13px;color:{primary["color"]};margin-left:10px">{primary["change"]}</span>'
        f'<div style="font-size:11px;color:#666;margin-top:3px">{primary["note"]}</div>'
        f'</div>'
    ) if primary else ""

    secondary_html = ""
    if secondaries:
        sec_rows = "".join(
            f'<tr><td style="padding:4px 0;color:#888">{m["label"]}</td>'
            f'<td style="text-align:right;color:{m["color"]}">{m["price"]} &nbsp;{m["change"]}</td></tr>'
            for m in secondaries
        )
        secondary_html = (
            f'<div style="background:#1a1a1a;border-radius:5px;padding:13px 18px;margin-bottom:12px">'
            f'<div style="font-size:10px;color:#666;margin-bottom:6px">GLOBAL MARKETS</div>'
            f'<table style="width:100%;font-size:12px;border-collapse:collapse">{sec_rows}</table>'
            f'</div>'
        )

    key_levels_html = ""
    if cfg.get("key_levels"):
        kl_rows = "".join(
            f'<tr><td style="padding:4px 0;color:#888">{kl["label"]}</td>'
            f'<td style="text-align:right;color:{kl.get("color","#ddd")}">{kl["value"]}</td></tr>'
            for kl in cfg["key_levels"]
        )
        key_levels_html = (
            f'<div style="background:#1a1a1a;border-radius:5px;padding:13px 18px;margin-bottom:12px">'
            f'<div style="font-size:10px;color:#666;margin-bottom:6px">KEY LEVELS</div>'
            f'<table style="width:100%;font-size:12px;border-collapse:collapse">{kl_rows}</table>'
            f'</div>'
        )

    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#111;font-family:Arial,sans-serif">
<div style="max-width:620px;margin:0 auto;padding:20px 14px">

<div style="background:#1a1a1a;border-radius:7px;padding:14px 18px;margin-bottom:12px;border-top:3px solid {top_color}">
  <div style="font-size:10px;color:#666;letter-spacing:1px">{theme.upper()}</div>
  <div style="font-size:12px;color:#888;margin-top:2px">{ts}</div>
</div>

{cd_html}
{primary_html}
{key_levels_html}
{secondary_html}

<div style="background:#1a1a1a;border-radius:5px;padding:13px 18px;margin-bottom:12px">
  <div style="font-size:10px;color:#666;margin-bottom:4px">HEADLINES (keyword-filtered)</div>
  {rows}
</div>

</div></body></html>"""

    return subject, html


def send_email(cfg, subject, html):
    email  = os.environ.get("TRACKER_EMAIL", "kushagra.raina@gmail.com")
    mailer = cfg.get("mailer_path", "/usr/sbin/sendmail")
    nl     = "\n"
    msg    = (
        f"From: {email}{nl}To: {email}{nl}Subject: {subject}{nl}"
        f"MIME-Version: 1.0{nl}Content-Type: text/html; charset=utf-8{nl}{nl}{html}"
    )
    r = subprocess.run([mailer, "-oi", email], input=msg, text=True, capture_output=True)
    if r.returncode != 0:
        print(f"!! sendmail error ({r.returncode}): {r.stderr.strip()}")
    else:
        print(f"[{ist_now()}] Sent: {subject[:80]}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    args       = parse_args()
    cfg        = load_config(args.config)
    sf         = state_file_for(args.config)
    state      = load_state(sf)
    seen_ids   = set(state.get("seen_ids", []))

    markets              = fetch_all_markets(cfg["markets"], state)
    items, new_items     = fetch_all_news(
        cfg["feeds"], cfg["filter_keywords"],
        cfg.get("max_items_per_feed", 5), seen_ids,
    )
    items                = tag_items(items, cfg.get("signals", []))
    urgent, urgent_kw    = is_urgent(new_items, cfg.get("urgent_keywords", []))

    send, reason = should_send(new_items, state)
    if send:
        subject, html = build_email(cfg, items, markets, new_items, urgent, urgent_kw)
        send_email(cfg, subject, html)
        if reason == "hourly":
            state["last_hourly_send"] = datetime.now(timezone.utc).isoformat()
    else:
        print(f"[{ist_now()}] Skip — no new items and market closed / hourly not due")

    state["seen_ids"] = list(seen_ids | {i["id"] for i in items})
    save_state(state, sf)


if __name__ == "__main__":
    main()
