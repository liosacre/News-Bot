import os
import requests
from datetime import datetime, timedelta, time
from dateutil import parser, tz
import xml.etree.ElementTree as ET

FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FF_XML_NEXTWEEK = "https://nfs.faireconomy.media/ff_calendar_nextweek.xml"

FF_TZ = tz.gettz("America/Chicago")
USER_TZ = tz.gettz("America/Los_Angeles")

MAX_DISCORD_CHARS = 1800

# True = include Medium(🟠) + High(🔴). False = High(🔴) only
INCLUDE_MEDIUM = True

def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()

def fetch_xml(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/xml,text/xml,*/*"}
    r = requests.get(url, headers=headers, timeout=30)
    print(f"GET {url} -> {r.status_code} {r.headers.get('content-type','')}")
    r.raise_for_status()
    return r.text

def parse_events_from_xml(xml_text: str):
    root = ET.fromstring(xml_text)
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

def impact_level_emoji(impact_raw: str):
    s = (impact_raw or "").strip().lower()
    if "high" in s or "red" in s:
        return "high", "🔴"
    if "medium" in s or "med" in s or "orange" in s:
        return "medium", "🟠"
    if s.isdigit():
        n = int(s)
        if n >= 3: return "high", "🔴"
        if n == 2: return "medium", "🟠"
    return "low", "🟡"

def parse_dt_ff(e):
    ts = (e.get("timestamp") or "").strip()
    if ts.isdigit():
        n = int(ts)
        if n > 10_000_000_000:
            n //= 1000
        return datetime.fromtimestamp(n, tz=FF_TZ), True

    date_str = (e.get("date") or "").strip()
    time_str = (e.get("time") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        d = parser.parse(date_str).date()
        return datetime.combine(d, time(0, 0)).replace(tzinfo=FF_TZ), False

    dt = parser.parse(f"{date_str} {time_str}")
    return dt.replace(tzinfo=FF_TZ), True

def build_template_b_message(events):
    # We format a fixed Template B message no matter what happens
    lines = []
    lines.append("@everyone")
    lines.append("🔴 **USD IMPORTANT NEWS THIS WEEK (PT)**")
    lines.append("")  # spacer

    # Window: next 7 days in PT (you can change to full week if you want)
    start_pt = datetime.now(USER_TZ).date()
    end_pt = start_pt + timedelta(days=7)

    rows = []
    usd_total = 0

    for e in events:
        ccy = (e.get("currency") or "").strip().upper()
        title = (e.get("title") or "").strip()
        if not title:
            continue

        if ccy == "USD":
            usd_total += 1

        # Filter USD only
        if ccy != "USD":
            continue

        level, emoji = impact_level_emoji(e.get("impact") or "")
        if level == "low":
            continue
        if level == "medium" and not INCLUDE_MEDIUM:
            continue

        try:
            dt_ff, time_known = parse_dt_ff(e)
        except Exception:
            continue

        dt_pt = dt_ff.astimezone(USER_TZ)
        d_pt = dt_pt.date()

        # keep only next 7 days
        if not (start_pt <= d_pt < end_pt):
            continue

        t_txt = dt_pt.strftime("%-I:%M %p") if time_known else "TBD"
        rows.append((d_pt, dt_pt, t_txt, emoji, title))

    # Sort + dedupe
    rows.sort(key=lambda x: (x[0], x[1]))
    dedup = []
    seen = set()
    for d_pt, dt_pt, t_txt, emoji, title in rows:
        key = (d_pt.isoformat(), t_txt, emoji, title)
        if key in seen:
            continue
        seen.add(key)
        dedup.append((d_pt, t_txt, emoji, title))

    if not dedup:
        # ✅ STILL in Template B format
        lines.append("• ✅ No USD high/medium events found in the next 7 days.")
        # Debug ONLY in GitHub logs (not Discord)
        print(f"DEBUG: USD events found in feed (any impact): {usd_total}")
    else:
        current_day = None
        for d_pt, t_txt, emoji, title in dedup:
            if current_day != d_pt:
                current_day = d_pt
                # ✅ Day + readable date (what you asked)
                lines.append("")
                lines.append(f"**{d_pt.strftime('%A').upper()} ({d_pt.strftime('%b %-d')})**")
            lines.append(f"• {t_txt} PT | {emoji} {title}")

    msg = "\n".join(lines)
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated)"
    return msg

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    # Pull both feeds (some weeks spill)
    xml1 = fetch_xml(FF_XML_THISWEEK)
    xml2 = fetch_xml(FF_XML_NEXTWEEK)
    events = parse_events_from_xml(xml1) + parse_events_from_xml(xml2)

    msg = build_template_b_message(events)
    discord_post(webhook, msg)

if __name__ == "__main__":
    main()
