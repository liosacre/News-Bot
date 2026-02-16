import os
import requests
from datetime import datetime, timedelta
from dateutil import parser, tz

# ForexFactory weekly export JSON (this week + next week)
FF_JSON_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_JSON_NEXTWEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

# ForexFactory is commonly set to America/Chicago by default.
# Your times will be posted in Pacific in the Discord message below.
FF_TZ = tz.gettz("America/Chicago")
USER_TZ = tz.gettz("America/Los_Angeles")

def discord_post(webhook_url: str, content: str) -> None:
    r = requests.post(webhook_url, json={"content": content}, timeout=20)
    r.raise_for_status()

def as_events(payload):
    if isinstance(payload, list):
        return payload
    for k in ("events", "data", "calendar", "items"):
        v = payload.get(k)
        if isinstance(v, list):
            return v
    return []

def is_high_impact(impact):
    if impact is None:
        return False
    s = str(impact).strip().lower()
    # common encodings across exports
    return s in ("high", "red", "high impact", "3", "highimpact")

def parse_event_dt(e):
    # Prefer timestamp fields if present (seconds)
    for k in ("timestamp", "ts", "timeStamp"):
        v = e.get(k)
        if v is not None and str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=FF_TZ)

    # Fallback: date + time strings
    date_str = e.get("date") or e.get("day") or ""
    time_str = e.get("time") or e.get("datetime") or ""

    if not date_str:
        raise ValueError("missing date")

    # Handle all-day/tentative
    if not time_str or str(time_str).lower() in ("all day", "tentative"):
        return parser.parse(str(date_str)).replace(tzinfo=FF_TZ)

    return parser.parse(f"{date_str} {time_str}").replace(tzinfo=FF_TZ)

def fetch(url):
    return requests.get(url, timeout=30).json()

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    now = datetime.now(USER_TZ)
    tomorrow = (now + timedelta(days=1)).date()

    events = []
    for payload in (fetch(FF_JSON_THISWEEK), fetch(FF_JSON_NEXTWEEK)):
        events.extend(as_events(payload))

    hits = []
    for e in events:
        if not is_high_impact(e.get("impact")):
            continue

        try:
            dt_ff = parse_event_dt(e)
        except Exception:
            continue

        dt_user = dt_ff.astimezone(USER_TZ)
        if dt_user.date() != tomorrow:
            continue

        title = e.get("title") or e.get("event") or e.get("name") or "Event"
        currency = e.get("currency") or e.get("ccy") or ""
        time_txt = dt_user.strftime("%-I:%M %p PT")
        hits.append((currency, title, time_txt))

    # remove duplicates while keeping order
    hits = list(dict.fromkeys(hits))

    if not hits:
        return  # nothing tomorrow, stay quiet

    lines = [f"📌 **Tomorrow ({tomorrow.strftime('%b %d, %Y')}) — Red Folder (High Impact) events:**"]
    for currency, title, t in hits:
        prefix = f"**{currency}** — " if currency else ""
        lines.append(f"- {prefix}{title} — **{t}**")

    discord_post(webhook, "\n".join(lines))

if __name__ == "__main__":
    main()
