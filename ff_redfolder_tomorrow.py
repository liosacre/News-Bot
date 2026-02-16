import os
import requests
from datetime import datetime, time
from dateutil import parser, tz
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

# --- Use THIS WEEK XML only (most consistent + includes currency) ---
FF_XML_THISWEEK = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# --- TIMEZONES ---
FF_TZ = tz.gettz("America/Chicago")        # feed timezone (usually)
USER_TZ = tz.gettz("America/Los_Angeles")  # output in PT

MAX_DISCORD_CHARS = 1900

# Set True if you want Medium+High. Set False for High-only (true red folder).
INCLUDE_MEDIUM = True

def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()

def http_get(url: str, accept: str):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": accept}
    r = requests.get(url, headers=headers, timeout=30)
    ct = r.headers.get("content-type", "")
    print(f"GET {url} -> {r.status_code} {ct}")
    return r

def fetch_events_from_xml(url: str):
    r = http_get(url, "application/xml,text/xml,*/*")
    if r.status_code != 200:
        raise RuntimeError(f"XML non-200: {r.status_code}")

    root = ET.fromstring(r.text)

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

def impact_level_and_emoji(impact_raw: str):
    s = (impact_raw or "").strip().lower()
    # common strings
    if "high" in s or "red" in s:
        return "high", "🔴"
    if "medium" in s or "med" in s or "orange" in s:
        return "medium", "🟠"
    if "low" in s:
        return "low", "🟡"
    # numeric fallback
    if s.isdigit():
        n = int(s)
        if n >= 3:
            return "high", "🔴"
        if n == 2:
            return "medium", "🟠"
        return "low", "🟡"
    return "low", "🟡"

def parse_event_dt_ff(e):
    """
    Return (dt_ff, time_known_bool) in FF_TZ.
    Handles timestamp seconds OR milliseconds.
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
        return datetime.combine(d, time(0, 0)).replace(tzinfo=FF_TZ), False

    dt = parser.parse(f"{date_str} {time_str}")
    return dt.replace(tzinfo=FF_TZ), True

def build_week_message_from_thisweek_xml(events, webhook):
    # Filter USD first (no date window — we trust "THIS WEEK" feed)
    usd_events = []
    for e in events:
        ccy = (e.get("currency") or "").strip().upper()
        title = (e.get("title") or "").strip()
        if ccy != "USD" or not title:
            continue
        usd_events.append(e)

    # Impact stats for debugging if needed
    impact_counts = Counter()
    for e in usd_events:
        level, _emoji = impact_level_and_emoji(e.get("impact") or "")
        impact_counts[level] += 1

    rows = []
    for e in usd_events:
        level, emoji = impact_level_and_emoji(e.get("impact") or "")
        if level == "low":
            continue
        if level == "medium" and not INCLUDE_MEDIUM:
            continue

        try:
            dt_ff, time_known = parse_event_dt_ff(e)
        except Exception:
            continue

        dt_pt = dt_ff.astimezone(USER_TZ)
        d_pt = dt_pt.date()

        t_txt = dt_pt.strftime("%-I:%M %p") if time_known else "TBD"
        rows.append((d_pt, dt_pt, t_txt, emoji, (e.get("title") or "").strip()))

    # dedupe + sort
    seen = set()
    uniq = []
    for d_pt, dt_pt, t_txt, emoji, title in rows:
        key = (d_pt.isoformat(), t_txt, emoji, title)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((d_pt, dt_pt, t_txt, emoji, title))
    uniq.sort(key=lambda x: (x[0], x[1]))

    header = "@everyone\n🔴 USD IMPORTANT NEWS THIS WEEK (PT)\n"

    if not uniq:
        # Show very clear debug so we know what the feed says for USD events
        # (This will tell us if FF is labeling your “red folder” differently.)
        dbg = (
            f"(debug: USD events in thisweek feed = {len(usd_events)} | "
            f"high={impact_counts.get('high',0)}, medium={impact_counts.get('medium',0)}, low={impact_counts.get('low',0)})"
        )
        suffix = "(High+Medium)" if INCLUDE_MEDIUM else "(High only)"
        msg = header + f"\n- No USD high/medium events found in THIS WEEK feed. {suffix}\n{dbg}"
        discord_post(webhook, msg)
        return

    lines = [header]
    current_day = None
    for d_pt, _dt_pt, t_txt, emoji, title in uniq:
        if current_day != d_pt:
            current_day = d_pt
            # ✅ this is what you wanted: DAY + DATE, not “Sunday -> Saturday”
            day_header = d_pt.strftime("%A").upper()
            date_label = d_pt.strftime("%b %-d")  # e.g. Feb 19
            lines.append(f"\n{day_header} ({date_label})")
        lines.append(f"- {t_txt} PT | {emoji} {title}")

    msg = "\n".join(lines)
    if len(msg) > MAX_DISCORD_CHARS:
        msg = msg[:MAX_DISCORD_CHARS] + "\n…(truncated)"

    discord_post(webhook, msg)

def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook:
        raise SystemExit("Missing DISCORD_WEBHOOK_URL secret/env var")

    events = fetch_events_from_xml(FF_XML_THISWEEK)
    build_week_message_from_thisweek_xml(events, webhook)

if __name__ == "__main__":
    main()
