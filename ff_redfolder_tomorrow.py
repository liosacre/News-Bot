#!/usr/bin/env python3
"""
ForexFactory (thisweek.json export) -> Discord weekly digest (USD only)
- Correct times in New York (ET)
- Shows the *event date* (weekday + month/day) for each item (no confusing week range)
- Includes @everyone (and enables it via allowed_mentions)
- Shows the red-folder name (event title)
- Uses your FF export link from env: FF_JSON_URL
- Uses your webhook from env: DISCORD_WEBHOOK_URL

REQUIRED GitHub Secrets / env vars:
- DISCORD_WEBHOOK_URL
- FF_JSON_URL

OPTIONAL (if your FF export times are shown in a specific timezone on the site):
- FF_SOURCE_TZ (default: America/Los_Angeles)
"""

from __future__ import annotations

import os
import time
import json
import datetime as dt
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests


ET = ZoneInfo("America/New_York")


def die(msg: str) -> None:
    raise RuntimeError(msg)


def env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        die(f"Missing {name} env var (GitHub Secret) – set it in repo Settings → Secrets → Actions.")
    return v


def safe_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x).strip()


def normalize_impact(raw: Any) -> str:
    """
    Returns: "high" | "medium" | "low" | ""
    Handles common FF export variations.
    """
    s = safe_str(raw).lower()
    if not s:
        return ""

    # common encodings
    if s in {"high", "red", "3", "high impact", "highimpact"}:
        return "high"
    if s in {"medium", "orange", "2", "medium impact", "mediumimpact"}:
        return "medium"
    if s in {"low", "yellow", "1", "low impact", "lowimpact"}:
        return "low"

    # sometimes it's like "High" or "Medium"
    if "high" in s:
        return "high"
    if "medium" in s:
        return "medium"
    if "low" in s:
        return "low"

    return ""


def fetch_json(url: str) -> Any:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    ct = (r.headers.get("content-type") or "").lower()
    # If server returns HTML (cloudflare etc), .json() will explode; show snippet.
    if "json" not in ct:
        snippet = r.text[:200].replace("\n", " ")
        die(f"FF_JSON_URL did not return JSON (content-type={ct}). First chars: {snippet}")

    return r.json()


def parse_epoch(ts: Any) -> Optional[dt.datetime]:
    """
    If FF gives a unix timestamp, treat it as UTC epoch, convert to ET.
    Handles seconds vs milliseconds.
    """
    if ts is None:
        return None

    # Sometimes it's a string
    try:
        n = int(float(ts))
    except Exception:
        return None

    # milliseconds vs seconds heuristic
    if n > 10_000_000_000:  # > ~2286-11-20 in seconds => likely ms
        n = n // 1000

    try:
        dtu = dt.datetime.fromtimestamp(n, tz=dt.timezone.utc)
        return dtu.astimezone(ET)
    except Exception:
        return None


def parse_date_time_fallback(
    date_str: str,
    time_str: str,
    source_tz: ZoneInfo,
) -> Optional[dt.datetime]:
    """
    Fallback if no timestamp.
    Assumes date/time are in the timezone you viewed/exported on ForexFactory
    (default: America/Los_Angeles), then converts to ET.
    """
    ds = safe_str(date_str)
    ts = safe_str(time_str)

    if not ds:
        return None

    # Handle "All Day" / "Tentative"
    if not ts or ts.lower() in {"all day", "tentative"}:
        # Date-only: set noon local so ET date conversion is stable
        try:
            d = dt.datetime.strptime(ds, "%Y-%m-%d").date()
            local_noon = dt.datetime(d.year, d.month, d.day, 12, 0, tzinfo=source_tz)
            return local_noon.astimezone(ET)
        except Exception:
            return None

    # Common FF formats seen in exports:
    # - date: "2026-02-18"
    # - time: "11:00am" or "11:00 am" or "11:00"
    s = f"{ds} {ts}".strip().lower().replace(" ", "")

    fmts = [
        "%Y-%m-%d%I:%M%p",  # 2026-02-1811:00am
        "%Y-%m-%d%I%p",     # 2026-02-1811am
        "%Y-%m-%d%H:%M",    # 2026-02-1811:00
    ]

    for f in fmts:
        try:
            naive = dt.datetime.strptime(s, f)
            local = naive.replace(tzinfo=source_tz)
            return local.astimezone(ET)
        except Exception:
            pass

    return None


def get_event_dt(event: Dict[str, Any], source_tz: ZoneInfo) -> Tuple[Optional[dt.datetime], str]:
    """
    Returns (datetime_in_ET or None, time_label)
    time_label can be "ALL DAY", "TENTATIVE", or "".
    """
    # Try timestamps first (most reliable)
    for k in ("timestamp", "ts", "timeStamp", "datestamp", "dateStamp", "datetimeStamp"):
        d = parse_epoch(event.get(k))
        if d:
            return d, ""

    date_str = safe_str(event.get("date") or event.get("Date"))
    time_str = safe_str(event.get("time") or event.get("Time") or event.get("datetime") or event.get("dateTime"))

    if time_str.lower() in {"all day", "allday"}:
        d = parse_date_time_fallback(date_str, "", source_tz)
        return d, "ALL DAY"

    if time_str.lower() == "tentative":
        d = parse_date_time_fallback(date_str, "", source_tz)
        return d, "TENTATIVE"

    d = parse_date_time_fallback(date_str, time_str, source_tz)
    return d, ""


def pick_title(event: Dict[str, Any]) -> str:
    for k in ("title", "event", "name", "headline", "detail"):
        t = safe_str(event.get(k))
        if t:
            return t
    return "(Unnamed event)"


