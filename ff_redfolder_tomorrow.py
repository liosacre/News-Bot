import os
import requests
from datetime import datetime
from dateutil import parser, tz
import xml.etree.ElementTree as ET

FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Feed is commonly Chicago time; we display in PT
FF_TZ = tz.gettz("America/Chicago")
PT_TZ = tz.gettz("America/Los_Angeles")

MAX_DISCORD_CHARS = 1800


def discord_post(webhook_url: str, content: str) -> None:
    r = requests.post(
        webhook_url,
        json={
            "content": content,
            "allowed_mentions": {"parse": ["everyone"]},  # allow @everyone
        },
        timeout=20,
    )
    r.raise_for_status()


def fetch_text(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/xml,text/xml,*/*"}
    r = requests.get(url, headers=headers, timeout=30)
    print(f"GET {url} -> {r.status_code} {r.headers.get('content-type','')}")
    r.raise_for_status()
    return r.text


def _get_text_or_attr(node: ET.Element, candidates):
    """
    Read a value from:
    - child tag text (e.g. <currency>USD</currency>)
    - OR attribute (e.g. <event currency="USD">)
    Returns '' if not found.
    """
    # Try tags first
    for key in candidates:
        child = node.find(key)
        if child is not None and child.text:
            val = child.text.strip()
            if val:
                return val

    # Then try attributes
    for key in candidates:
        val = (node.attrib.get(key) or "").strip()
        if val:
            return val

    return ""


def parse_events(xml_text: str):
    root = ET.fromstring(xml_text)

    # Works for both namespaced and non-namespaced XML
    events = []
    for ev in root.iter():
        tag = ev.tag.lower()
        if tag.endswith("event"):
            # currency sometimes appears as <currency> or <country> or attribute
            currency = _get_text_or_attr(ev, ["currency", "country", "ccy"])
            impact = _get_text_or_attr(ev, ["impact", "importance"])
            title = _get_text_or_attr(ev, ["title", "event", "name"])
            date_str = _get_text_or_attr(ev, ["date", "day"])
            time_str = _get_text_or_attr(ev, ["time", "datetime"])
            timestamp = _get_text_or_attr(ev, ["timestamp", "ts", "timeStamp"])

            events.append(
                {
                    "currency": currency,
                    "impact": impact,
                    "title": title,
                    "date": date_str,
                    "time": time_str,
                    "timestamp": timestamp,
                }
            )

    return events


def impact_is_red(impact_raw: str) -> bool:
    s = (impact_raw or "").strip().lower()
    # ForexFactory feeds vary: "High", "high", "3", "red"
    if "high" in s or "red" in s:
        return True
    if s.isdigit() and int(s) >= 3:
        return True
    return False


def parse_event_dt(e):
    """
    Returns (dt_ff, time_known)
    dt_ff is timezone-aware in FF_TZ
    """
    ts = (e.get("timestamp") or "").strip()
    if ts.isdigit():
        n = int(ts)
        if n > 10_000_000_000:  # milliseconds -> seconds
            n //= 1000
        return datetime.fromtimestamp(n, tz=FF_TZ), True

    date_str = (e.get("date") or "").strip()
    time_str = (e.get("time") or "").strip()

    if not date_str:
        raise ValueError("missing date")

    # Some feeds use "Tentative", "All Day", or blank
    if not time_str or time_str.lower() in ("all day", "tentative", "tbd"):
        d = parser.parse(date_str).date()
        dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=FF_TZ)
        return dt, False

    dt = parser.parse(f"{date_str} {time_str}")
    # If parser returns naive, attach feed tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FF_TZ)
    return dt, True


def build_message(events):
    # Filter: USD + RED only + has title
    filtered = []
    for e in events:
        ccy = (e.get("currency") or "").strip().upper()
        title = (e.get("title") or "").strip()
        if ccy != "USD":
            continue
        if not title:
            continue
        if not impact_is_red(e.get("impact") or ""):
            continue

        try:
            dt_ff, time_known = parse_event_dt(e)
        except Exception:
            continue

        dt_pt = dt_ff.astimezone(PT_TZ)
        filtered.append((dt_pt, time_known, title))

    filtered.sort(key=lambda x: x[0])

    lines = []
    lines.append("@everyone")
    lines.append("🔴 **USD RED FOLDER EVENTS THIS WEEK (PT)**")
    lines.append("")

    if not filtered:
        lines.append("• ✅ No USD red-folder (High impact) events found in the THISWEEK feed.")
    else:
        current_day = None
        for dt_pt, time_known, title in filtered:
            day_key = dt_pt.date()
            if current_day != day_key:
                current_day = day_key
                # Example: WEDNESDAY, Feb 18
                lines.append(f"**{dt_pt.strftime('%A').upper()}, {dt_pt.strftime('%b %-d')}**")
            t_txt = dt_pt.strftime("%-I:%M %p") if time_known else "TBD"
            lines.append(f"• {t_txt} PT — {title}")
            lines.append("")

    msg = "\n".join(lines).strip()
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated)"
    return msg


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    xml_text = fetch_text(FF_XML_THISWEEK)
    events = parse_events(xml_text)

    # Useful debug in Actions logs (NOT Discord)
    usd_any = sum(1 for e in events if (e.get("currency") or "").strip().upper() == "USD")
    red_any = sum(1 for e in events if impact_is_red(e.get("impact") or ""))
    print(f"DEBUG: parsed_events={len(events)} | usd_any={usd_any} | red_any={red_any}")

    msg = build_message(events)
    discord_post(webhook, msg)


if __name__ == "__main__":
    main()
