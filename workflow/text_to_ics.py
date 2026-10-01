#!/usr/bin/env python3
import warnings
warnings.filterwarnings("ignore", message="urllib3 v2 only supports")

import os
import sys
import json
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone

import urllib.error
import urllib.request
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

API_URL = "https://api.openai.com/v1/chat/completions"
API_KEY_FILE = os.path.expanduser("~/.config/openai-key")


def _system_timezone():
    """The Mac's current zone, e.g. Europe/Berlin. Falls back to UTC."""
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
    except (IndexError, OSError):
        return "UTC"


def _vtimezone(tz_name):
    """A VTIMEZONE block for tz_name, from the zone's real offsets.

    Calendar clients need the offsets spelled out. Rather than hardcode one
    region's rules, read this year's standard and daylight offsets from the
    zone itself. Zones without daylight saving get a single STANDARD block.
    """
    zone = ZoneInfo(tz_name)
    year = datetime.now().year
    offsets = {}
    for month in range(1, 13):
        probe = datetime(year, month, 15, 12, 0, tzinfo=zone)
        key = (probe.utcoffset(), probe.dst() or timedelta(0), probe.tzname())
        offsets.setdefault(key, probe)

    std = min(offsets, key=lambda k: k[1])
    dst = max(offsets, key=lambda k: k[1])

    def fmt(delta):
        total = int(delta.total_seconds())
        sign = "+" if total >= 0 else "-"
        total = abs(total)
        return f"{sign}{total // 3600:02d}{(total % 3600) // 60:02d}"

    lines = [f"BEGIN:VTIMEZONE", f"TZID:{tz_name}"]
    if dst[1]:
        lines += [
            "BEGIN:DAYLIGHT",
            f"TZOFFSETFROM:{fmt(std[0])}",
            f"TZOFFSETTO:{fmt(dst[0])}",
            f"TZNAME:{dst[2]}",
            f"DTSTART:{offsets[dst].strftime('%Y%m%dT%H%M%S')}",
            "END:DAYLIGHT",
        ]
    lines += [
        "BEGIN:STANDARD",
        f"TZOFFSETFROM:{fmt(dst[0])}",
        f"TZOFFSETTO:{fmt(std[0])}",
        f"TZNAME:{std[2]}",
        f"DTSTART:{offsets[std].strftime('%Y%m%dT%H%M%S')}",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]
    # Plain newlines: fold_line splits on them and re-joins with CRLF.
    return "\n".join(lines)


MODEL = os.environ.get("TEXT_TO_ICS_MODEL", "").strip() or "gpt-4.1"
DEFAULT_DURATION_HOURS = 2
# Fallback zone for events whose location implies nothing. Override with
# TEXT_TO_ICS_TZ, or leave it to follow the Mac's own setting. Every zone in use
# gets a VTIMEZONE block built from its real transition rules, so this is
# correct outside Central Europe too.
TZ = os.environ.get("TEXT_TO_ICS_TZ") or _system_timezone()


EVENTS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "events",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["events"],
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "summary", "date", "end_date", "start_time", "end_time", "timezone",
                            "location", "description", "url", "organizer", "categories",
                        ],
                        "properties": {
                            "summary": {"type": "string"},
                            "date": {"type": "string", "description": "YYYY-MM-DD"},
                            "end_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, last day of a span"},
                            "start_time": {"type": ["string", "null"], "description": "HH:MM 24h"},
                            "end_time": {"type": ["string", "null"], "description": "HH:MM 24h"},
                            "timezone": {"type": ["string", "null"], "description": "IANA zone, e.g. Asia/Bangkok"},
                            "location": {"type": ["string", "null"]},
                            "description": {"type": ["string", "null"]},
                            "url": {"type": ["string", "null"]},
                            "organizer": {"type": ["string", "null"]},
                            "categories": {
                                "type": ["array", "null"],
                                "items": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    },
}

