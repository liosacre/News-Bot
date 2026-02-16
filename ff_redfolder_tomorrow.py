import os
import requests
from datetime import datetime, timedelta
from dateutil import parser, tz
import xml.etree.ElementTree as ET

# Your ForexFactory THIS WEEK JSON export link
FF_JSON_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json?version=022d9054928114f2c2f2b2cadd3e8066"

# Fallback feed (usually more reliable if JSON gets blocked)
FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# FF calendar timezone assumption + your output timezone
FF_TZ = tz.gettz("America/Chicago")
USER_TZ = tz.gettz("America/Los_Angeles")

def discord_post(webhook_url: str, content: str) -> None:
    r = requests.post(webhook_url, json={"content": content}, timeout=20)
    r.raise_for_status()

def http_get(url: str, accept: str):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36",
        "Accept": accept,
    }
    r = requests.get(url, headers=headers, timeout=30)
    ct = r.headers.get("content-type", "")
    print(f"GET {url} -> {r.status_code} {ct}")
    return r, ct

def fetch_json_events():
    """Try JSON feed. Return list[dict] events or None if not usable."""
    r, ct = http_get(FF_JSON_THISWEEK, "application/json,text/plain,*/*")

    if r.status_code != 200:
        print("JSON non-200. BODY PREVIEW:", r.text[:200])
        return None

    if "json" not in ct.lower():
        print("JSON returned non-json content-type. BODY PREVIEW:", r.text[:200])
        return None

    try:
        payload = r.json()
    except Exception as e:
        print("JSON parse failed:", e)
        print("BODY PREVIEW:", r.text[:200])
        return None

    # Normalize payload -> events list
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("events", "data", "calendar", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []

def fetch_xml_events():
    """Fallback XML feed. Return list[dict] events or [] if none."""
    r, ct = http_get(FF_XML_THISWEEK, "application/xml,text/xml,*/*")

    if r.status_code != 200:
        print("XML non-200. BODY PREVIEW:", r.text[:200])
        return []

    # Parse XML
    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        print("XML parse failed:", e)
        print("BODY PREVIEW:", r.text[:200])
        return []

    events = []
    for ev in root.findall(".//event"):
        def get(tag):
            node = ev.find(tag)
            return (node.text or "").strip() if node is not None else ""

        events.append({
            "date": get("date"),
            "time": get("time"),
            "currency": get("currency"),
            "impact": get("impact"),
            "title": get("title") or get("event") or get("name"),
            "timestamp": get("timestamp") or get("ts") or get("timeStamp"),
        })
    return events

def is_high_impact(impact) -> bool:
    s = str(impact or "").strip().lower()
    return s in ("high", "red", "high impact", "3", "highimpact")

def parse_event_dt(e) -> datetime:
    """
    Parse event datetime from either JSON-style dict or our XML dict.
    Returns timezone-aware dt in FF_TZ.
    """
    # timestamp support (if present)
    ts = e.get("timestamp") or e.get("ts") or e.get("timeStamp")
    if ts is not None and str(ts).strip().isdigit():
        return datetime.fromtimestamp(int(str(ts).strip()), tz=FF_TZ)

    date_str = (e.get("date") or e.get("day") or "").strip()
    time_str = (e.get("time") or e.get("datetime") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    if not time_str or time_str.lower() in ("all day", "tentative"):
        return parser.parse(date_str).replace(tzinfo=FF_TZ)

    return parser.parse(f"{date_str} {time_str}").replace(tzinfo=FF_TZ)

def main():
    print("✅ ROOT SCRIPT RUNNING (USD-only, red-folder only)")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    tomorrow = (datetime.now(USER_TZ) + timedelta(days=1)).date()

    # 1) Try JSON
    events = fetch_json_events()

    # 2) Fallback to XML if JSON not usable (blocked/HTML/etc.)
    if events is None:
        print("Falling back to XML feed...")
        events = fetch_xml_events()

    hits = []
    for e in events:
        # USD only
        currency = (e.get("currency") or e.get("ccy") or "").strip().upper()
        if currency != "USD":
            continue

        # high impact only (red folder)
        if not is_high_impact(e.get("impact")):
            continue

        try:
            dt_user = parse_event_dt(e).astimezone(USER_TZ)
        except Exception:
            continue

        if dt_user.date() != tomorrow:
            continue

        title = e.get("title") or e.get("event") or e.get("name") or "Event"
        time_txt = dt_user.strftime("%-I:%M %p PT")
        hits.append((title, time_txt))

    # de-dupe keep order
    hits = list(dict.fromkeys(hits))

    if not hits:
        print("No USD red-folder events found for tomorrow.")
        return  # success, just quiet

    lines = [f"🇺🇸📌 **Tomorrow ({tomorrow.strftime('%b %d, %Y')}) — USD Red Folder (High Impact) events:**"]
    for title, t in hits:
        lines.append(f"- {title} — **{t}**")

    discord_post(webhook, "\n".join(lines))
    print("Posted to Discord.")

if __name__ == "__main__":
    main()
