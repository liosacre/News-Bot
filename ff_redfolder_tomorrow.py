import os
import requests
from datetime import datetime, timedelta
from dateutil import parser, tz

# ForexFactory "THIS WEEK" Weekly Export JSON URL (your link)
FF_JSON_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json?version=022d9054928114f2c2f2b2cadd3e8066"

# ForexFactory commonly uses America/Chicago as calendar timezone by default.
# The Discord message time below is shown in Pacific Time.
FF_TZ = tz.gettz("America/Chicago")
USER_TZ = tz.gettz("America/Los_Angeles")

def discord_post(webhook_url: str, content: str) -> None:
    r = requests.post(webhook_url, json={"content": content}, timeout=20)
    r.raise_for_status()

def fetch_json(url: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(url, headers=headers, timeout=30)

    # Helpful debug in Actions logs (won't expose your webhook)
    ct = r.headers.get("content-type", "")
    print(f"FETCH -> {r.status_code} {ct}")

    if r.status_code != 200:
        print("BODY PREVIEW:", r.text[:200])
        return {}

    if "json" not in ct.lower():
        # Sometimes a blocked/HTML page comes back
        print("NOT JSON. BODY PREVIEW:", r.text[:200])
        return {}

    try:
        return r.json()
    except Exception as e:
        print("JSON PARSE FAILED:", e)
        print("BODY PREVIEW:", r.text[:200])
        return {}

def as_events(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("events", "data", "calendar", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []

def is_high_impact(impact) -> bool:
    s = str(impact or "").strip().lower()
    # Common encodings across exports
    return s in ("high", "red", "high impact", "3", "highimpact")

def parse_event_dt(e) -> datetime:
    # Timestamp fields (seconds) if present
    for k in ("timestamp", "ts", "timeStamp"):
        v = e.get(k)
        if v is not None and str(v).isdigit():
            return datetime.fromtimestamp(int(v), tz=FF_TZ)

    # Fallback: "date" + "time"
    date_str = e.get("date") or e.get("day") or ""
    time_str = e.get("time") or e.get("datetime") or ""

    if not date_str:
        raise ValueError("missing date")

    if not time_str or str(time_str).lower() in ("all day", "tentative"):
        return parser.parse(str(date_str)).replace(tzinfo=FF_TZ)

    return parser.parse(f"{date_str} {time_str}").replace(tzinfo=FF_TZ)

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    # "1 day before" = events happening tomorrow in your timezone
    now_user = datetime.now(USER_TZ)
    tomorrow = (now_user + timedelta(days=1)).date()

    payload = fetch_json(FF_JSON_THISWEEK)
    events = as_events(payload)

    hits = []
    for e in events:
        # ✅ USD ONLY
        currency = (e.get("currency") or e.get("ccy") or "").strip().upper()
        if currency != "USD":
            continue

        # ✅ Red folder / high impact only
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
        time_txt = dt_user.strftime("%-I:%M %p PT")
        hits.append((title, time_txt))

    # Remove duplicates while keeping order
    hits = list(dict.fromkeys(hits))

    if not hits:
        print("No USD red-folder events found for tomorrow.")
        return

    lines = [f"🇺🇸📌 **Tomorrow ({tomorrow.strftime('%b %d, %Y')}) — USD Red Folder (High Impact) events:**"]
    for title, t in hits:
        lines.append(f"- {title} — **{t}**")

    discord_post(webhook, "\n".join(lines))
    print("Posted to Discord.")

if __name__ == "__main__":
    main()