SYSTEM_PROMPT_TEMPLATE = """\
You extract calendar events from text snippets. Parse as many details as possible.
Return valid JSON only.
Schema:
{{
  "events": [
    {{
      "summary": "Event title",
      "date": "YYYY-MM-DD",
      "end_date": "YYYY-MM-DD" or null,
      "start_time": "HH:MM" or null,
      "end_time": "HH:MM" or null,
      "timezone": "IANA timezone of the location" or null,
      "location": "Full address if available, otherwise venue/city" or null,
      "description": "All additional details" or null,
      "url": "URL if found in text" or null,
      "organizer": "Organizer/host name" or null,
      "categories": ["category1"] or null
    }}
  ]
}}
Date anchor:
- Today is {today} ({weekday}). The current year is {year}.
- Resolve all relative dates ("tomorrow", "next Friday", bare "June 5") against this.
- NEVER output a year other than {year} or {next_year} unless the source text states one explicitly.
- If a bare month/day has already passed this year, use {next_year}.
Rules:
- Extract ALL events found in the text
- Use 24-hour time format
- If no specific start time, set start_time to null
- Set end_date only for a continuous span (trip, stay, holiday, multi-day fair)
- If hours repeat on each day of a span, emit one event per day and no end_date
- Times are always the LOCAL wall-clock time at the event's location
- Set timezone to the IANA identifier implied by the location (e.g. a Bangkok
  hotel booking -> "Asia/Bangkok"), but only when the location makes it
  unambiguous; otherwise null
- Parse as much detail as possible into the description: prices, ticket info,
  seat/section info, booking references, performer names, notes, conditions
- Prefer full addresses for location (street, zip, city, country) when available
- Keep the original language for event names and locations
- If there is a combined/group price, note it in each event's description"""




def fail(message, detail=""):
    """Report and stop.

    The message goes to stdout because the Alfred notification only shows
    stdout; anything on stderr is invisible outside the debug console. The
    server's response body is detail, and stays on stderr so the notification
    keeps to one readable line.

    The notification alone is too easy to miss: a banner is gone in seconds and
    Focus suppresses it entirely. So the message also goes to an alert that
    stays until dismissed.
    """
    print(message)
    if detail:
        print(detail, file=sys.stderr)
    alert(message)
    raise SystemExit(1)


def alert(message):
    """Best effort; a failed alert must not hide the error being reported."""
    script = (
        "on run argv\n"
        "  tell me to activate\n"
        "  display alert (item 1 of argv) message (item 2 of argv) as critical giving up after 120\n"
        "end run"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script, "Text to ICS failed", message],
            capture_output=True,
            timeout=130,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def load_api_key():
    """Environment first, then the key file. Neither is a fatal surprise."""
    key = os.environ.get("OPENAI_API_KEY")
    if key and key.strip():
        return key.strip()
    try:
        with open(API_KEY_FILE, "r") as f:
            key = f.read().strip()
    except FileNotFoundError:
        fail(
            "No API key. Set OPENAI_API_KEY in the workflow configuration, "
            f"or put the key in {API_KEY_FILE}."
        )
    if not key:
        fail(f"{API_KEY_FILE} is empty.")
    return key


