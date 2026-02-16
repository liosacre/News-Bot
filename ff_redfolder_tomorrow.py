#!/usr/bin/env python3
"""
ff_redfolder_tomorrow.py

Reads a ForexFactory weekly JSON export link (THISWEEK) and posts USD red-folder (High impact)
events to Discord using a webhook.

REQUIRED env vars (GitHub Actions Secrets):
- DISCORD_WEBHOOK_URL  -> your Discord webhook URL
- FF_JSON_URL          -> your exported "thisweek.json" link (full URL)

Optional env vars:
- FF_SOURCE_TZ         -> timezone the FF "time" field is in (default: America/Los_Angeles)
                          Set this to match what you see on the FF calendar page.
"""

import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import requests
from dateutil import tz, parser


# ----------------------------
# Config (env)
# ----------------------------
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
FF_JSON_URL = os.environ.get("FF_JSON_URL", "").strip()

# The timezone that the FF "time" text should be interpreted as
# (ForexFactory calendar UI time). Your screenshot showed LA time.
FF_SOURCE_TZ_NAME = os.environ.get("FF_SOURCE_TZ", "America/Los_Angeles").strip() or "America/Los_Angeles"

# Output timezone (New York / ET)
OUT_TZ = tz.gettz("America/New_York")

# Discord message options
PING_EVERYONE = True  # you asked for @everyone


# ----------------------------
# Helpers
# ----------------------------
def die(msg: str) -> None:
    raise RuntimeError(msg)


def http_get_json(url: str) -> Any:
    headers = {
        "User-Agent": "Mozilla/5.0 (GitHubActions; FFDiscordBot)",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    # Some endpoints may return JSON with an incorrect content-type; still parse.
    try:
        return r.json()
    except Exception as e:
        snippet = r.text[:300].replace("\n", "\\n")
        raise RuntimeError(f"Failed to parse JSON. status={r.status_code}, snippet={snippet}") from e


def is_usd(ev: Dict[str, Any]) -> bool:
    c = (ev.get("currency") or ev.get("cur") or "").strip().upper()
    return c == "USD"


def impact_level(ev: Dict[str, Any]) -> str:
    """
    ForexFactory exports vary. We normalize impact into: 'high', 'medium', 'low', or ''.
    """
    raw = ev.get("impact") or ev.get("impactTitle") or ev.get("imp") or ""
    s = str(raw).strip().lower()

    # Sometimes it's numeric/encoded or class names
    # Common: "High", "Medium", "Low"
    if "high" in s:
        return "high"
    if "medium" in s:
        return "medium"
    if "low" in s:
        return "low"

    # Some exports use digits like 3/2/1
    if s in ("3", "highimpact", "high-impact", "red"):
        return "high"
    if s in ("2", "mediumimpact", "orange"):
        return "medium"
    if s in ("1", "lowimpact", "yellow"):
        return "low"

    return ""


def title_of(ev: Dict[str, Any]) -> str:
    return (ev.get("title") or ev.get("event") or ev.get("name") or ev.get("description") or "").strip()


def looks_all_day(time_str: str) -> bool:
    s = time_str.strip().lower()
    return s in ("all day", "tentative", "tbd", "na", "n/a", "")


def parse_event_datetime(ev: Dict[str, Any], source_tz) -> Optional[datetime]:
    """
    Convert an FF event into a timezone-aware datetime in source_tz.
    Returns None for all-day/tentative events with no usable time.
    """
    # Many FF exports include a unix timestamp
    for k in ("timestamp", "ts", "timeStamp", "time_stamp"):
        v = ev.get(k)
        if v is not None and str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=tz.UTC).astimezone(source_tz)

    date_str = (ev.get("date") or ev.get("day") or "").strip()
    time_str = (ev.get("time") or ev.get("datetime") or "").strip()

    if not date_str:
        return None

    # If it's all day / tentative: treat as date-only; no clock time
    if looks_all_day(time_str):
        # Parse date only in source tz at 00:00
        d = parser.parse(date_str).date()
        return datetime(d.year, d.month, d.day, 0, 0, tzinfo=source_tz)

    # Some exports use "Feb 18" + separate year; parser handles most, but we
    # keep it simple and rely on parser with timezone injection.
    dt = parser.parse(f"{date_str} {time_str}", fuzzy=True)

    # Force timezone if naive
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=source_tz)
    else:
        dt = dt.astimezone(source_tz)

    return dt


