import os
import requests
from datetime import datetime
from dateutil import parser, tz
import xml.etree.ElementTree as ET

FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

UTC_TZ = tz.gettz("UTC")
NY_TZ = tz.gettz("America/New_York")

MAX_DISCORD_CHARS = 1800

# False = HIGH only (true red folder). True = include MEDIUM too.
INCLUDE_MEDIUM = False


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


def get_text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def impact_level(impact_raw: str) -> str:
    s = (impact_raw or "").strip().lower()
    if "high" in s or "red" in s:
        return "high"
    if "medium" in s or "med" in s or "orange" in s:
        return "medium"
    if s.isdigit():
        n = int(s)
        if n >= 3:
            return "high"
        if n == 2:
            return "medium"
    return "low"


def parse_event_datetime_ny(ev: dict):
    """
    Returns (dt_ny, time_known_bool)
    Prefer Unix timestamp (UTC) then convert -> NY time.
    """
    ts = (ev.get("timestamp") or "").strip()
    if ts.isdigit():
        n = int(ts)
        if n > 10_000_000_000:  # ms guard
            n //= 1000
        dt_utc = datetime.fromtimestamp(n, tz=UTC_TZ)
        return dt_utc.astimezone(NY_TZ), True

    date_str = (ev.get("date") or "").strip()
    time_str = (ev.get("time") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        d = parser.parse(date_str).date()
        dt_ny = datetime(d.year, d.month, d.day, 0, 0, tzinfo=NY_TZ)
        return dt_ny, False

    dt = parser.parse(f"{date_str} {time_str}")
    # If timezone missing, assume NY time
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY_TZ)
    else:
        dt = dt.astimezone(NY_TZ)
    return dt, True


def parse_events(xml_text: str):
    root = ET.fromstring(xml_text)
    events = []
    for node in root.findall(".//event"):
        # ForexFactory often uses <country>USD</country>
        country = get_text(node, "country")
        currency = get_text(node, "currency")

        events.append({
            "title": get_text(node, "title") or get_text(node, "event") or get_text(node, "name"),
            "ccy": (country or currency).strip().upper(),
            "impact": get_text(node, "impact"),
            "date": get_text(node, "date"),
            "time": get_text(node, "time"),
            "timestamp": get_text(node, "timestamp") or get_text(node, "ts") or get_text(node, "timeStamp"),
        })
    return events


def build_template_b_message(events):
    lines = []
    lines.append("@everyone")
    lines.append("🔴 USD RED FOLDER THIS WEEK (NEW YORK / ET)")
    lines.append("")

    rows = []
    usd_any = 0
    usd_high = 0
    usd_medium = 0

    for ev in events:
        if ev["ccy"] == "USD":
            usd_any += 1

        if ev["ccy"] != "USD":
            continue

        lvl = impact_level(ev.get("impact", ""))
        if lvl == "low":
            continue
        if lvl == "medium" and not INCLUDE_MEDIUM:
            continue

        title = (ev.get("title") or "").strip()
        if not title:
            continue

        try:
            dt_ny, time_known = parse_event_datetime_ny(ev)
        except Exception:
            continue

        t_txt = dt_ny.strftime("%-I:%M %p") if time_known else "TBD"
        rows.append((dt_ny, t_txt, title, lvl))

        if lvl == "high":
            usd_high += 1
        elif lvl == "medium":
            usd_medium += 1

    rows.sort(key=lambda x: x[0])

    # Dedup
    seen = set()
    dedup = []
    for dt_ny, t_txt, title, lvl in rows:
        key = (dt_ny.date().isoformat(), t_txt, title, lvl)
        if key in seen:
            continue
        seen.add(key)
        dedup.append((dt_ny, t_txt, title, lvl))

    if not dedup:
        lines.append("- No USD red-folder (HIGH) events found in THISWEEK feed.")
    else:
        current_day = None
        for dt_ny, t_txt, title, _lvl in dedup:
            if current_day != dt_ny.date():
                current_day = dt_ny.date()
                lines.append("")
                lines.append(f"{dt_ny.strftime('%A').upper()} ({dt_ny.strftime('%b %-d')})")
            lines.append(f"- {t_txt} ET | {title}")

    # Shows in GitHub Actions logs so you can confirm it’s reading USD & impacts
    print(
        f"DEBUG: parsed_events={len(events)} | usd_any={usd_any} | usd_high={usd_high} | usd_medium={usd_medium}"
    )

    msg = "\n".join(lines).strip()
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated)"
    return msg


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    xml_text = fetch_xml(FF_XML_THISWEEK)
    events = parse_events(xml_text)

    msg = build_template_b_message(events)
    discord_post(webhook, msg)


if __name__ == "__main__":
    main()
