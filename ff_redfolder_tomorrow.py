#!/usr/bin/env python3
"""
USD RED FOLDER (High impact) -> Discord weekly digest (New York / ET)

✅ USD only
✅ RED folder only (High impact only)
✅ Correct New York time (ET)
✅ Day header shows the *event day* (ex: WEDNESDAY (Feb 18))
✅ Includes @everyone (and enables it)
✅ Uses ForexFactory "thisweek.json" export link from FF_JSON_URL
✅ Uses Discord webhook from DISCORD_WEBHOOK_URL
✅ Safe splitting for Discord 2000-char limit

REQUIRED env vars (GitHub Actions Secrets):
- DISCORD_WEBHOOK_URL
- FF_JSON_URL

OPTIONAL:
- FF_SOURCE_TZ (default: America/Los_Angeles)
  Use this ONLY if your JSON does not include a timestamp field and you need to interpret date+time strings.
"""

from __future__ import annotations

import os
import time
import json
import re
from datetime import datetime, timedelta, timezone, date
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")


# ---------------------------
# Env helpers
# ---------------------------
def env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(
            f"Missing {name} env var (GitHub Secret). "
            f"Create it in Repo → Settings → Secrets and variables → Actions."
        )
    return v


# ---------------------------
# ForexFactory JSON fetch
# ---------------------------
def fetch_ff_events(url: str) -> List[Dict[str, Any]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (GitHubActions; FF-Discord-Digest)",
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    # Some servers send JSON with text/plain; try json() then fallback to loads.
    try:
        data = r.json()
    except Exception:
        data = json.loads(r.text)

    # Handle common shapes
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        for k in ("events", "data", "calendar", "rows", "items", "result"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
        # Sometimes it's {"0": {...}, "1": {...}}
        if all(isinstance(v, dict) for v in data.values()):
            return list(data.values())

    return []


# ---------------------------
# Field normalizers
# ---------------------------
def get_currency(ev: Dict[str, Any]) -> str:
    for k in ("currency", "cur", "ccy", "Currency"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return ""


def get_title(ev: Dict[str, Any]) -> str:
    for k in ("title", "event", "name", "headline", "text"):
        v = ev.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "Unnamed event"


def impact_is_high(ev: Dict[str, Any]) -> bool:
    raw = (
        ev.get("impact")
        or ev.get("impactTitle")
        or ev.get("impact_title")
        or ev.get("importance")
        or ev.get("volatility")
        or ""
    )

    # numeric
    if isinstance(raw, (int, float)):
        return int(raw) >= 3

    s = str(raw).strip().lower()
    if not s:
        return False

    # Common values: "High", "high", "High Impact Expected", "red", "3"
    if "high" in s or "red" in s:
        return True
    if s == "3":
        return True

    return False


# ---------------------------
# Datetime parsing (correct ET)
# ---------------------------
def parse_unix_timestamp_to_et(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    try:
        n = int(str(v).strip())
    except Exception:
        return None

    # ms -> s
    if n > 10_000_000_000:
        n //= 1000

    try:
        dt_utc = datetime.fromtimestamp(n, tz=timezone.utc)
        return dt_utc.astimezone(ET)
    except Exception:
        return None


def parse_date_str(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip()
    # Most FF exports use YYYY-MM-DD
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        pass

    # Fallback: try common "Feb 18" / "Feb 18 2026"
    for fmt in ("%b %d %Y", "%B %d %Y", "%b %d", "%B %d"):
        try:
            return datetime.strptime(s.replace(",", ""), fmt).date()
        except Exception:
            pass

    return None


def parse_time_str(s: str) -> Optional[Tuple[int, int]]:
    if not s:
        return None
    t = s.strip().lower()
    if t in ("all day", "tentative", "tbd", "n/a"):
        return None

    t = t.replace(" ", "")
    # 24h HH:MM
    m = re.match(r"^(\d{1,2}):(\d{2})$", t)
    if m:
        return int(m.group(1)), int(m.group(2))

    # 12h HH:MMam
    m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)$", t)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2))
        ap = m.group(3)
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        return hh, mm

    # 12h HHam
    m = re.match(r"^(\d{1,2})(am|pm)$", t)
    if m:
        hh = int(m.group(1))
        ap = m.group(2)
        if hh == 12:
            hh = 0
        if ap == "pm":
            hh += 12
        return hh, 0

    return None


def event_datetime_et(ev: Dict[str, Any], source_tz: ZoneInfo) -> Optional[datetime]:
    """
    Priority:
      1) timestamp -> UTC -> ET (best)
      2) date+time strings -> interpreted in source_tz -> ET (fallback)
    """
    # 1) timestamp fields
    for k in ("timestamp", "timeStamp", "ts", "date_timestamp", "event_timestamp"):
        dt_et = parse_unix_timestamp_to_et(ev.get(k))
        if dt_et is not None:
            return dt_et

    # 2) fallback: date + time strings (interpret as source_tz)
    d = parse_date_str(str(ev.get("date") or ev.get("day") or "").strip())
    if d is None:
        return None

    time_raw = str(ev.get("time") or "").strip()
    hm = parse_time_str(time_raw)

    if hm is None:
        # All-day/tentative -> noon source time (stable), convert to ET
        dt_src = datetime(d.year, d.month, d.day, 12, 0, tzinfo=source_tz)
        return dt_src.astimezone(ET)

    hh, mm = hm
    dt_src = datetime(d.year, d.month, d.day, hh, mm, tzinfo=source_tz)
    return dt_src.astimezone(ET)


# ---------------------------
# Discord formatting
# ---------------------------
def fmt_day_header(d: datetime) -> str:
    return f"{d.strftime('%A').upper()} ({d.strftime('%b')} {d.day})"


def fmt_time(d: datetime) -> str:
    # "2:00 PM ET"
    h = d.strftime("%I").lstrip("0") or "12"
    return f"{h}:{d.strftime('%M')} {d.strftime('%p')} ET"


def chunk_for_discord(text: str, limit: int = 1900) -> List[str]:
    text = text.strip()
    if len(text) <= limit:
        return [text]

    parts: List[str] = []
    cur = ""

    for block in text.split("\n\n"):
        nxt = block if not cur else cur + "\n\n" + block
        if len(nxt) <= limit:
            cur = nxt
        else:
            if cur:
                parts.append(cur)
                cur = block
            else:
                # hard split
                for i in range(0, len(block), limit):
                    parts.append(block[i : i + limit])
                cur = ""

    if cur:
        parts.append(cur)

    return parts


def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},  # enable @everyone
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()


# ---------------------------
# Main logic
# ---------------------------
def build_weekly_red_folder_message(events: List[Dict[str, Any]], source_tz: ZoneInfo) -> str:
    now_et = datetime.now(ET)
    end_et = now_et + timedelta(days=7)

    picked: List[Tuple[datetime, str]] = []  # (dt_et, title)

    for ev in events:
        if get_currency(ev) != "USD":
            continue
        if not impact_is_high(ev):  # RED folder only
            continue

        dt_et = event_datetime_et(ev, source_tz)
        if dt_et is None:
            continue

        if not (now_et <= dt_et <= end_et):
            continue

        picked.append((dt_et, get_title(ev)))

    picked.sort(key=lambda x: x[0])

    lines: List[str] = []
    lines.append("@everyone")
    lines.append("🔴 **USD RED FOLDER — NEXT 7 DAYS (NEW YORK / ET)**")
    lines.append("")

    if not picked:
        lines.append("• No USD **High-impact (Red folder)** events found in the next 7 days.")
        return "\n".join(lines).strip()

    current_day: Optional[date] = None
    for dt_et, title in picked:
        if current_day != dt_et.date():
            if current_day is not None:
                lines.append("")
            lines.append(f"**{fmt_day_header(dt_et)}**")
            current_day = dt_et.date()

        lines.append(f"• **{fmt_time(dt_et)}** | {title}")

    return "\n".join(lines).strip()


def main() -> None:
    webhook = env_required("DISCORD_WEBHOOK_URL")
    ff_url = env_required("FF_JSON_URL")

    source_tz_name = os.environ.get("FF_SOURCE_TZ", "America/Los_Angeles").strip() or "America/Los_Angeles"
    try:
        source_tz = ZoneInfo(source_tz_name)
    except Exception:
        raise RuntimeError(f"Invalid FF_SOURCE_TZ timezone: {source_tz_name}")

    events = fetch_ff_events(ff_url)
    if not events:
        raise RuntimeError("No events parsed from FF_JSON_URL (JSON structure unexpected or empty).")

    msg = build_weekly_red_folder_message(events, source_tz)

    for i, part in enumerate(chunk_for_discord(msg)):
        # Only ping everyone once
        if i > 0:
            part = part.replace("@everyone\n", "", 1)
        discord_post(webhook, part)
        time.sleep(0.6)


if __name__ == "__main__":
    main()
