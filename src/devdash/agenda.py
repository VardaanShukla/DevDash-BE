"""The user's calendar, read from its private iCal address (Google Calendar, Outlook and
Apple Calendar all publish one). Read-only and needs no sign-in: the address itself is the
key, so it is kept in DevDash's own database and never sent back to the page in full.

Repeating events are expanded for the common rules: daily, weekly (on given weekdays),
monthly on the same day and yearly, with an interval, a count or an end date, skipped
dates and moved occurrences. Rarer rules, like "the second Tuesday", show their first
occurrence only."""

import re
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from devdash.store import connect

SETTING = "calendar_url"
TIMEOUT = 15
# Fetch the calendar again at most this often.
FRESH_SECONDS = 300
MAX_BYTES = 20 * 1024 * 1024
MAX_OCCURRENCES = 2000
WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

_cache: dict[str, tuple[float, str]] = {}


class AgendaError(RuntimeError):
    pass


def address() -> str | None:
    conn = connect()
    row = conn.execute("SELECT value FROM settings WHERE name = ?", (SETTING,)).fetchone()
    conn.close()
    return row["value"] if row else None


def connect_calendar(url: str) -> None:
    """Checks the address answers with a calendar, then keeps it."""
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not re.match(r"https?://[^\s/]+/\S*$", url):
        raise AgendaError("Paste the calendar's address, starting with https:// or webcal://")
    _fetch(url, fresh=True)
    conn = connect()
    with conn:
        conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES (?, ?)", (SETTING, url))
    conn.close()


def disconnect() -> None:
    url = address()
    _cache.pop(url or "", None)
    conn = connect()
    with conn:
        conn.execute("DELETE FROM settings WHERE name = ?", (SETTING,))
    conn.close()


def agenda(days: int, refresh: bool = False) -> dict:
    url = address()
    if not url:
        return {"connected": False, "source": None, "events": [], "fetched_at": None, "error": None}
    source = re.sub(r"^https?://([^/]+)/.*$", r"\1", url)
    try:
        text = _fetch(url, fresh=refresh)
    except AgendaError as error:
        return {"connected": True, "source": source, "events": [], "fetched_at": None, "error": str(error)}
    local = datetime.now().astimezone().tzinfo
    start = datetime.combine(date.today(), datetime.min.time(), local)
    events = occurrences(text, start, start + timedelta(days=days), local)
    fetched = datetime.fromtimestamp(_cache[url][0], timezone.utc).isoformat(timespec="seconds")
    return {"connected": True, "source": source, "events": events, "fetched_at": fetched, "error": None}


def _fetch(url: str, fresh: bool = False) -> str:
    cached = _cache.get(url)
    if cached and not fresh and time.time() - cached[0] < FRESH_SECONDS:
        return cached[1]
    request = urllib.request.Request(url, headers={"User-Agent": "DevDash"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise AgendaError(f"The calendar's address answered {error.code}. It may have been reset; paste the new one.")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise AgendaError("Can't reach the calendar right now. Check the internet connection.")
    if len(body) > MAX_BYTES:
        raise AgendaError("That calendar is too big to read.")
    text = body.decode("utf-8", errors="replace")
    if "BEGIN:VCALENDAR" not in text[:2000]:
        raise AgendaError("That address isn't a calendar. Use the one marked iCal or .ics.")
    _cache[url] = (time.time(), text)
    return text


def occurrences(text: str, start: datetime, end: datetime, local: tzinfo) -> list[dict]:
    """The events between `start` and `end`, repeating ones expanded, in time order."""
    events = list(_events(text, local))
    moved = {(e["uid"], e["recurrence_id"]) for e in events if e["recurrence_id"]}
    found = []
    for event in events:
        if event["cancelled"]:
            continue
        length = event["end"] - event["start"]
        for begin in _starts(event, start - length, end):
            if (event["uid"], begin) in moved and not event["recurrence_id"]:
                continue
            finish = begin + length
            if finish <= start if length else begin < start:
                continue
            found.append({
                "title": event["title"] or "(No title)",
                "start": _iso(begin, event["all_day"], local),
                "end": _iso(finish, event["all_day"], local),
                "all_day": event["all_day"],
                "location": event["location"],
            })
    found.sort(key=lambda e: (e["start"][:10], not e["all_day"], e["start"]))
    return found


def _iso(moment: datetime, all_day: bool, local: tzinfo) -> str:
    return moment.date().isoformat() if all_day else moment.astimezone(local).isoformat(timespec="minutes")


def _events(text: str, local: tzinfo):
    # Long lines are folded onto the next line, which then starts with a space or tab.
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    event, depth = None, 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            event, depth = {"exdate": []}, 0
        elif event is None:
            continue
        elif line.startswith("BEGIN:"):
            depth += 1  # an alarm inside the event
        elif line.startswith("END:") and depth:
            depth -= 1
        elif line == "END:VEVENT":
            parsed = _event(event, local)
            if parsed:
                yield parsed
            event = None
        elif not depth and ":" in line:
            head, value = line.split(":", 1)
            name, *params = head.split(";")
            params = dict(p.split("=", 1) for p in params if "=" in p)
            if name == "EXDATE":
                event["exdate"] += [(v, params) for v in value.split(",")]
            else:
                event[name.upper()] = (value, params)


def _event(raw: dict, local: tzinfo) -> dict | None:
    if "DTSTART" not in raw:
        return None
    start, all_day = _moment(*raw["DTSTART"], local)
    if "DTEND" in raw:
        end = _moment(*raw["DTEND"], local)[0]
    elif "DURATION" in raw:
        end = start + _duration(raw["DURATION"][0])
    else:
        end = start + (timedelta(days=1) if all_day else timedelta())
    return {
        "uid": raw.get("UID", ("", {}))[0],
        "title": _text(raw.get("SUMMARY", ("", {}))[0]),
        "location": _text(raw.get("LOCATION", ("", {}))[0]) or None,
        "start": start,
        "end": max(end, start),
        "all_day": all_day,
        "rule": _rule(raw["RRULE"][0]) if "RRULE" in raw else None,
        "skip": {_moment(v, p, local)[0] for v, p in raw["exdate"]},
        "recurrence_id": _moment(*raw["RECURRENCE-ID"], local)[0] if "RECURRENCE-ID" in raw else None,
        "cancelled": raw.get("STATUS", ("", {}))[0].upper() == "CANCELLED",
    }


def _moment(value: str, params: dict, local: tzinfo) -> tuple[datetime, bool]:
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=local), True
    moment = datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=timezone.utc), False
    return moment.replace(tzinfo=_zone(params.get("TZID"), local)), False