def get_input_text():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    result = subprocess.run(["pbpaste"], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    fail("No input text")


def extract_events(text, api_key):
    now = datetime.now()
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        today=now.strftime("%Y-%m-%d"),
        weekday=now.strftime("%A"),
        year=now.year,
        next_year=now.year + 1,
    )
    payload = {
        "model": MODEL,
        "response_format": EVENTS_RESPONSE_FORMAT,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.0,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", "replace")
        detail = error_body[:300]
        if exc.code == 401:
            fail("OpenAI rejected the key (401). Check it in the workflow configuration.", detail)
        if exc.code == 429 and ("insufficient_quota" in error_body or "credit_balance_exhausted" in error_body):
            # Same status as a rate limit, but waiting will not help.
            fail("OpenAI account is out of credits. Add credits at platform.openai.com > Billing.", detail)
        if exc.code == 429:
            fail("Rate limited by OpenAI (429). Wait a minute and try again.", detail)
        fail(f"OpenAI returned HTTP {exc.code}.", detail)
    except urllib.error.URLError as exc:
        # No response object exists here, so nothing may reference one.
        fail(f"Could not reach OpenAI: {exc.reason}")

    content = body["choices"][0]["message"]["content"].strip()
    events = json.loads(content).get("events", [])

    cutoff = (now - timedelta(days=30)).date()
    kept = []
    for ev in events:
        try:
            dt = datetime.strptime(ev["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            print(f"Skipping event with bad date: {ev.get('summary', '?')}", file=sys.stderr)
            continue
        # A running multi-day event is not past until its last day.
        d = last_day(ev, dt).date()
        if d < cutoff:
            print(f"Skipping event in the past: {ev.get('summary', '?')} ({d})", file=sys.stderr)
            continue
        kept.append(ev)
    return kept


def event_timezone(event):
    """The event's own zone when the model named a real one, else the default."""
    name = (event.get("timezone") or "").strip()
    if not name:
        return TZ
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        print(f"Ignoring invalid timezone from API: {name}", file=sys.stderr)
        return TZ
    return name


def last_day(event, start):
    """Last day of a span; the start day when there is no usable end_date."""
    value = (event.get("end_date") or "").strip()
    if not value:
        return start
    try:
        end = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        print(f"Ignoring unparsable end date: {value}", file=sys.stderr)
        return start
    if end < start:
        print(f"Ignoring end date before start date: {value}", file=sys.stderr)
        return start
    return end


def ics_escape(text):
    if not text:
        return ""
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def build_ics(events):
    lines = []
    zones = []
    for ev in events:
        try:
            dt = datetime.strptime(ev["date"], "%Y-%m-%d")
        except (KeyError, ValueError, TypeError):
            continue
        start_time = ev.get("start_time")
        end_time = ev.get("end_time")
        dt_last = last_day(ev, dt)

        lines.append("BEGIN:VEVENT")
        lines.append(f"UID:{uuid.uuid4()}@text-to-ics")

        if start_time:
            tz_name = event_timezone(ev)
            if tz_name not in zones:
                zones.append(tz_name)
            h, m = map(int, start_time.split(":"))
            dt_start = dt.replace(hour=h, minute=m)
            if end_time:
                eh, em = map(int, end_time.split(":"))
                dt_end = dt_last.replace(hour=eh, minute=em)
                # On a single day, an end at or before the start runs past midnight.
                if dt_end <= dt_start:
                    dt_end += timedelta(days=1)
            else:
                dt_end = dt_last.replace(hour=h, minute=m) + timedelta(hours=DEFAULT_DURATION_HOURS)
            lines.append(f"DTSTART;TZID={tz_name}:{dt_start.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND;TZID={tz_name}:{dt_end.strftime('%Y%m%dT%H%M%S')}")
        else:
            # All-day DTEND is exclusive, so a span ends the day after its last day.
            lines.append(f"DTSTART;VALUE=DATE:{dt.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(dt_last + timedelta(days=1)).strftime('%Y%m%d')}")

        lines.append(f"SUMMARY:{ics_escape(ev.get('summary', 'Event'))}")
        if ev.get("location"):
            lines.append(f"LOCATION:{ics_escape(ev['location'])}")
        if ev.get("description"):
            lines.append(f"DESCRIPTION:{ics_escape(ev['description'])}")

        if ev.get("url"):
            lines.append(f"URL:{ics_escape(ev['url'])}")
        if ev.get("organizer"):
            lines.append(f"ORGANIZER;CN={ics_escape(ev['organizer'])}:invalid:nomail")
        if ev.get("categories"):
            lines.append(f"CATEGORIES:{','.join(ics_escape(c) for c in ev['categories'])}")

        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines.append(f"DTSTAMP:{now}")

        # Alerts: 1 day and 1 hour before
        lines.append("BEGIN:VALARM")
        lines.append("TRIGGER:-P1D")
        lines.append("ACTION:DISPLAY")
        lines.append(f"DESCRIPTION:{ics_escape(ev.get('summary', 'Event'))}")
        lines.append("END:VALARM")
        lines.append("BEGIN:VALARM")
        lines.append("TRIGGER:-PT1H")
        lines.append("ACTION:DISPLAY")
        lines.append(f"DESCRIPTION:{ics_escape(ev.get('summary', 'Event'))}")
        lines.append("END:VALARM")

        lines.append("END:VEVENT")

    header = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Text to ICS//Alfred Workflow//EN",
        "CALSCALE:GREGORIAN",
    ]
    # Only the zones actually referenced by a TZID above, so an all-day-only
    # file carries no VTIMEZONE at all.
    header += [_vtimezone(z) for z in zones]
    return "\r\n".join(fold_line(l) for l in header + lines + ["END:VCALENDAR"])


def fold_line(line):
    # RFC 5545: lines longer than 75 octets must be folded with CRLF + space.
    # A VTIMEZONE block already contains its own newlines; fold per sub-line.
    if "\n" in line:
        return "\r\n".join(fold_line(sub) for sub in line.split("\n"))
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks, i = [], 0
    while i < len(encoded):
        # First chunk gets 75 bytes; continuation chunks get 74 (the leading space counts).
        size = 75 if not chunks else 74
        end = min(i + size, len(encoded))
        # Don't split inside a multi-byte UTF-8 sequence.
        while end < len(encoded) and (encoded[end] & 0xC0) == 0x80:
            end -= 1
        chunks.append(encoded[i:end].decode("utf-8"))
        i = end
    return "\r\n ".join(chunks)


def main():
    try:
        text = get_input_text()
        api_key = load_api_key()
        events = extract_events(text, api_key)
        if not events:
            print("No events found in text")
            return

        ics_content = build_ics(events)

        fd, filepath = tempfile.mkstemp(suffix=".ics", prefix="text-to-ics-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(ics_content)

        subprocess.run(["open", filepath], check=False)

        n = len(events)
        print(f"Opened {n} event{'s' if n != 1 else ''} in Calendar")

    except json.JSONDecodeError:
        print("Failed to parse event data from API")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
