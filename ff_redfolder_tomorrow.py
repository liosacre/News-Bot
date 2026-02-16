import os
import requests
from datetime import datetime, timedelta, time
from dateutil import parser, tz
import xml.etree.ElementTree as ET

# ---- FEEDS ----
# Your ForexFactory JSON link (THIS WEEK)
FF_JSON_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json?version=022d9054928114f2c2f2b2cadd3e8066"
# Next week JSON (helps cover the next-7-days window near week boundaries)
FF_JSON_NEXTWEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

# XML fallbacks (often more reliable)
FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FF_XML_NEXTWEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"

# ---- TIMEZONES ----
FF_TZ = tz.gettz("America/Chicago")        # FF calendar common default
USER_TZ = tz.gettz("America/Los_Angeles")  # output timezone

MAX_DISCORD_CHARS = 1900  # keep under Discord 2000 char limit

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

def normalize_json_payload(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("events", "data", "calendar", "items"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []

def fetch_events_from_json(url: str):
    r, ct = http_get(url, "application/json,text/plain,*/*")
    if r.status_code != 200:
        print("JSON non-200. BODY PREVIEW:", r.text[:160])
        return None
    if "json" not in ct.lower():
        print("JSON not-json content-type. BODY PREVIEW:", r.text[:160])
        return None
    try:
        payload = r.json()
    except Exception as e:
        print("JSON parse failed:", e)
        print("BODY PREVIEW:", r.text[:160])
        return None
    return normalize_json_payload(payload)

def fetch_events_from_xml(url: str):
    r, _ct = http_get(url, "application/xml,text/xml,*/*")
    if r.status_code != 200:
        print("XML non-200. BODY PREVIEW:", r.text[:160])
        return []
    try:
        root = ET.fromstring(r.text)
    except Exception as e:
        print("XML parse failed:", e)
        print("BODY PREVIEW:", r.text[:160])
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

def get_feed_events(json_url: str, xml_url: str):
    events = fetch_events_from_json(json_url)
    if events is not None:
        return events
    print("Falling back to XML for this feed...")
    return fetch_events_from_xml(xml_url)

def is_high_impact(impact) -> bool:
    s = str(impact or "").strip().lower()
    return s in ("high", "red", "high impact", "3", "highimpact")

def parse_event_dt_ff(e):
    """
    Return (dt_ff, time_known_bool). dt_ff is timezone-aware in FF_TZ.
    If time is missing/tentative, time_known_bool=False and dt_ff becomes 00:00.
    """
    ts = e.get("timestamp") or e.get("ts") or e.get("timeStamp")
    if ts is not None and str(ts).strip().isdigit():
        return datetime.fromtimestamp(int(str(ts).strip()), tz=FF_TZ), True

    date_str = (e.get("date") or e.get("day") or "").strip()
    time_str = (e.get("time") or e.get("datetime") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        d = parser.parse(date_str).date()
        return datetime.combine(d, time(0, 0)).replace(tzinfo=FF_TZ), False

    dt = parser.parse(f"{date_str} {time_str}")
    return dt.replace(tzinfo=FF_TZ), True

def build_weekly_digest(events, start_date_user, end_date_user, webhook):
    rows = []
    for e in events:
        currency = (e.get("currency") or e.get("ccy") or "").strip().upper()
        if currency != "USD":
            continue
        if not is_high_impact(e.get("impact")):
            continue

        title = (e.get("title") or e.get("event") or e.get("name") or "").strip()
        if not title:
            continue

        try:
            dt_ff, time_known = parse_event_dt_ff(e)
        except Exception:
            continue

        dt_user = dt_ff.astimezone(USER_TZ)
        d_user = dt_user.date()

        # include only the next-7-days window
        if not (start_date_user <= d_user < end_date_user):
            continue

        time_txt = dt_user.strftime("%-I:%M %p PT") if time_known else "TBD"
        rows.append((d_user, dt_user, time_txt, title))

    # de-dupe and sort
    seen = set()
    uniq = []
    for d_user, dt_user, time_txt, title in rows:
        key = (d_user.isoformat(), time_txt, title)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((d_user, dt_user, time_txt, title))

    uniq.sort(key=lambda x: (x[0], x[1]))

    header = (
        f"🇺🇸📅 **USD Red Folder (High Impact) — Weekly Digest**\n"
        f"**{start_date_user.strftime('%b %d, %Y')} → {(end_date_user - timedelta(days=1)).strftime('%b %d, %Y')} (PT)**\n"
    )

    if not uniq:
        msg = header + "\n✅ No USD red-folder events found in the next 7 days."
        discord_post(webhook, msg)
        return

    lines = [header]
    current_day = None

    for d_user, _dt_user, time_txt, title in uniq:
        if current_day != d_user:
            current_day = d_user
            lines.append(f"\n**{current_day.strftime('%a, %b %d')}**")
        lines.append(f"- {time_txt} — {title}")

    msg = "\n".join(lines)

    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated) ✅"

    discord_post(webhook, msg)

def main():
    print("✅ ROOT SCRIPT RUNNING (weekly USD red-folder digest)")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    start_user = datetime.now(USER_TZ).date()
    end_user = start_user + timedelta(days=7)

    # pull both week feeds so next-7-days is covered even near week boundaries
    events_this = get_feed_events(FF_JSON_THISWEEK, FF_XML_THISWEEK)
    events_next = get_feed_events(FF_JSON_NEXTWEEK, FF_XML_NEXTWEEK)
    all_events = (events_this or []) + (events_next or [])

    build_weekly_digest(all_events, start_user, end_user, webhook)
    print("Done.")

if __name__ == "__main__":
    main()
