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

MAX_DISCORD_CHARS = 1900

def pick(e, *keys):
    """Return first non-empty field from a dict for the given keys."""
    for k in keys:
        v = e.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s != "":
            return v
    return None

def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},  # allow @everyone ping (if permitted)
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

def is_red_folder(impact) -> bool:
    s = str(impact or "").strip().lower()

    # numeric impact levels (some exports)
    if s.isdigit():
        return int(s) >= 3

    # text variants
    return ("high" in s) or ("red" in s)

def parse_event_dt_ff(e):
    """
    Return (dt_ff, time_known_bool) in FF_TZ.
    Handles timestamp in seconds OR milliseconds.
    """
    ts = pick(e, "timestamp", "ts", "timeStamp", "time_stamp", "timeUnix", "unix", "epoch")
    if ts is not None and str(ts).strip().isdigit():
        n = int(str(ts).strip())
        # milliseconds guard: 13 digits usually
        if n > 10_000_000_000:  # > ~ year 2286 in seconds -> treat as ms
            n = n // 1000
        return datetime.fromtimestamp(n, tz=FF_TZ), True

    date_str = str(pick(e, "date", "day", "eventDate", "event_date") or "").strip()
    time_str = str(pick(e, "time", "datetime", "eventTime", "event_time") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        d = parser.parse(date_str).date()
        return datetime.combine(d, time(0, 0)).replace(tzinfo=FF_TZ), False

    dt = parser.parse(f"{date_str} {time_str}")
    return dt.replace(tzinfo=FF_TZ), True

def build_digest_template_b(events, start_date_pt, end_date_pt, webhook):
    # Template B + date per day header (no confusing range line)
    header = "@everyone\n🔴 USD RED FOLDER THIS WEEK (PT)\n"

    rows = []

    for e in events:
        currency = str(pick(e, "currency", "ccy", "Currency") or "").strip().upper()
        impact = pick(e, "impact", "Impact", "impactTitle", "impact_title", "impactId", "impact_id", "importance")

        title = str(pick(e, "title", "event", "name", "eventName", "event_name") or "").strip()

        if currency != "USD":
            continue
        if not is_red_folder(impact):
            continue
        if not title:
            continue

        try:
            dt_ff, time_known = parse_event_dt_ff(e)
        except Exception:
            continue

        dt_pt = dt_ff.astimezone(USER_TZ)
        d_pt = dt_pt.date()

        if not (start_date_pt <= d_pt < end_date_pt):
            continue

        t_txt = dt_pt.strftime("%-I:%M %p") if time_known else "TBD"
        rows.append((d_pt, dt_pt, t_txt, title))

    # Dedup + sort
    seen = set()
    uniq = []
    for d_pt, dt_pt, t_txt, title in rows:
        key = (d_pt.isoformat(), t_txt, title)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((d_pt, dt_pt, t_txt, title))

    uniq.sort(key=lambda x: (x[0], x[1]))

    if not uniq:
        # Debug info (so we can see what the feed contains)
        sample = []
        for e in (events or [])[:8]:
            c = str(pick(e, "currency", "ccy") or "")
            imp = str(pick(e, "impact", "impactTitle", "impactId") or "")
            nm = str(pick(e, "title", "event", "name") or "")
            sample.append(f"{c}|{imp}|{nm}"[:80])

        msg = header + "\n- No USD red-folder events found in the next 7 days.\n"
        msg += f"\n(debug: fetched {len(events or [])} events; sample: {', '.join(sample)})"
        discord_post(webhook, msg)
        return

    lines = [header]
    current_day = None

    for d_pt, _dt_pt, t_txt, title in uniq:
        if current_day != d_pt:
            current_day = d_pt
            day_header = d_pt.strftime("%A").upper()
            date_label = d_pt.strftime("%b %-d")  # e.g. Feb 18
            lines.append(f"\n{day_header} ({date_label})")
        lines.append(f"- {t_txt} PT | {title}")

    msg = "\n".join(lines)
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated)"

    discord_post(webhook, msg)

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    start_pt = datetime.now(USER_TZ).date()
    end_pt = start_pt + timedelta(days=7)

    events_this = get_feed_events(FF_JSON_THISWEEK, FF_XML_THISWEEK)
    events_next = get_feed_events(FF_JSON_NEXTWEEK, FF_XML_NEXTWEEK)
    all_events = (events_this or []) + (events_next or [])

    build_digest_template_b(all_events, start_pt, end_pt, webhook)

if __name__ == "__main__":
    main()
