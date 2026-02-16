import os
import json
import textwrap
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dtparser


ET = ZoneInfo("America/New_York")


def _impact_label(v) -> str:
    """Normalize impact to: high/medium/low/holiday/unknown."""
    if v is None:
        return "unknown"
    s = str(v).strip().lower()

    # common strings
    if s in ("high", "red", "3", "high impact", "highimpact"):
        return "high"
    if s in ("medium", "orange", "2", "medium impact", "mediumimpact"):
        return "medium"
    if s in ("low", "yellow", "1", "low impact", "lowimpact"):
        return "low"
    if "holiday" in s:
        return "holiday"

    # sometimes it's numeric but not stringified nicely
    try:
        n = int(float(s))
        if n >= 3:
            return "high"
        if n == 2:
            return "medium"
        if n == 1:
            return "low"
    except Exception:
        pass

    return "unknown"


def _get_currency(e: dict) -> str:
    # ForexFactory exports vary; try a few keys
    for k in ("currency", "ccy", "symbol"):
        if k in e and e[k]:
            return str(e[k]).strip().upper()
    # sometimes it’s in "country"/"title" etc; we only want explicit USD
    return ""


def _parse_event_dt_et(e: dict) -> datetime | None:
    """
    Best-effort parse of event datetime, returned in ET.
    Handles:
      - timestamp/ts/timeStamp seconds
      - datetime/date+time strings
      - date only ("all day"/"tentative") -> noon ET fallback
    """
    # 1) timestamp fields (seconds)
    for k in ("timestamp", "ts", "timeStamp", "time_stamp"):
        v = e.get(k)
        if v is None:
            continue
        try:
            # timestamp may be ms; detect
            iv = int(str(v))
            if iv > 10_000_000_000:  # ms
                iv = iv // 1000
            dt = datetime.fromtimestamp(iv, tz=timezone.utc).astimezone(ET)
            return dt
        except Exception:
            pass

    # 2) combined datetime
    for k in ("datetime", "dateTime", "date_time"):
        if e.get(k):
            try:
                dt = dtparser.parse(str(e[k]))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ET)
                return dt.astimezone(ET)
            except Exception:
                pass

    # 3) date + time
    date_str = e.get("date") or e.get("day") or e.get("eventDate") or ""
    time_str = e.get("time") or e.get("eventTime") or ""

    date_str = str(date_str).strip()
    time_str = str(time_str).strip()

    if not date_str:
        return None

    # if time is "All Day"/"Tentative"/blank -> set noon ET so it groups on the right day
    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        try:
            d = dtparser.parse(date_str).date()
            return datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET)
        except Exception:
            return None

    # Many FF exports show time like "11:00am" and date like "Feb 18"
    # dtparser can handle "Feb 18 11:00am"
    try:
        dt = dtparser.parse(f"{date_str} {time_str}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET)
    except Exception:
        # fallback: parse date only
        try:
            d = dtparser.parse(date_str).date()
            return datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET)
        except Exception:
            return None


def fetch_events(ff_json_url: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(ff_json_url, headers=headers, timeout=30)
    r.raise_for_status()

    ct = (r.headers.get("content-type") or "").lower()
    text = r.text.strip()

    # If FF serves html/xml by mistake, fail loudly with a helpful snippet
    if "json" not in ct:
        # still might be JSON with wrong header; try parse
        try:
            return json.loads(text)
        except Exception:
            snippet = text[:300].replace("\n", " ")
            raise RuntimeError(
                f"FF_JSON_URL did not return JSON (content-type={ct}). "
                f"First bytes: {snippet}"
            )

    try:
        data = r.json()
    except Exception as ex:
        snippet = text[:300].replace("\n", " ")
        raise RuntimeError(f"Could not parse JSON. First bytes: {snippet}") from ex

    # Some exports wrap list in a dict
    if isinstance(data, dict):
        for k in ("events", "data", "calendar", "items"):
            if isinstance(data.get(k), list):
                return data[k]
        # if dict but unknown
        raise RuntimeError("Unexpected JSON shape (dict).")
    if not isinstance(data, list):
        raise RuntimeError("Unexpected JSON shape (not a list).")

    return data


def chunk_for_discord(s: str, limit: int = 1900) -> list[str]:
    """
    Split long message into <= limit chunks, preserving lines where possible.
    """
    lines = s.splitlines()
    chunks = []
    buf = []
    size = 0

    for line in lines:
        add = len(line) + 1
        if size + add > limit and buf:
            chunks.append("\n".join(buf).rstrip())
            buf = [line]
            size = len(line) + 1
        else:
            buf.append(line)
            size += add

    if buf:
        chunks.append("\n".join(buf).rstrip())
    return chunks


def discord_post(webhook: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},  # allow @everyone
    }
    r = requests.post(webhook, json=payload, timeout=20)
    r.raise_for_status()


def build_weekly_digest(events: list[dict]) -> str:
    now_et = datetime.now(ET)
    week_start = (now_et - timedelta(days=now_et.weekday())).date()  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday

    # Filter: USD + HIGH only (red folder)
    filtered = []
    for e in events:
        ccy = _get_currency(e)
        if ccy != "USD":
            continue
        impact = _impact_label(e.get("impact") or e.get("impactTitle") or e.get("impact_title"))
        if impact != "high":
            continue

        dt_et = _parse_event_dt_et(e)
        if dt_et is None:
            continue

        # Keep only events that fall within this calendar week in ET (Mon-Sun)
        if not (week_start <= dt_et.date() <= week_end):
            continue

        title = (e.get("title") or e.get("event") or e.get("name") or "Unknown Event").strip()
        time_str_raw = str(e.get("time") or "").strip().lower()
        is_tbd = (not time_str_raw) or (time_str_raw in ("all day", "tentative", "tbd"))

        filtered.append(
            {
                "dt": dt_et,
                "title": title,
                "tbd": is_tbd,
            }
        )

    filtered.sort(key=lambda x: x["dt"])

    header = [
        "@everyone",
        "🔴 **USD RED FOLDER — THIS WEEK (NEW YORK / ET)**",
        f"Week of **{week_start.strftime('%b %d, %Y')}** → **{week_end.strftime('%b %d, %Y')}**",
        "",
    ]

    if not filtered:
        header.append("• No **USD high-impact (red folder)** events found in this week’s feed.")
        return "\n".join(header).strip()

    # Group by day
    out = header
    cur_day = None
    for item in filtered:
        d = item["dt"].date()
        if d != cur_day:
            cur_day = d
            out.append("")
            out.append(f"**{item['dt'].strftime('%A').upper()} ({item['dt'].strftime('%b %d')})**")
        if item["tbd"]:
            out.append(f"• **TBD ET** | {item['title']}")
        else:
            out.append(f"• **{item['dt'].strftime('%-I:%M %p')} ET** | {item['title']}")

    return "\n".join(out).strip()


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    ff_json_url = os.environ.get("FF_JSON_URL", "").strip()

    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL secret/env var.")
    if not ff_json_url:
        raise RuntimeError("Missing FF_JSON_URL secret/env var (your thisweek.json export link).")

    events = fetch_events(ff_json_url)
    message = build_weekly_digest(events)

    # Send (split if too long)
    for chunk in chunk_for_discord(message):
        discord_post(webhook, chunk)


if __name__ == "__main__":
    main()
