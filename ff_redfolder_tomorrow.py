import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

OUT_TZ = ZoneInfo("America/New_York")  # New York / ET
TARGET_CCY = "USD"
RED_ONLY = True  # True = High only (red). False = High+Medium

DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK_URL"
FF_JSON_ENV = "FF_JSON_URL"


# ---------------- Discord ----------------
def discord_post(webhook_url: str, content: str) -> None:
    # Split into <=2000 char chunks (Discord limit)
    MAX = 2000
    chunks = []
    while content:
        if len(content) <= MAX:
            chunks.append(content)
            break
        cut = content.rfind("\n", 0, MAX)
        if cut == -1:
            cut = MAX
        chunks.append(content[:cut])
        content = content[cut:].lstrip("\n")

    for ch in chunks:
        r = requests.post(
            webhook_url,
            json={
                "content": ch,
                "allowed_mentions": {"parse": ["everyone"]},  # allow @everyone
            },
            timeout=20,
        )
        r.raise_for_status()


# ---------------- ForexFactory JSON ----------------
def fetch_ff_events(url: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("events", "data", "calendar", "rows", "items"):
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _as_str(x) -> str:
    if x is None:
        return ""
    if isinstance(x, (str, int, float, bool)):
        return str(x)
    if isinstance(x, dict):
        # common nested shapes
        for k in ("code", "value", "text", "name", "currency"):
            if k in x:
                return _as_str(x.get(k))
        return ""
    return ""


def get_currency(e: dict) -> str:
    # handle many possible keys + nested dicts
    for k in ("currency", "cur", "ccy", "currencyCode", "currency_code"):
        if k in e:
            s = _as_str(e.get(k)).strip().upper()
            if s:
                return s
    # sometimes nested like e["country"]["currency"] etc.
    for k in ("country", "meta"):
        if isinstance(e.get(k), dict):
            s = _as_str(e[k].get("currency")).strip().upper()
            if s:
                return s
    return ""


def get_impact(e: dict) -> str:
    raw = (
        e.get("impact")
        or e.get("importance")
        or e.get("volatility")
        or e.get("impactTitle")
        or e.get("impact_title")
    )
    s = _as_str(raw).strip().lower()

    # numeric scales
    if s in ("3", "high", "red") or "high" in s or "red" in s:
        return "High"
    if s in ("2", "medium", "orange") or "medium" in s or "orange" in s:
        return "Medium"
    if s in ("1", "low", "yellow") or "low" in s or "yellow" in s:
        return "Low"
    return "Unknown"


def get_title(e: dict) -> str:
    for k in ("title", "event", "name", "headline"):
        s = _as_str(e.get(k)).strip()
        if s:
            return s
    return "Unknown Event"


def parse_dt_et(e: dict) -> datetime | None:
    """
    KEY FIX:
    ForexFactory exports often include date + time strings that already match the calendar display.
    We treat those as ET directly (America/New_York) to avoid the classic bug:
    "UTC time mistakenly labeled as ET" (your 7:00 PM ET issue).
    """
    date_str = _as_str(e.get("date") or e.get("day") or e.get("event_date")).strip()
    time_str = _as_str(e.get("time") or e.get("event_time")).strip()

    # date often looks like YYYY-MM-DD
    if date_str:
        # If time is like "2:00pm" / "2:00 pm" / "14:00"
        if time_str and time_str.lower() not in ("all day", "tentative", "na", "n/a"):
            t = time_str.lower().replace(" ", "")
            # normalize "2:00pm"
            m = re.match(r"^(\d{1,2}):(\d{2})(am|pm)$", t)
            if m:
                hh = int(m.group(1))
                mm = int(m.group(2))
                ap = m.group(3)
                if ap == "pm" and hh != 12:
                    hh += 12
                if ap == "am" and hh == 12:
                    hh = 0
                try:
                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=OUT_TZ)
                except Exception:
                    pass

            # 24h fallback "14:00"
            m2 = re.match(r"^(\d{1,2}):(\d{2})$", t)
            if m2:
                hh = int(m2.group(1))
                mm = int(m2.group(2))
                try:
                    d = datetime.strptime(date_str, "%Y-%m-%d").date()
                    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=OUT_TZ)
                except Exception:
                    pass

        # if no time, set noon ET
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, 12, 0, tzinfo=OUT_TZ)
        except Exception:
            pass

    # last resort: timestamp epoch (assume UTC then convert)
    for key in ("timestamp", "timeStamp", "ts", "date_timestamp", "event_timestamp"):
        v = _as_str(e.get(key)).strip()
        if v.isdigit():
            n = int(v)
            if n > 10_000_000_000:  # ms -> s
                n //= 1000
            dt_utc = datetime.fromtimestamp(n, tz=ZoneInfo("UTC"))
            return dt_utc.astimezone(OUT_TZ)

    return None


def build_message(events: list[dict]) -> str:
    now = datetime.now(OUT_TZ)
    end = now + timedelta(days=7)

    allowed = {"High"} if RED_ONLY else {"High", "Medium"}

    picked = []
    for e in events:
        if get_currency(e) != TARGET_CCY:
            continue

        impact = get_impact(e)
        if impact not in allowed:
            continue

        dt = parse_dt_et(e)
        if dt is None:
            continue

        if not (now <= dt <= end):
            continue

        picked.append((dt, impact, get_title(e)))

    picked.sort(key=lambda x: x[0])

    header = "@everyone\n🔴 **USD RED FOLDER (HIGH IMPACT) — NEXT 7 DAYS (NEW YORK / ET)**\n"

    if not picked:
        return header + "\n• No USD RED folder (High impact) events found in the next 7 days."

    lines = [header]
    current_date = None

    for dt, impact, title in picked:
        if current_date != dt.date():
            current_date = dt.date()
            lines.append("")
            lines.append(f"**{dt.strftime('%A').upper()} ( {dt.strftime('%b %-d')} )**")

        lines.append(f"• **{dt.strftime('%-I:%M %p ET')}** | {title}")

    return "\n".join(lines).strip()


def main():
    webhook = os.environ.get(DISCORD_WEBHOOK_ENV, "").strip()
    if not webhook:
        raise RuntimeError("Missing DISCORD_WEBHOOK_URL secret")

    ff_url = os.environ.get(FF_JSON_ENV, "").strip()
    if not ff_url:
        raise RuntimeError("Missing FF_JSON_URL secret (your thisweek.json export link)")

    events = fetch_ff_events(ff_url)
    msg = build_message(events)
    discord_post(webhook, msg)


if __name__ == "__main__":
    main()