def _zone(name: str | None, local: tzinfo) -> tzinfo:
    if not name:
        return local
    try:
        return ZoneInfo(name.strip('"'))
    except (ZoneInfoNotFoundError, ValueError):
        # Outlook names zones like "India Standard Time"; take them as the computer's own.
        return local


def _duration(value: str) -> timedelta:
    found = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not found:
        return timedelta()
    sign, weeks, days, hours, minutes, seconds = found.groups()
    delta = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                      minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -delta if sign == "-" else delta


def _text(value: str) -> str:
    return re.sub(r"\\([\;,nN])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value).strip()


def _rule(value: str) -> dict:
    return dict(part.split("=", 1) for part in value.upper().split(";") if "=" in part)


def _starts(event: dict, after: datetime, end: datetime):
    """When the event starts: once, or each time its rule repeats it before `end`. Steps
    that end before `after` are skipped without counting, unless the rule has a count."""
    first, rule = event["start"], event["rule"]
    if not rule:
        if first < end:
            yield first
        return
    frequency = rule.get("FREQ")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        until, _ = _moment(rule["UNTIL"], {}, first.tzinfo)
        if event["all_day"] or len(rule["UNTIL"]) == 8:
            until += timedelta(days=1) - timedelta(seconds=1)
    supported = frequency in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY") and not (
        set(rule) & {"BYSETPOS", "BYWEEKNO", "BYYEARDAY", "BYHOUR", "BYMINUTE"}
        or (frequency in ("MONTHLY", "YEARLY") and ("BYDAY" in rule or "BYMONTHDAY" in rule))
    )
    if not supported:
        if first < end:
            yield first
        return
    days = sorted(WEEKDAYS[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in WEEKDAYS)
    seen, skipped = 0, 0
    if count is None and after > first:
        # A rule that began years ago: jump to just before the window.
        if frequency in ("DAILY", "WEEKLY"):
            gone = (after - first).days // (1 if frequency == "DAILY" else 7)
        else:
            gone = (after.year - first.year) * 12 + after.month - first.month
            gone //= 12 if frequency == "YEARLY" else 1
        skipped = max(0, gone // interval - 1)
    for step in range(skipped, skipped + MAX_OCCURRENCES):
        for moment in _step(first, frequency, interval, step, days):
            if moment < first:
                continue
            if (until and moment > until) or moment >= end or (count is not None and seen >= count):
                return
            seen += 1
            if moment not in event["skip"]:
                yield moment


def _step(first: datetime, frequency: str, interval: int, step: int, days: list[int]) -> list[datetime]:
    if frequency == "DAILY":
        moment = first + timedelta(days=step * interval)
        return [moment] if not days or moment.weekday() in days else []
    if frequency == "WEEKLY":
        week = first + timedelta(weeks=step * interval)
        if not days:
            return [week]
        monday = week - timedelta(days=week.weekday())
        return [monday + timedelta(days=d) for d in days]
    months = step * interval * (12 if frequency == "YEARLY" else 1)
    year, month = divmod(first.month - 1 + months, 12)
    try:
        return [first.replace(year=first.year + year, month=month + 1)]
    except ValueError:
        return []  # the 31st in a shorter month, or 29 February