def fmt_time_et(dt_source: datetime) -> str:
    """
    Output as ET time (New York), like '7:00 PM ET' or 'All Day ET'
    """
    dt_et = dt_source.astimezone(OUT_TZ)
    return dt_et.strftime("%-I:%M %p ET")


def fmt_day_heading(dt_source: datetime) -> str:
    dt_et = dt_source.astimezone(OUT_TZ)
    return dt_et.strftime("%A (%b %-d)")  # e.g., Wednesday (Feb 18)


def within_next_7_days(dt_source: datetime, now_et: datetime) -> bool:
    dt_et = dt_source.astimezone(OUT_TZ)
    return now_et <= dt_et < (now_et + timedelta(days=7))


def discord_post(webhook_url: str, content: str) -> None:
    r = requests.post(webhook_url, json={"content": content}, timeout=25)
    # Discord can return 204 No Content for success
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Discord post failed: {r.status_code} {r.text[:250]}")


def build_message(events: List[Dict[str, Any]]) -> str:
    """
    Template B style:
    @everyone
    🔴 USD RED FOLDER THIS WEEK (NEW YORK / ET)

    WEDNESDAY (Feb 18)
    • 11:00 AM ET | FOMC Meeting Minutes
    ...
    """
    now_et = datetime.now(tz=OUT_TZ)

    # Group by ET calendar day
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    for ev in events:
        source_tz = tz.gettz(FF_SOURCE_TZ_NAME)
        dt = parse_event_datetime(ev, source_tz)
        if not dt:
            continue
        if not within_next_7_days(dt, now_et):
            continue

        day = fmt_day_heading(dt).upper()  # "WEDNESDAY (Feb 18)"
        grouped[day].append({"dt": dt, "title": title_of(ev)})

    lines: List[str] = []
    if PING_EVERYONE:
        lines.append("@everyone")
    lines.append("🔴 USD RED FOLDER THIS WEEK (NEW YORK / ET)")
    lines.append("")

    if not grouped:
        lines.append("• No USD RED folder (High impact) events found in the next 7 days.")
        return "\n".join(lines)

    # Sort days by first event time
    def day_sort_key(day_key: str) -> datetime:
        return min(item["dt"] for item in grouped[day_key])

    for day in sorted(grouped.keys(), key=day_sort_key):
        lines.append(day)
        # Sort events by time
        items = sorted(grouped[day], key=lambda x: x["dt"])
        for item in items:
            dt = item["dt"]
            title = item["title"] or "(Untitled)"
            # If event was all-day/tentative we forced 00:00; still show "All Day"
            # based on original 'time' string is hard here; keep simple:
            # If time is exactly midnight and title contains "Holiday"/etc you might prefer All Day,
            # but we'll always show a time. You can tweak later.
            lines.append(f"• {fmt_time_et(dt)} | {title}")
        lines.append("")  # blank line between days

    # Trim trailing blank line
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        die("Missing DISCORD_WEBHOOK_URL env var (GitHub Actions secret).")
    if not FF_JSON_URL:
        die("Missing FF_JSON_URL env var (GitHub Actions secret). Put your thisweek.json export link there.")

    source_tz = tz.gettz(FF_SOURCE_TZ_NAME)
    if source_tz is None:
        die(f"Invalid FF_SOURCE_TZ timezone: {FF_SOURCE_TZ_NAME}")

    data = http_get_json(FF_JSON_URL)

    # FF exports might be:
    # - a list of events
    # - or {"events":[...]}
    if isinstance(data, dict) and "events" in data and isinstance(data["events"], list):
        events_raw = data["events"]
    elif isinstance(data, list):
        events_raw = data
    else:
        # Try to find a list anywhere
        events_raw = None
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v and isinstance(v[0], dict):
                    events_raw = v
                    break
        if events_raw is None:
            die("Unexpected JSON shape from FF_JSON_URL (not a list of events).")

    # Filter: USD + High impact (red folder)
    filtered = []
    for ev in events_raw:
        if not isinstance(ev, dict):
            continue
        if not is_usd(ev):
            continue
        if impact_level(ev) != "high":
            continue
        # Must have a title
        if not title_of(ev):
            continue
        filtered.append(ev)

    msg = build_message(filtered)
    discord_post(DISCORD_WEBHOOK_URL, msg)


if __name__ == "__main__":
    main()
