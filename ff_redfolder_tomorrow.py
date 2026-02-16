#!/usr/bin/env python3
"""
ForexFactory thisweek.json -> Discord
✅ USD only (uses 'country' field from FF feed)
✅ RED folder only (impact == "High")
✅ Correct New York time (ET) (parses ISO 'date' with offset, converts to America/New_York)
✅ Template shows actual event day (e.g., WEDNESDAY (Feb 18))
✅ Includes @everyone (and enables it via allowed_mentions)
✅ Splits messages to stay under Discord 2000-char limit

Required env vars (GitHub Actions Secrets):
- DISCORD_WEBHOOK_URL
- FF_JSON_URL
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

ET = ZoneInfo("America/New_York")


def env_required(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise RuntimeError(
            f"Missing {name} env var (GitHub Secret). "
            f"Add it in Repo → Settings → Secrets and variables → Actions."
        )
    return v


def fetch_events(ff_url: str) -> List[Dict[str, Any]]:
    headers = {
        "User-Agent": "Mozilla/5.0 (GitHubActions; FFDiscordBot)",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(ff_url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if isinstance(data, dict):
        # in case it ever wraps events
        for k in ("events", "data", "items", "calendar"):
            v = data.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]

    return []


def is_usd(ev: Dict[str, Any]) -> bool:
    # ForexFactory feed uses "country": "USD" / "EUR" etc.
    return str(ev.get("country", "")).strip().upper() == "USD"


def is_red_folder(ev: Dict[str, Any]) -> bool:
    # ForexFactory feed uses "impact": "High"/"Medium"/"Low"/"Holiday"
    return str(ev.get("impact", "")).strip().lower() == "high"


def get_title(ev: Dict[str, Any]) -> str:
    t = str(ev.get("title", "")).strip()
    return t if t else "Unnamed event"


def parse_dt_et(ev: Dict[str, Any]) -> Optional[datetime]:
    """
    ForexFactory feed date is ISO with offset, ex: 2026-02-18T14:00:00-05:00
    We'll parse it and convert to ET timezone.
    """
    s = str(ev.get("date", "")).strip()
    if not s:
        return None
    try:
        dt_obj = datetime.fromisoformat(s)
    except Exception:
        return None

    # If tz missing (unlikely), assume ET
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=ET)

    return dt_obj.astimezone(ET)


def fmt_day_header(d: datetime) -> str:
    # Example: WEDNESDAY (Feb 18)
    return f"{d.strftime('%A').upper()} ({d.strftime('%b')} {d.day})"


def fmt_time(d: datetime) -> str:
    # Example: 2:00 PM ET
    hour = d.strftime("%I").lstrip("0") or "12"
    return f"{hour}:{d.strftime('%M')} {d.strftime('%p')} ET"


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
        "allowed_mentions": {"parse": ["everyone"]},
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()


def build_message(events: List[Dict[str, Any]]) -> str:
    now_et = datetime.now(ET)
    end_et = now_et + timedelta(days=7)

    picked: List[Tuple[datetime, str]] = []
    for ev in events:
        if not is_usd(ev):
            continue
        if not is_red_folder(ev):
            continue

        dt_et = parse_dt_et(ev)
        if not dt_et:
            continue

        # Only upcoming next 7 days
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

    current_day = None
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

    events = fetch_events(ff_url)
    if not events:
        raise RuntimeError("No events parsed from FF_JSON_URL (unexpected JSON shape or empty).")

    msg = build_message(events)

    for i, part in enumerate(chunk_for_discord(msg)):
        # only ping everyone in first chunk
        if i > 0:
            part = part.replace("@everyone\n", "", 1)
        discord_post(webhook, part)
        time.sleep(0.6)


if __name__ == "__main__":
    main()
