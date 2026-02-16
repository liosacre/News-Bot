import os
import sys
import json
import requests
from datetime import datetime, timezone
from dateutil import parser, tz


ET = tz.gettz("America/New_York")


def env_required(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(
            f"Missing {name} env var (GitHub Secret) — set it in repo Settings → Secrets → Actions "
            f"and pass it in your workflow 'env:' block."
        )
    return v.strip()


def fetch_json(url: str) -> list[dict]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    # Some hosts return text/plain even though it's JSON
    text = r.text.strip()
    if not text:
        raise RuntimeError("FF_JSON_URL returned empty response")

    try:
        data = r.json()
    except Exception:
        data = json.loads(text)

    # The FF feed is usually a list; but handle dict wrappers safely.
    if isinstance(data, dict):
        # common wrapper keys
        for k in ("events", "data", "calendar", "items"):
            if k in data and isinstance(data[k], list):
                return data[k]
        # fallback: try dict values
        if all(isinstance(v, dict) for v in data.values()):
            return list(data.values())
        raise RuntimeError("Unexpected JSON structure (dict) from FF feed")

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected JSON structure: {type(data).__name__}")

    return data


def impact_level(ev: dict) -> str:
    # Try common fields seen in FF exports
    raw = (
        ev.get("impact")
        or ev.get("impactTitle")
        or ev.get("impact_title")
        or ev.get("importance")
        or ev.get("volatility")
        or ""
    )

    # numeric impact (sometimes 1/2/3)
    if isinstance(raw, (int, float)):
        if int(raw) >= 3:
            return "high"
        if int(raw) == 2:
            return "medium"
        return "low"

    s = str(raw).strip().lower()
    if any(x in s for x in ("high", "red", "3")):
        return "high"
    if any(x in s for x in ("medium", "med", "orange", "2")):
        return "medium"
    return "low"


def is_usd(ev: dict) -> bool:
    cur = (ev.get("currency") or ev.get("cur") or ev.get("ccy") or "").strip().upper()
    if cur == "USD":
        return True

    # Some exports use "country": "USD" or "United States"
    country = (ev.get("country") or ev.get("nation") or "").strip().upper()
    if country == "USD":
        return True

    return False


def get_title(ev: dict) -> str:
    for k in ("title", "event", "name", "text"):
        v = ev.get(k)
        if v:
            return str(v).strip()
    return "Unnamed event"


def parse_event_dt_et(ev: dict):
    """
    Best-effort, but we prioritize Unix epoch timestamps if present
    (those are the least ambiguous and usually UTC-based).
    """
    # Timestamp fields
    for k in ("timestamp", "ts", "timeStamp", "unix", "time_unix"):
        v = ev.get(k)
        if v is None:
            continue
        try:
            if isinstance(v, str) and v.strip().isdigit():
                v = int(v.strip())
            if isinstance(v, (int, float)) and int(v) > 10_000_000:  # sanity
                dt_utc = datetime.fromtimestamp(int(v), tz=timezone.utc)
                return dt_utc.astimezone(ET)
        except Exception:
            pass

    # ISO datetime field
    for k in ("datetime", "dateTime", "start", "start_time"):
        v = ev.get(k)
        if v:
            try:
                dt = parser.parse(str(v))
                if dt.tzinfo is None:
                    # If no tzinfo, treat as UTC (safer than double-converting ET)
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(ET)
            except Exception:
                pass

    # Fallback: date + time strings
    date_str = ev.get("date") or ev.get("day") or ""
    time_str = ev.get("time") or ev.get("hour") or ""

    if not date_str:
        return None

    # Handle "All Day" / "Tentative"
    t = str(time_str).strip().lower()
    if t in ("all day", "tentative", "tbd", ""):
        try:
            d = parser.parse(str(date_str)).date()
            # represent as noon ET for ordering, but we'll print as TBD
            return datetime(d.year, d.month, d.day, 12, 0, tzinfo=ET)
        except Exception:
            return None

    try:
        dt_guess = parser.parse(f"{date_str} {time_str}")
        if dt_guess.tzinfo is None:
            # If FF gives local-time strings, this is ambiguous.
            # To avoid "double conversion" mistakes, assume the string is already ET.
            dt_guess = dt_guess.replace(tzinfo=ET)
        return dt_guess.astimezone(ET)
    except Exception:
        return None


def fmt_day_header(dt_et: datetime) -> str:
    # Example: WEDNESDAY (Feb 18)
    return dt_et.strftime("%A").upper() + f" ({dt_et.strftime('%b')} {dt_et.day})"


def fmt_time_et(dt_et: datetime, ev: dict) -> str:
    # If original says all day/tentative, show TBD
    t = (ev.get("time") or "").strip().lower()
    if t in ("all day", "tentative", "tbd"):
        return "TBD ET"
    return dt_et.strftime("%-I:%M %p ET") if hasattr(dt_et, "strftime") else "TBD ET"


def build_message(events: list[dict]) -> str:
    usd_events = [e for e in events if is_usd(e)]

    high = []
    high_med = []

    for e in usd_events:
        lvl = impact_level(e)
        dt_et = parse_event_dt_et(e)
        item = (dt_et, e, lvl)
        if lvl == "high":
            high.append(item)
        if lvl in ("high", "medium"):
            high_med.append(item)

    # Sort by datetime, pushing unknown to end
    def key_fn(x):
        dt_et, _, _ = x
        return dt_et if dt_et is not None else datetime.max.replace(tzinfo=ET)

    high.sort(key=key_fn)
    high_med.sort(key=key_fn)

    # TEMPLATE B — single message, ET times, grouped by day
    lines = []
    lines.append("@everyone")
    lines.append("🔴 **USD RED FOLDER THIS WEEK (NEW YORK / ET)**")
    lines.append("")

    if not high:
        lines.append("• No USD **red-folder (High impact)** events found in this week feed.")
    else:
        current_day = None
        for dt_et, ev, _lvl in high:
            if not dt_et:
                continue
            day = dt_et.date()
            if current_day != day:
                current_day = day
                lines.append(f"**{fmt_day_header(dt_et)}**")
            title = get_title(ev)
            lines.append(f"• {fmt_time_et(dt_et, ev)} | {title}")
        lines.append("")

    lines.append("@everyone")
    lines.append("🔴 **USD IMPORTANT NEWS (ET) — THIS WEEK (HIGH + MEDIUM)**")
    lines.append("")

    if not high_med:
        lines.append("• No USD **High/Medium** events found in this week feed.")
    else:
        current_day = None
        for dt_et, ev, _lvl in high_med:
            if not dt_et:
                continue
            day = dt_et.date()
            if current_day != day:
                current_day = day
                lines.append(f"**{fmt_day_header(dt_et)}**")
            title = get_title(ev)
            lines.append(f"• {fmt_time_et(dt_et, ev)} | {title}")

    # Discord hard limit is 2000 chars; keep it safe
    msg = "\n".join(lines).strip()
    if len(msg) > 1900:
        msg = msg[:1900] + "\n…(trimmed)"
    return msg


def discord_post(webhook_url: str, content: str) -> None:
    payload = {
        "content": content,
        "allowed_mentions": {"parse": ["everyone"]},
    }
    r = requests.post(webhook_url, json=payload, timeout=20)
    r.raise_for_status()


def main():
    webhook = env_required("DISCORD_WEBHOOK_URL")
    ff_url = env_required("FF_JSON_URL")

    events = fetch_json(ff_url)
    message = build_message(events)
    discord_post(webhook, message)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
