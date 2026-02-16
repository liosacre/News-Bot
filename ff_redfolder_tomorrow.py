import os
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dtparser


OUT_TZ = ZoneInfo("America/New_York")  # always print in New York / ET


def _impact_label(v) -> str:
    if v is None:
        return "unknown"
    s = str(v).strip().lower()

    if s in ("high", "red", "3", "high impact", "highimpact"):
        return "high"
    if s in ("medium", "orange", "2", "medium impact", "mediumimpact"):
        return "medium"
    if s in ("low", "yellow", "1", "low impact", "lowimpact"):
        return "low"
    if "holiday" in s:
        return "holiday"

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
    for k in ("currency", "ccy", "symbol"):
        if e.get(k):
            return str(e[k]).strip().upper()
    return ""


def _parse_event_dt_out_tz(e: dict, source_tz: ZoneInfo) -> datetime | None:
    """
    Return datetime in OUT_TZ (ET).

    Priority:
      1) timestamp fields (absolute time) -> convert to ET
      2) datetime-like fields with tz -> convert to ET
      3) date + time strings (assumed in source_tz) -> convert to ET
    """
    # 1) timestamps (seconds or ms) — best because absolute
    for k in ("timestamp", "ts", "timeStamp", "time_stamp"):
        v = e.get(k)
        if v is None:
            continue
        try:
            iv = int(str(v))
            if iv > 10_000_000_000:  # ms
                iv //= 1000
            dt_utc = datetime.fromtimestamp(iv, tz=timezone.utc)
            return dt_utc.astimezone(OUT_TZ)
        except Exception:
            pass

    # 2) datetime field
    for k in ("datetime", "dateTime", "date_time"):
        if e.get(k):
            try:
                dt = dtparser.parse(str(e[k]))
                if dt.tzinfo is None:
                    # If FF gives datetime without tz, assume source_tz
                    dt = dt.replace(tzinfo=source_tz)
                return dt.astimezone(OUT_TZ)
            except Exception:
                pass

    # 3) date + time fields (THIS is where your bug usually is)
    date_str = (e.get("date") or e.get("day") or e.get("eventDate") or "").strip()
    time_str = (e.get("time") or e.get("eventTime") or "").strip()

    if not date_str:
        return None

    is_tbd = (not time_str) or (time_str.lower() in ("all day", "tentative", "tbd"))
    if is_tbd:
        # put noon in source_tz so it groups on the correct day, then convert
        try:
            d = dtparser.parse(date_str).date()
            dt = datetime(d.year, d.month, d.day, 12, 0, tzinfo=source_tz)
            return dt.astimezone(OUT_TZ)
        except Exception:
            return None

    try:
        # Parse naive, then force SOURCE timezone, then convert to ET
        dt_naive = dtparser.parse(f"{date_str} {time_str}")
        if dt_naive.tzinfo is None:
            dt_src = dt_naive.replace(tzinfo=source_tz)
        else:
            dt_src = dt_naive.astimezone(source_tz)
        return dt_src.astimezone(OUT_TZ)
    except Exception:
        return None


def fetch_events(ff_json_url: str) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json,text/plain,*/*"}
    r = requests.get(ff_json_url, headers=headers, timeout=30)
    r.raise_for_status()

    text = r.text.strip()
    try:
        data = r.json()
    except Exception as ex:
        snippet = text[:300].replace("\n", " ")
        raise RuntimeError(f"Could not parse JSON. First bytes: {snippet}") from ex

    if isinstance(data, dict):
        for k in ("events", "data", "calendar", "items"):
            if isinstance(data.get(k), list):
                return data[k]
        raise RuntimeError("Unexpected JSON shape (dict).")
    if not isinstance(data, list):
        raise RuntimeError("Unexpected JSON shape (not a list).")

    return data


def chunk_for_discord(s: str, limit: int = 1900) -> list[str]:
    lines = s.splitlines()
    chunks, buf, size = [], [], 0
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
    payload = {"content": content, "allowed_mentions": {"parse": ["everyone"]}}
    r = requests.post(webhook, json=payload, timeout=20)
    r.raise_for_status()


def build_weekly_digest(events: list[dict], source_tz: ZoneInfo) -> str:
    now_et = datetime.now(OUT_TZ)
    week_start = (now_et - timedelta(days=now_et.weekday())).date()  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday

    filtered = []
    for e in events:
        if _get_currency(e) != "USD":
            continue

        impact = _impact_label(e.get("impact") or e.get("impactTitle") or e.get("impact_title"))
        if impact != "high":  # red folder only
            continue

        dt_et = _parse_event_dt_out_tz(e, source_tz)
        if dt_et is None:
            continue

        # Keep only this week (Mon–Sun) in ET
        if not (week_start <= dt_et.date() <= week_end):
            continue

        title = (e.get("title") or e.get("event") or e.get("name") or "Unknown Event").strip()
        time_str_raw = str(e.get("time") or "").strip().lower()
        tbd = (not time_str_raw) or (time_str_raw in ("all day", "tentative", "tbd"))

        filtered.append({"dt": dt_et, "title": title, "tbd": tbd})

    filtered.sort(key=lambda x: x["dt"])

    out = [
        "@everyone",
        "🔴 **USD RED FOLDER — THIS WEEK (NEW YORK / ET)**",
        f"Week of **{week_start.strftime('%b %d, %Y')}** → **{week_end.strftime('%b %d, %Y')}**",
        "",
    ]

    if not filtered:
        out.append("• No **USD high-impact (red folder)** events found in this week’s feed.")
        return "\n".join(out).strip()

    cur_day = None
    for item in filtered:
        if item["dt"].date() != cur_day:
            cur_day = item["dt"].date()
            out.append("")
            out.append(f"**{item['dt'].strftime('%A').upper()} ({item['dt'].strftime('%b %d')})**")

        if item["tbd"]:
            out.append(f"• **TBD ET** | {item['title']}")
        else:
            # %-I works on Linux (GitHub runners). If it ever breaks, switch to %I and strip leading 0.
            out.append(f"• **{item['dt'].strftime('%-I:%M %p')} ET** | {item['title']}")

    return "\n".join(out).strip()


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    ff_json_url = os.environ.get("FF_JSON_URL", "").strip()

    # IMPORTANT: set this to match the timezone your FF calendar/export is showing
    # Your screenshots show LA/Pacific -> keep default as America/Los_Angeles
    source_tz_name = os.environ.get("FF_SOURCE_TZ", "America/Los_Angeles").strip()
    source_tz = ZoneInfo(source_tz_name)

    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL secret/env var.")
    if not ff_json_url:
        raise RuntimeError("Missing FF_JSON_URL secret/env var (your thisweek.json export link).")

    events = fetch_events(ff_json_url)
    message = build_weekly_digest(events, source_tz)

    for chunk in chunk_for_discord(message):
        discord_post(webhook, chunk)


if __name__ == "__main__":
    main()