def pick_currency(event: Dict[str, Any]) -> str:
    for k in ("currency", "ccy", "Currency"):
        c = safe_str(event.get(k))
        if c:
            return c.upper()
    return ""


def chunk_for_discord(text: str, limit: int = 1900) -> List[str]:
    """
    Discord limit is 2000 chars. Keep safety margin.
    Splits on blank lines first, then on lines if needed.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    blocks = text.split("\n\n")
    cur = ""

    for b in blocks:
        if not cur:
            cur = b
        elif len(cur) + 2 + len(b) <= limit:
            cur += "\n\n" + b
        else:
            parts.append(cur)
            cur = b

    if cur:
        parts.append(cur)

    # If any part still too large, split by lines
    final: List[str] = []
    for p in parts:
        if len(p) <= limit:
            final.append(p)
            continue

        lines = p.split("\n")
        cur2 = ""
        for ln in lines:
            if not cur2:
                cur2 = ln
            elif len(cur2) + 1 + len(ln) <= limit:
                cur2 += "\n" + ln
            else:
                final.append(cur2)
                cur2 = ln
        if cur2:
            final.append(cur2)

    return final


def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},  # allows @everyone
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()


def format_day_header(d: dt.date) -> str:
    # Example: WEDNESDAY (Feb 18)
    return f"{d.strftime('%A').upper()} ({d.strftime('%b')} {d.day})"


def format_time_et(d: dt.datetime) -> str:
    # Example: 1:30 PM ET
    return d.strftime("%-I:%M %p") + " ET"


def build_digest(
    events: List[Dict[str, Any]],
    source_tz: ZoneInfo,
    mode: str = "red_only",
) -> str:
    """
    mode:
      - "red_only": USD + High impact only
      - "high_medium": USD + High+Medium
    """
    # Filter USD and impact
    wanted_impacts = {"high"} if mode == "red_only" else {"high", "medium"}

    picked: List[Tuple[dt.datetime, str, str]] = []  # (dt_et, impact, title)
    all_day: List[Tuple[dt.date, str, str]] = []     # (date_et, impact, title)

    for e in events:
        if pick_currency(e) != "USD":
            continue

        impact = normalize_impact(e.get("impact") or e.get("Impact") or e.get("impactTitle") or e.get("impact_title"))
        if impact not in wanted_impacts:
            continue

        dte, label = get_event_dt(e, source_tz)
        title = pick_title(e)

        if not dte:
            continue

        if label in {"ALL DAY", "TENTATIVE"}:
            all_day.append((dte.date(), impact, title + f" ({label})"))
        else:
            picked.append((dte, impact, title))

    # Sort
    picked.sort(key=lambda x: x[0])
    all_day.sort(key=lambda x: x[0])

    # Group by date
    by_day: Dict[dt.date, List[str]] = {}

    def icon(imp: str) -> str:
        return "🔴" if imp == "high" else "🟠"

    for dte, imp, title in picked:
        day = dte.date()
        by_day.setdefault(day, []).append(f"• {format_time_et(dte)} | {title} {icon(imp)}")

    for day, imp, title in all_day:
        by_day.setdefault(day, []).append(f"• ALL DAY ET | {title} {icon(imp)}")

    # If nothing found
    if not by_day:
        impact_label = "High impact (Red Folder)" if mode == "red_only" else "High/Medium impact"
        header = "🔴 USD RED FOLDER THIS WEEK (NEW YORK / ET)" if mode == "red_only" else "🟠 USD IMPORTANT NEWS THIS WEEK (NEW YORK / ET)"
        return "\n".join([
            "@everyone",
            header,
            "",
            f"• No USD {impact_label} events found in thisweek feed.",
        ]).strip()

    # Build message
    header = "🔴 USD RED FOLDER THIS WEEK (NEW YORK / ET)" if mode == "red_only" else "🟠 USD IMPORTANT NEWS THIS WEEK (NEW YORK / ET)"
    lines: List[str] = ["@everyone", header, ""]

    for day in sorted(by_day.keys()):
        lines.append(format_day_header(day))
        for item in by_day[day]:
            lines.append(item)
        lines.append("")  # blank line between days

    return "\n".join(lines).strip()


def main() -> None:
    webhook = env_required("DISCORD_WEBHOOK_URL")
    ff_url = env_required("FF_JSON_URL")

    source_tz_name = os.environ.get("FF_SOURCE_TZ", "America/Los_Angeles").strip() or "America/Los_Angeles"
    try:
        source_tz = ZoneInfo(source_tz_name)
    except Exception:
        die(f"Invalid FF_SOURCE_TZ timezone: {source_tz_name}")

    data = fetch_json(ff_url)

    # The export might be:
    # - a list of events
    # - or {"events": [...]}
    events: List[Dict[str, Any]] = []
    if isinstance(data, list):
        events = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        if isinstance(data.get("events"), list):
            events = [x for x in data["events"] if isinstance(x, dict)]
        elif isinstance(data.get("data"), list):
            events = [x for x in data["data"] if isinstance(x, dict)]
        else:
            # last-resort: scan dict values for list
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    events = [x for x in v if isinstance(x, dict)]
                    break

    if not events:
        die("No events found in FF_JSON_URL JSON structure. (Export format unexpected)")

    # Build the message (RED FOLDER ONLY)
    msg = build_digest(events, source_tz=source_tz, mode="red_only")

    # Post (split if too long)
    for part in chunk_for_discord(msg):
        discord_post(webhook, part)
        time.sleep(0.6)  # small pause to avoid rate limits


if __name__ == "__main__":
    main()
