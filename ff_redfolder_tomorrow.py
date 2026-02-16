#!/usr/bin/env python3
"""
ForexFactory USD Red Folder (High impact) — Weekly Digest (New York / ET)

Required env vars (set via GitHub Actions):
  - DISCORD_WEBHOOK_URL : Discord webhook URL
  - FF_JSON_URL         : your ForexFactory "thisweek.json" export link

Optional:
  - FF_SOURCE_TZ        : timezone the FF export's date/time strings are in (default: America/Los_Angeles)
                          Only used if the JSON does NOT include a timestamp/ISO datetime.
  - DAYS_AHEAD          : default 7
  - DEBUG               : "1" to print extra debug info
"""

import os
import json
import re
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


# ----------------------------
# Config
# ----------------------------
TARGET_TZ = ZoneInfo("America/New_York")  # Output timezone (ET / New York)
SOURCE_TZ = ZoneInfo(os.getenv("FF_SOURCE_TZ", "America/Los_Angeles"))  # fallback parsing tz
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
DEBUG = os.getenv("DEBUG", "0") == "1"

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
FF_JSON_URL = os.getenv("FF_JSON_URL", "").strip()

UA = "Mozilla/5.0 (GitHubActions; ForexFactoryDigest)"


# ----------------------------
# Helpers
# ----------------------------
def die(msg: str) -> None:
    raise RuntimeError(msg)


def normalize_currency(e: dict) -> str:
    for k in ("currency", "cur", "ccy"):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return ""


def normalize_title(e: dict) -> str:
    for k in ("title", "event", "name", "headline"):
        v = e.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "(Untitled event)"


def normalize_impact(e: dict) -> str:
    """
    Return one of: "high", "medium", "low", "unknown"
    Handles: "High", "Medium", "Low", numeric 3/2/1, "red" etc.
    """
    v = e.get("impact", e.get("impactTitle", e.get("impact_title")))
    if v is None:
        return "unknown"

    # numeric
    if isinstance(v, (int, float)):
        if int(v) >= 3:
            return "high"
        if int(v) == 2:
            return "medium"
        if int(v) == 1:
            return "low"
        return "unknown"

    s = str(v).strip().lower()
    if not s:
        return "unknown"

    if any(x in s for x in ("high", "red", "3", "high impact", "highimpact")):
        return "high"
    if any(x in s for x in ("medium", "orange", "2", "med")):
        return "medium"
    if any(x in s for x in ("low", "yellow", "1")):
        return "low"
    return "unknown"


def parse_isoish(s: str):
    s = s.strip()
    if not s:
        return None
    # handle trailing Z
    if s.endswith("Z"):
        s2 = s[:-1] + "+00:00"
    else:
        s2 = s
    try:
        return datetime.fromisoformat(s2)
    except Exception:
        return None


def parse_date_str(date_str: str) -> datetime.date | None:
    """
    Accepts:
      - 2026-02-18
      - 2026.02.18
      - Feb 18 2026
      - Feb 18
      - 18 Feb 2026
    """
    if not date_str:
        return None
    s = str(date_str).strip()

    # Normalize separators
    s = s.replace("/", "-").replace(".", "-")

    fmts = [
        "%Y-%m-%d",
        "%b %d %Y",
        "%B %d %Y",
        "%d %b %Y",
        "%d %B %Y",
        "%b %d",
        "%B %d",
    ]
    for f in fmts:
        try:
            dt = datetime.strptime(s, f)
            return dt.date()
        except Exception:
            continue

    # Sometimes FF has "Feb 18, 2026"
    s2 = s.replace(",", "")
    for f in ["%b %d %Y", "%B %d %Y"]:
        try:
            dt = datetime.strptime(s2, f)
            return dt.date()
        except Exception:
            continue

    return None


def parse_time_str(time_str: str) -> tuple[int, int] | None:
    """
    Accepts:
      - 11:00am / 11:00 am / 11:00 AM
      - 5:30am
      - 13:30
      - "All Day" / "Tentative" -> None
    """
    if not time_str:
        return None
    s = str(time_str).strip().lower()
    if not s or s in ("all day", "tentative", "tbd", "n/a"):
        return None

    s = s.replace(" ", "")
    # 24h HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})$", s)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 12h HH:MMam
    m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)$", s)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ap = m.group(3)
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        return hh, mm

    # Sometimes "11am"
    m = re.match(r"^(\d{1,2})(am|pm)$", s)
    if m:
        hh = int(m.group(1))
        ap = m.group(2)
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        return hh, 0

    return None


def event_datetime_et(e: dict) -> datetime | None:
    """
    Best effort:
      1) unix timestamp fields -> assume UTC, convert to ET
      2) ISO-ish datetime fields -> respect offset if present, otherwise assume SOURCE_TZ
      3) date + time strings -> assume SOURCE_TZ, convert to ET
    """
    # 1) timestamp (seconds or ms)
    for k in ("timestamp", "ts", "timeStamp", "time_stamp"):
        v = e.get(k)
        if v is None:
            continue
        try:
            n = int(str(v))
            if n > 1_000_000_000_000:  # ms
                n //= 1000
            dt_utc = datetime.fromtimestamp(n, tz=timezone.utc)
            return dt_utc.astimezone(TARGET_TZ)
        except Exception:
            pass

    # 2) ISO-ish
    for k in ("datetime", "dateTime", "dt"):
        v = e.get(k)
        if not v:
            continue
        dt = parse_isoish(str(v))
        if dt is None:
            continue
        # If no tzinfo, assume SOURCE_TZ
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SOURCE_TZ)
        return dt.astimezone(TARGET_TZ)

    # 3) date + time
    date_str = e.get("date") or e.get("day") or e.get("event_date")
    time_str = e.get("time") or e.get("event_time") or e.get("hour")

    d = parse_date_str(str(date_str)) if date_str else None
    if d is None:
        return None

    hm = parse_time_str(str(time_str)) if time_str else None
    if hm is None:
        # All-day / missing time -> set 00:00 in source tz
        dt_src = datetime(d.year, d.month, d.day, 0, 0, tzinfo=SOURCE_TZ)
        return dt_src.astimezone(TARGET_TZ)

    hh, mm = hm
    dt_src = datetime(d.year, d.month, d.day, hh, mm, tzinfo=SOURCE_TZ)
    return dt_src.astimezone(TARGET_TZ)


def fmt_time(dt: datetime) -> str:
    # "1:30 PM"
    h = dt.strftime("%I").lstrip("0") or "12"
    return f"{h}:{dt.strftime('%M')} {dt.strftime('%p')}"


def fmt_day_header(d: datetime) -> str:
    # "WEDNESDAY (Feb 18)"
    return f"{d.strftime('%A').upper()} ({d.strftime('%b %d')})"


def discord_post(webhook: str, content: str) -> None:
    r = requests.post(webhook, json={"content": content}, timeout=25)
    r.raise_for_status()


def chunk_messages(text: str, limit: int = 1900):
    """
    Discord hard limit is 2000 chars; keep margin.
    Splits on double newlines when possible.
    """
    parts = []
    cur = ""
    for block in text.split("\n\n"):
        if not cur:
            nxt = block
        else:
            nxt = cur + "\n\n" + block

        if len(nxt) <= limit:
            cur = nxt
        else:
            if cur:
                parts.append(cur)
                cur = block
            else:
                # single block too big -> hard split
                for i in range(0, len(block), limit):
                    parts.append(block[i : i + limit])
                cur = ""
