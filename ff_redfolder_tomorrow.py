import os
import requests
from datetime import datetime, time
from dateutil import parser, tz
import xml.etree.ElementTree as ET

# --- FEED (use ONLY thisweek; nextweek was 404 in your logs) ---
FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# --- TIMEZONES ---
FF_TZ = tz.gettz("America/Chicago")        # feed timezone (commonly)
ET_TZ = tz.gettz("America/New_York")       # New York time (ET)

MAX_DISCORD_CHARS = 1800

# True = include Medium(🟠) + High(🔴). False = High(🔴) only (true red folder)
INCLUDE_MEDIUM = False  # set True if you want orange folders too


def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},  # allow @everyone ping (if server permits)
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
    """
    Parse XML robustly:
    Reads values from either child tags (<currency>USD</currency>) OR attributes (currency="USD")
    Works even if the XML is namespaced.
    """
    root = ET.fromstring(xml_text)
    events = []

    def get_text_or_attr(node: ET.Element, keys):
        # child tags
        for k in keys:
            child = node.find(k)
            if child is not None and child.text:
                v = child.text.strip()
                if v:
                    return v
        # attributes
        for k in keys:
            v = (node.attrib.get(k) or "").strip()
            if v:
                return v
        return ""

    for node in root.iter():
        # namespace-safe check: tag may look like "{ns}event"
        if str(node.tag).lower().endswith("event"):
            events.append({
                "currency": get_text_or_attr(node, ["currency", "country", "ccy"]),
                "impact": get_text_or_attr(node, ["impact", "importance"]),
                "title": get_text_or_attr(node, ["title", "event", "name"]),
                "date": get_text_or_attr(node, ["date", "day"]),
                "time": get_text_or_attr(node, ["time", "datetime"]),
                "timestamp": get_text_or_attr(node, ["timestamp", "ts", "timeStamp", "time_stamp"]),
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
        if n >= 3:
            return "high", "🔴"
        if n == 2:
            return "medium", "🟠"
    return "low", "🟡"


def parse_event_dt_ff(e):
    """
    Return (dt_ff, time_known_bool) in FF_TZ.
    Handles timestamp in seconds OR milliseconds.
    """
    ts = (e.get("timestamp") or "").strip()
    if ts.isdigit():
        n = int(ts)
        if n > 10_000_000_000:  # ms guard
            n //= 1000
        return datetime.fromtimestamp(n, tz=FF_TZ), True

    date_str = (e.get("date") or "").strip()
    time_str = (e.get("time") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        d = parser.parse(date_str).date()
        dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=FF_TZ)
        return dt, False

    dt = parser.parse(f"{date_str} {time_str}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FF_TZ)
    return dt, True


def build_template_b_message(events):
    """
    Template B style, but improved:
    - @everyone
    - Title line
    - Day headers in ALL CAPS + (Mon DD)
    - Lines: "- 8:30 AM ET | CPI m/m"
    - Uses THISWEEK feed contents (no "Sunday → Saturday" range line)
    """
    lines = []
    lines.append("@everyone")
    lines.append("🔴 USD RED FOLDER THIS WEEK (ET)")
    lines.append("")

    rows = []
    usd_any = 0
    usd_red = 0

    for e in events:
        ccy = (e.get("currency") or "").strip().upper()
        title = (e.get("title") or "").strip()
        if ccy == "USD":
            usd_any += 1

        if ccy != "USD" or not title:
            continue

        level, _emoji = impact_level_emoji(e.get("impact") or "")
        if level == "low":
            continue
        if level == "medium" and not INCLUDE_MEDIUM:
            continue

        try:
            dt_ff, time_known = parse_event_dt_ff(e)
        except Exception:
            continue

        dt_et = dt_ff.astimezone(ET_TZ)
        t_txt = dt_et.strftime("%-I:%M %p") if time_known else "TBD"

        rows.append((dt_et, t_txt, title, level))
        if level == "high":
            usd_red += 1

    # Sort + dedupe
    rows.sort(key=lambda x: x[0])
    seen = set()
    dedup = []
    for dt_et, t_txt, title, level in rows:
        key = (dt_et.date().isoformat(), t_txt, title, level)
        if key in seen:
            continue
        seen.add(key)
        dedup.append((dt_et, t_txt, title, level))

    if not dedup:
        # Still template formatted
        want = "High+Medium" if INCLUDE_MEDIUM else "High only"
        lines.append(f"- No USD {'high/medium' if INCLUDE_MEDIUM else 'red-folder (high)'} events found in THISWEEK feed. ({want})")
    else:
        current_day = None
        for dt_et, t_txt, title, _level in dedup:
            if current_day != dt_et.date():
                current_day = dt_et.date()
                # Example: WEDNESDAY (Feb 19)
                lines.append("")
                lines.append(f"{dt_et.strftime('%A').upper()} ({dt_et.strftime('%b %-d')})")
            lines.append(f"- {t_txt} ET | {title}")

    # Debug to Actions logs only
    print(f"DEBUG: parsed_events={len(events)} | usd_any={usd_any} | usd_high={usd_red}")

    msg = "\n".join(lines).strip()
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated)"
    return msg


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    xml_text = fetch_xml(FF_XML_THISWEEK)
    events = parse_events_from_xml(xml_text)

    msg = build_template_b_message(events)
    discord_post(webhook, msg)


if __name__ == "__main__":
    main()
