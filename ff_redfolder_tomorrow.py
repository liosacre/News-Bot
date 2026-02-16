import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

# =========================
# SETTINGS (edit these)
# =========================

# Put your ForexFactory JSON export URL in GitHub Secret named FF_JSON_URL (recommended).
# If you don't, it will fall back to this default value below.
DEFAULT_FF_JSON_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Discord webhook comes from GitHub Secret: DISCORD_WEBHOOK_URL
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"

# Output timezone: New York time
OUT_TZ = ZoneInfo("America/New_York")

# Filters
TARGET_CURRENCY = "USD"
ALLOWED_IMPACTS = {"High", "Medium"}   # set to {"High"} if you want ONLY red folders


# =========================
# DISCORD
# =========================

def discord_post(webhook_url: str, content: str) -> None:
    # Discord hard limit is 2000 chars per message
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


# =========================
# FOREXFACTORY PARSING
# =========================

def fetch_ff_json(url: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    # Sometimes servers return HTML even with 200; guard it
    ctype = (r.headers.get("content-type") or "").lower()
    if "json" not in ctype:
        # still try, but fail loudly if not json
        pass

    data = r.json()

    # Common shapes:
    # - list of events
    # - {"events":[...]}
    # - {"data":[...]}
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("events", "data", "calendar", "rows", "items"):
            if k in data and isinstance(data[k], list):
                return data[k]
    return []


def normalize_impact(val) -> str:
    s = str(val or "").strip().lower()
    # common values: "High", "Medium", "Low" OR "high" OR "3" etc.
    if s in ("high", "3", "red"):
        return "High"
    if s in ("medium", "2", "orange"):
        return "Medium"
    if s in ("low", "1", "yellow"):
        return "Low"
    return str(val or "").strip() or "Unknown"


def parse_event_dt_to_et(e: dict) -> datetime | None:
    """
    Best-effort parsing:
    Prefer timestamp fields (usually UTC), convert properly to ET.
    """
    # timestamp fields that appear in exports
    for key in ("timestamp", "timeStamp", "ts", "date_timestamp", "event_timestamp"):
        v = e.get(key)
        if v is None:
            continue
        try:
            n = int(str(v).strip())
            # milliseconds -> seconds
            if n > 10_000_000_000:
                n //= 1000
            dt_utc = datetime.fromtimestamp(n, tz=timezone.utc)
            return dt_utc.astimezone(OUT_TZ)
        except Exception:
            pass

    # fallback: date + time strings (risky, but better than nothing)
    date_str = e.get("date") or e.get("day") or e.get("event_date")
    time_str = e.get("time") or e.get("event_time")
    if not date_str:
        return None

    # If time missing, treat as all-day at 12:00 ET
    if not time_str or str(time_str).strip().lower() in ("all day", "tentative", "na", "n/a"):
        base = datetime.strptime(str(date_str).strip(), "%Y-%m-%d")
        return base.replace(tzinfo=OUT_TZ, hour=12, minute=0)

    # Try common formats
    ds = str(date_str).strip()
    ts = str(time_str).strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M%p", "%Y-%m-%d %I:%M %p"):
        try:
            dt_local = datetime.strptime(f"{ds} {ts}", fmt)
            return dt_local.replace(tzinfo=OUT_TZ)
        except Exception:
            continue

    return None


def get_title(e: dict) -> str:
    for k in ("title", "event", "name", "headline"):
        v = e.get(k)
        if v:
            return str(v).strip()
    return "Unknown Event"


def get_currency(e: dict) -> str:
    for k in ("currency", "cur", "ccy"):
        v = e.get(k)
        if v:
            return str(v).strip().upper()
    return ""


def build_weekly_message(events: list[dict]) -> str:
    now_et = datetime.now(OUT_TZ)

    # Decide “this week” window: now -> next 7 days
    end_et = now_et + timedelta(days=7)

    # Filter + parse
    picked = []
    for e in events:
        ccy = get_currency(e)
        if ccy != TARGET_CURRENCY:
            continue

        impact = normalize_impact(e.get("impact") or e.get("importance") or e.get("volatility"))
        if impact not in ALLOWED_IMPACTS:
            continue

        dt_et = parse_event_dt_to_et(e)
        if dt_et is None:
            continue

        # keep only within the next 7 days (weekly digest)
        if not (now_et <= dt_et <= end_et):
            continue

        picked.append((dt_et, impact, get_title(e)))

    picked.sort(key=lambda x: x[0])

    header = "@everyone\n🔴 **USD IMPORTANT NEWS (ET) — NEXT 7 DAYS**\n"
    if not picked:
        return header + "\n• No USD High/Medium events found in the next 7 days."

    lines = []
    for dt_et, impact, title in picked:
        day = dt_et.strftime("%A")           # Wednesday
        date_txt = dt_et.strftime("%b %-d")  # Feb 18
        time_txt = dt_et.strftime("%-I:%M %p ET")  # 2:00 PM ET
        badge = "🔴" if impact == "High" else "🟠"
        lines.append(f"• {badge} **{day}, {date_txt} — {time_txt}**  — {title}")

    return header + "\n" + "\n".join(lines)


def main():
    webhook = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip()
    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL (GitHub secret).")

    ff_url = os.environ.get("FF_JSON_URL", "").strip() or DEFAULT_FF_JSON_URL

    events = fetch_ff_json(ff_url)
    msg = build_weekly_message(events)
    discord_post(webhook, msg)


if __name__ == "__main__":
    main()
