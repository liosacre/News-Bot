#!/usr/bin/env python3
"""
ForexFactory USD Red Folder (High impact) — Weekly Digest (New York / ET)

Required env vars (set via GitHub Actions):
  - DISCORD_WEBHOOK_URL : Discord webhook URL
  - FF_JSON_URL         : your ForexFactory "thisweek.json" export link

Optional:
  - FF_SOURCE_TZ : timezone the FF export's date/time strings are in IF JSON has no timestamp
                   default: America/Los_Angeles
  - DAYS_AHEAD   : default 7
  - DEBUG        : "1" for extra debug lines
"""

import os
import re
import requests
from datetime import datetime, timedelta, timezone, date
from typing import Optional, Tuple, List
from zoneinfo import ZoneInfo


# ----------------------------
# Config
# ----------------------------
TARGET_TZ = ZoneInfo("America/New_York")  # output timezone
SOURCE_TZ = ZoneInfo(os.getenv("FF_SOURCE_TZ", "America/Los_Angeles"))  # fallback input tz
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
DEBUG = os.getenv("DEBUG", "0") == "1"

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
FF_JSON_URL = os.getenv("FF_JSON_URL", "").strip()

UA = "Mozilla/5.0 (GitHubActions; ForexFactoryDigest)"


def die(msg: str) -> None:
    raise RuntimeError(msg)


