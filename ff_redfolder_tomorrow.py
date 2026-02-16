import os
import requests
from datetime import datetime, timedelta, time
from dateutil import parser, tz
import xml.etree.ElementTree as ET

# ---- FEEDS ----
FF_JSON_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.json?version=022d9054928114f2c2f2b2cadd3e8066"
FF_JSON_NEXTWEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FF_XML_NEXTWEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"

# ---- TIMEZONES ----
FF_TZ = tz.gettz("America/Chicago")
USER_TZ = tz.gettz("America/Los_Angeles")

MAX_DISCORD_CHARS = 1900  # keep under Discord 2000 char limit

def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},  # allow @everyone ping
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()

def http_get(url: str, accept: str):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": accept}
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

def impact_label(impact) -> str:
    # Make the “red folder” explicit in the message
    if is_high_impact(impact):
        return "🔴 Red (High Impact)"
    return "Impact"

def parse_event_dt_ff(e):
    """
    Return (dt_ff, time_known_bool). dt_ff timezone-aware in FF_TZ.
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

        impact = e.get("impact")
        if not is_high_impact(impact):
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

        if not (start_date_user <= d_user < end_date_user):
            continue

        t_txt = dt_user.strftime("%-I:%M %p PT") if time_known else "TBD"
        rows.append((d_user, dt_user, t_txt, title, impact_label(impact)))

    # de-dupe and sort
    seen = set()
    uniq = []
    for d_user, dt_user, t_txt, title, imp_lbl in rows:
        key = (d_user.isoformat(), t_txt, title, imp_lbl)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((d_user, dt_user, t_txt, title, imp_lbl))

    uniq.sort(key=lambda x: (x[0], x[1]))

    start_day_name = start_date_user.strftime("%A")
    end_day_name = (end_date_user - timedelta(days=1)).strftime("%A")

    header = (
        "@everyone\n"
        "🇺🇸📅 **USD Red Folder — Weekly Digest**\n"
        f"**{start_day_name} → {end_day_name} (PT)**\n"
    )

    if not uniq:
        msg = header + "\n✅ No USD red-folder events found in the next 7 days."
        discord_post(webhook, msg)
        return

    lines = [header]
    current_day = None

    for d_user, _dt_user, t_txt, title, imp_lbl in uniq:
        day_name = d_user.strftime("%A")
        if current_day != d_user:
            current_day = d_user
            lines.append(f"\n**{day_name}**")
        # Now explicitly shows the “red folder name/label” + the event name (title)
        lines.append(f"- {t_txt} — {imp_lbl} — **{title}**")

    msg = "\n".join(lines)
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated) ✅"

    discord_post(webhook, msg)

def main():
    print("✅ ROOT SCRIPT RUNNING (weekly USD red-folder digest + label + @everyone)")

    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    start_user = datetime.now(USER_TZ).date()
    end_user = start_user + timedelta(days=7)

    events_this = get_feed_events(FF_JSON_THISWEEK, FF_XML_THISWEEK)
    events_next = get_feed_events(FF_JSON_NEXTWEEK, FF_XML_NEXTWEEK)
    all_events = (events_this or []) + (events_next or [])

    build_weekly_digest(all_events, start_user, end_user, webhook)
    print("Done.")

if __name__ == "__main__":
    main()
