import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

# ========= SETTINGS =========
OUT_TZ = ZoneInfo("America/New_York")     # New York / ET output
TARGET_CCY = "USD"

# Red folder only
ALLOWED_IMPACTS = {"High"}               # change to {"High","Medium"} if you want orange too

# GitHub secrets:
# - DISCORD_WEBHOOK_URL
# - FF_JSON_URL  (your exported "thisweek" json link)
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
FF_JSON_ENV = "FF_JSON_URL"

DEFAULT_FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


# ========= DISCORD =========
def discord_post(webhook_url: str, content: str) -> None:
    MAX = 2000
    chunks = []
    while content:
        if len(content) <= MAX:
            chunks.append(content)
            break
        cut = content.rfind("\n", 0, MAX)
        if cut == -1:
            cut = MAX
        chunks.append(content[:cut])
        content = content[cut:].lstrip("\n")

    for ch in chunks:
        r = requests.post(webhook_url, json={"content": ch}, timeout=20)
        r.raise_for_status()


# ========= FOREXFACTORY FETCH =========
def fetch_ff_json(url: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    data = r.json()

    # common shapes
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("events", "data", "calendar", "rows", "items"):
            if k in data and isinstance(data[k], list):
                return data[k]
    return []


def normalize_currency(e: dict) -> str:
    for k in ("currency", "cur", "ccy"):
        v = e.get(k)
        if v:
            return str(v).strip().upper()
    return ""


def normalize_impact(e: dict) -> str:
    # ForexFactory exports vary; catch lots of shapes
    raw = (
        e.get("impact")
        or e.get("importance")
        or e.get("volatility")
        or e.get("impactTitle")
        or e.get("impact_title")
    )
    s = str(raw or "").strip().lower()

    # handle text like "High", "High Impact Expected", "red", etc.
    if "high" in s or "red" in s or s == "3":
        return "High"
    if "medium" in s or "orange" in s or s == "2":
        return "Medium"
    if "low" in s or "yellow" in s or s == "1":
        return "Low"
    return "Unknown"


def get_title(e: dict) -> str:
    for k in ("title", "event", "name", "headline"):
        v = e.get(k)
        if v:
            return str(v).strip()
    return "Unknown Event"


def parse_event_dt_to_et(e: dict) -> datetime | None:
    """
    IMPORTANT:
    - If timestamp exists, treat it as UNIX epoch in UTC (safe).
    - Convert to ET for display.
    """
    for key in ("timestamp", "timeStamp", "ts", "date_timestamp", "event_timestamp"):
        v = e.get(key)
        if v is None:
            continue
        try:
            n = int(str(v).strip())
            # ms -> s
            if n > 10_000_000_000:
                n //= 1000
            dt_utc = datetime.fromtimestamp(n, tz=timezone.utc)
            return dt_utc.astimezone(OUT_TZ)
        except Exception:
            pass

    # fallback: if only date/time strings exist (less reliable)
    date_str = e.get("date") or e.get("day") or e.get("event_date")
    time_str = e.get("time") or e.get("event_time") or e.get("datetime")
    if not date_str:
        return None

    ds = str(date_str).strip()
    ts = str(time_str or "").strip()

    # some feeds use YYYY-MM-DD
    # if time missing, default noon ET
    if not ts or ts.lower() in ("all day", "tentative", "na", "n/a"):
        try:
            base = datetime.strptime(ds, "%Y-%m-%d").replace(tzinfo=OUT_TZ, hour=12, minute=0)
            return base
        except Exception:
            return None

    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p"):
        try:
            dt = datetime.strptime(f"{ds} {ts}", fmt)
            # assume ET if no timezone info
            return dt.replace(tzinfo=OUT_TZ)
        except Exception:
            continue

    return None


# ========= MESSAGE TEMPLATE (your style) =========
def build_message(events: list[dict]) -> str:
    now_et = datetime.now(OUT_TZ)

    # Next 7 days window (what you asked for)
    end_et = now_et + timedelta(days=7)

    picked = []
    for e in events:
        if normalize_currency(e) != TARGET_CCY:
            continue

        impact = normalize_impact(e)
        if impact not in ALLOWED_IMPACTS:
            continue

        dt_et = parse_event_dt_to_et(e)
        if dt_et is None:
            continue

        if not (now_et <= dt_et <= end_et):
            continue

        picked.append((dt_et, impact, get_title(e)))

    picked.sort(key=lambda x: x[0])

    header = "@everyone\n🔴 **USD RED FOLDER THIS WEEK (NEW YORK / ET)**\n"

    if not picked:
        # keep this short + clear
        return header + "\n• No USD RED folder (High impact) events found in the next 7 days."

    # group by day like your screenshot
    out_lines = [header]
    current_day = None

    for dt_et, impact, title in picked:
        day_key = dt_et.date()
        if day_key != current_day:
            current_day = day_key
            out_lines.append("")  # blank line between days
            out_lines.append(f"**{dt_et.strftime('%A').upper()} ( {dt_et.strftime('%b %-d')} )**")

        time_txt = dt_et.strftime("%-I:%M %p ET")
        out_lines.append(f"• **{time_txt}** | {title}")

    return "\n".join(out_lines).strip()


def main():
    webhook = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip()
    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL secret.")

    ff_url = os.environ.get(FF_JSON_ENV, "").strip() or DEFAULT_FF_JSON_URL
    events = fetch_ff_json(ff_url)

    msg = build_message(events)
    discord_post(webhook, msg)


if __name__ == "__main__":
    main()