# ----------------------------
# Normalizers
# ----------------------------
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
    Returns: "high", "medium", "low", "unknown"
    Handles: "High", numeric 3/2/1, "red", etc.
    """
    v = e.get("impact", e.get("impactTitle", e.get("impact_title")))
    if v is None:
        return "unknown"

    if isinstance(v, (int, float)):
        n = int(v)
        if n >= 3:
            return "high"
        if n == 2:
            return "medium"
        if n == 1:
            return "low"
        return "unknown"

    s = str(v).strip().lower()
    if not s:
        return "unknown"

    if any(x in s for x in ("high", "red", "high impact", "highimpact", "3")):
        return "high"
    if any(x in s for x in ("medium", "orange", "med", "2")):
        return "medium"
    if any(x in s for x in ("low", "yellow", "1")):
        return "low"
    return "unknown"


# ----------------------------
# Parsing helpers
# ----------------------------
def parse_isoish(s: str) -> Optional[datetime]:
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def parse_date_str(date_str: str) -> Optional[date]:
    """
    Accepts:
      - 2026-02-18
      - 2026.02.18
      - Feb 18 2026
      - Feb 18, 2026
      - Feb 18
    """
    if not date_str:
        return None
    s = str(date_str).strip()
    s = s.replace("/", "-").replace(".", "-")
    s_no_comma = s.replace(",", "")

    fmts = [
        "%Y-%m-%d",
        "%b %d %Y",
        "%B %d %Y",
        "%b %d",
        "%B %d",
    ]
    for f in fmts:
        for candidate in (s, s_no_comma):
            try:
                return datetime.strptime(candidate, f).date()
            except Exception:
                pass
    return None


def parse_time_str(time_str: str) -> Optional[Tuple[int, int]]:
    """
    Accepts:
      - 11:00am / 11:00 AM / 5:30am
      - 13:30
      - "All Day"/"Tentative"/"TBD" -> None
    """
    if not time_str:
        return None
    s = str(time_str).strip().lower()
    if not s or s in ("all day", "tentative", "tbd", "n/a"):
        return None

    s = s.replace(" ", "")

    m = re.match(r"^(\d{1,2}):(\d{2})$", s)  # 24h
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)$", s)  # 12h
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ap = m.group(3)
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        return hh, mm

    m = re.match(r"^(\d{1,2})(am|pm)$", s)  # e.g. 11am
    if m:
        hh = int(m.group(1))
        ap = m.group(2)
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        return hh, 0

    return None


def event_datetime_et(e: dict) -> Optional[datetime]:
    """
    Priority:
      1) timestamp fields -> assume UTC
      2) ISO-ish datetime -> respect tz if present else assume SOURCE_TZ
      3) date+time strings -> assume SOURCE_TZ
    """
    # 1) unix timestamp (seconds or ms)
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

    # 2) ISO-ish datetime string
    for k in ("datetime", "dateTime", "dt"):
        v = e.get(k)
        if not v:
            continue
        dt = parse_isoish(str(v))
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SOURCE_TZ)
        return dt.astimezone(TARGET_TZ)

    # 3) date + time strings
    date_str = e.get("date") or e.get("day") or e.get("event_date")
    time_str = e.get("time") or e.get("event_time") or e.get("hour")

    d = parse_date_str(str(date_str)) if date_str else None
    if d is None:
        return None

    hm = parse_time_str(str(time_str)) if time_str else None
    if hm is None:
        dt_src = datetime(d.year, d.month, d.day, 0, 0, tzinfo=SOURCE_TZ)
        return dt_src.astimezone(TARGET_TZ)

    hh, mm = hm
    dt_src = datetime(d.year, d.month, d.day, hh, mm, tzinfo=SOURCE_TZ)
    return dt_src.astimezone(TARGET_TZ)


# ----------------------------
# Formatting
# ----------------------------
def fmt_time(dt: datetime) -> str:
    h = dt.strftime("%I").lstrip("0") or "12"
    return f"{h}:{dt.strftime('%M')} {dt.strftime('%p')}"


def fmt_day_header(dt: datetime) -> str:
    return f"{dt.strftime('%A').upper()} ({dt.strftime('%b %d')})"


# ----------------------------
# Discord
# ----------------------------
def discord_post(webhook: str, content: str) -> None:
    r = requests.post(webhook, json={"content": content}, timeout=25)
    r.raise_for_status()


def chunk_messages(text: str, limit: int = 1900) -> List[str]:
    parts: List[str] = []
    cur = ""
    for block in text.split("\n\n"):
        nxt = block if not cur else (cur + "\n\n" + block)
        if len(nxt) <= limit:
            cur = nxt
        else:
            if cur:
                parts.append(cur)
                cur = block
            else:
                for i in range(0, len(block), limit):
                    parts.append(block[i : i + limit])
                cur = ""
    if cur:
        parts.append(cur)
    return parts


# ----------------------------
# Main
# ----------------------------
def main() -> None:
    if not DISCORD_WEBHOOK_URL:
        die("Missing DISCORD_WEBHOOK_URL env var (GitHub Secret).")
    if not FF_JSON_URL:
        die("Missing FF_JSON_URL env var (GitHub Secret) — set it to your thisweek.json export link.")

    headers = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    resp = requests.get(FF_JSON_URL, headers=headers, timeout=30)
    resp.raise_for_status()

    try:
        data = resp.json()
    except Exception:
        ct = resp.headers.get("content-type", "")
        die(f"FF_JSON_URL did not return JSON (content-type: {ct}). Make sure it's the *thisweek.json* export link.")

    # Extract events from different shapes
    if isinstance(data, list):
        events = data
    elif isinstance(data, dict):
        events = None
        for key in ("events", "data", "calendar", "result", "items"):
            if isinstance(data.get(key), list):
                events = data[key]
                break
        if events is None and all(isinstance(v, dict) for v in data.values()):
            events = list(data.values())
        if events is None:
            events = []
    else:
        events = []

    now_et = datetime.now(TARGET_TZ)
    end_et = now_et + timedelta(days=DAYS_AHEAD)

    usd_high: List[Tuple[datetime, str]] = []

    for e in events:
        if not isinstance(e, dict):
            continue

        if normalize_currency(e) != "USD":
            continue

        if normalize_impact(e) != "high":  # RED folder only
            continue

        dt_et = event_datetime_et(e)
        if dt_et is None:
            continue

        if not (now_et <= dt_et < end_et):
            continue

        usd_high.append((dt_et, normalize_title(e)))

    usd_high.sort(key=lambda x: x[0])

    # Template (what Discord should show)
    lines: List[str] = []
    lines.append("@everyone")
    lines.append("🔴 **USD RED FOLDER THIS WEEK (NEW YORK / ET)**")
    lines.append("")

    if not usd_high:
        lines.append(f"• ✅ No USD **High-impact (Red folder)** events found in the next {DAYS_AHEAD} days.")
    else:
        current_day: Optional[date] = None
        for dt_et, title in usd_high:
            if current_day != dt_et.date():
                if current_day is not None:
                    lines.append("")
                lines.append(f"**{fmt_day_header(dt_et)}**")
                current_day = dt_et.date()
            lines.append(f"• **{fmt_time(dt_et)} ET** | {title}")

    if DEBUG:
        lines.append("")
        lines.append(f"(debug: fetched={len(events)} total | usd_high_in_range={len(usd_high)} | now_et={now_et.isoformat()} | source_tz={SOURCE_TZ})")

    message = "\n".join(lines)

    parts = chunk_messages(message)
    for i, part in enumerate(parts):
        if i > 0:
            part = part.replace("@everyone\n", "", 1)
        discord_post(DISCORD_WEBHOOK_URL, part)


if __name__ == "__main__":
    main()
