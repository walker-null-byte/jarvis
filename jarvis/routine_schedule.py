"""Schedules for routines: a small cron parser, an English parser and plain-English descriptions.

A schedule is a dict:
    {"kind": "cron", "cron": "0 9 * * 1-5"}
    {"kind": "interval", "every_minutes": 20}
    {"kind": "once", "at": "2026-12-01T09:00:00"}

All times are naive local datetimes (the machine's time zone). Minutes that do
not exist because of a daylight-saving jump are skipped.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

EXAMPLES = (
    "every 15 minutes, every 2 hours, hourly, every day at 9am, every weekday at 09:00, "
    "every monday at 5pm, weekly on friday at 5pm, every morning, on the 1st at 8, "
    "in 20 minutes, tomorrow at 8, at 2026-12-01 09:00, or a cron line like 0 9 * * 1-5"
)

MONTH_NAMES = {n: i for i, n in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
DAY_NAMES = {n: i for i, n in enumerate("sun mon tue wed thu fri sat".split())}
DAY_LABELS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
CRON_ALIASES = {
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0", "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *",
}
MAX_INTERVAL_MINUTES = 60 * 24 * 366


class ScheduleError(ValueError):
    """A schedule that cannot be understood; the message is meant to be shown to the user."""


# ---------- cron ----------

@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset
    hours: frozenset
    days: frozenset      # day of month
    months: frozenset
    weekdays: frozenset  # 0 = Sunday .. 6 = Saturday
    days_star: bool      # the field started with "*" (Vixie semantics)
    weekdays_star: bool

    def day_matches(self, day):
        """Vixie rule: when both day fields are restricted either may match, else both must."""
        by_day = day.day in self.days
        by_weekday = (day.isoweekday() % 7) in self.weekdays
        if self.days_star or self.weekdays_star:
            return by_day and by_weekday
        return by_day or by_weekday


def _number(token, names, label):
    if token.isdigit():
        return int(token)
    if names and token[:3].lower() in names and token.isalpha():
        return names[token[:3].lower()]
    raise ScheduleError(f"Bad {label} value “{token}” in cron expression")


def _parse_field(text, low, high, names, label):
    values = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"Empty {label} entry in cron expression")
        span, _, step_text = part.partition("/")
        if "/" in part:
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(f"Bad step “{step_text}” in {label}")
            step = int(step_text)
        else:
            step = 1
        if span == "*":
            start, end = low, high
        elif "-" in span:
            first, _, last = span.partition("-")
            start, end = _number(first, names, label), _number(last, names, label)
        else:
            start = _number(span, names, label)
            end = high if "/" in part else start
        if not (low <= start <= high and low <= end <= high) or start > end:
            raise ScheduleError(f"{label.capitalize()} “{part}” is out of range ({low}-{high})")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression):
    """Parse a 5-field cron expression (or an @daily-style alias) into a CronSpec."""
    text = " ".join(str(expression or "").split())
    text = CRON_ALIASES.get(text.lower(), text)
    fields = text.split(" ")
    if len(fields) != 5:
        raise ScheduleError("A cron expression needs 5 fields: minute hour day-of-month month day-of-week")
    minutes = _parse_field(fields[0], 0, 59, None, "minute")
    hours = _parse_field(fields[1], 0, 23, None, "hour")
    days = _parse_field(fields[2], 1, 31, None, "day of month")
    months = _parse_field(fields[3], 1, 12, MONTH_NAMES, "month")
    weekdays = {d % 7 for d in _parse_field(fields[4], 0, 7, DAY_NAMES, "day of week")}
    return CronSpec(frozenset(minutes), frozenset(hours), frozenset(days), frozenset(months),
                    frozenset(weekdays), fields[2].startswith("*"), fields[4].startswith("*"))


def _exists(moment):
    """False for local wall-clock times skipped by a daylight-saving jump."""
    try:
        return datetime.fromtimestamp(moment.timestamp()) == moment
    except (OverflowError, OSError, ValueError):
        return True


def cron_next(spec, after):
    """The first matching minute strictly after `after`, or None within ~9 years."""
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hours, minutes = sorted(spec.hours), sorted(spec.minutes)
    day = start.date()
    for _ in range(366 * 9):
        if day.month in spec.months and spec.day_matches(day):
            for hour in hours:
                for minute in minutes:
                    candidate = datetime(day.year, day.month, day.day, hour, minute)
                    if candidate >= start and _exists(candidate):
                        return candidate
        day += timedelta(days=1)
    return None


def next_runs(expression, after=None, n=3):
    """The next `n` run times of a cron expression after `after` (default: now)."""
    spec = parse_cron(expression)
    moment, out = after or datetime.now(), []
    for _ in range(n):
        moment = cron_next(spec, moment)
        if moment is None:
            break
        out.append(moment)
    return out


# ---------- schedule dicts ----------

def _parse_iso(value):
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        raise ScheduleError(f"“{value}” is not a valid date and time") from None
    return moment.replace(tzinfo=None) if moment.tzinfo else moment


def validate_schedule(schedule):
    """Return a normalised copy of a schedule dict or raise ScheduleError."""
    if not isinstance(schedule, dict):
        raise ScheduleError("The schedule must be an object with a kind")
    kind = schedule.get("kind")
    if kind == "cron":
        expression = " ".join(str(schedule.get("cron", "")).split())
        parse_cron(expression)
        return {"kind": "cron", "cron": CRON_ALIASES.get(expression.lower(), expression)}
    if kind == "interval":
        try:
            minutes = int(schedule.get("every_minutes"))
        except (TypeError, ValueError):
            raise ScheduleError("An interval needs every_minutes as a whole number") from None
        if not 1 <= minutes <= MAX_INTERVAL_MINUTES:
            raise ScheduleError("An interval must be between 1 minute and 1 year")
        return {"kind": "interval", "every_minutes": minutes}
    if kind == "once":
        return {"kind": "once", "at": _parse_iso(schedule.get("at")).isoformat(timespec="seconds")}
    raise ScheduleError("Schedule kind must be cron, interval or once")


def next_after(schedule, after):
    """The next run strictly after `after` for any schedule kind, or None (a past one-off)."""
    schedule = validate_schedule(schedule)
    if schedule["kind"] == "cron":
        return cron_next(parse_cron(schedule["cron"]), after)
    if schedule["kind"] == "interval":
        return after.replace(microsecond=0) + timedelta(minutes=schedule["every_minutes"])
    at = _parse_iso(schedule["at"])
    return at if at > after else None


def upcoming(schedule, after=None, n=3):
    """The next `n` run times of any schedule."""
    moment, out = after or datetime.now(), []
    for _ in range(n):
        moment = next_after(schedule, moment)
        if moment is None:
            break
        out.append(moment)
    return out


# ---------- describing ----------

def _ordinal(number):
    suffix = "th" if 10 <= number % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return f"{number}{suffix}"


def _every(count, unit):
    return f"Every {unit}" if count == 1 else f"Every {count} {unit}s"


def _describe_cron(expression):
    spec = parse_cron(expression)
    every_month = len(spec.months) == 12
    minutes, hours = sorted(spec.minutes), sorted(spec.hours)
    if every_month and len(minutes) == 1 and len(hours) == 1:
        at = f"{hours[0]:02d}:{minutes[0]:02d}"
        weekdays = sorted(spec.weekdays)
        if spec.days_star and len(spec.days) == 31:
            if len(weekdays) == 7:
                return f"Daily at {at}"
            if weekdays == [1, 2, 3, 4, 5]:
                return f"Weekdays at {at}"
            if weekdays == [0, 6]:
                return f"Weekends at {at}"
            if len(weekdays) == 1:
                return f"{DAY_LABELS[weekdays[0]]}s at {at}"
            names = ", ".join(DAY_LABELS[d][:3] for d in sorted(weekdays, key=lambda d: (d - 1) % 7))
            return f"{names} at {at}"
        if spec.weekdays_star and len(weekdays) == 7:
            days = ", ".join(_ordinal(d) for d in sorted(spec.days))
            return f"Monthly on the {days} at {at}"
    if every_month and len(spec.days) == 31 and len(spec.weekdays) == 7:
        if len(hours) == 24 and len(minutes) == 1:
            return "Every hour, on the hour" if minutes[0] == 0 else f"Every hour at :{minutes[0]:02d}"
        step = _step_of(minutes, 60)
        if step and len(hours) == 24:
            return _every(step, "minute")
        step = _step_of(hours, 24)
        if step and len(minutes) == 1:
            suffix = "" if minutes[0] == 0 else f" at :{minutes[0]:02d}"
            return _every(step, "hour") + suffix
    return f"Cron: {expression}"


def _step_of(values, limit):
    """N when values are exactly 0, N, 2N, ... below limit (N > 1 or a full range), else 0."""
    if not values or values[0] != 0:
        return 0
    step = values[1] - values[0] if len(values) > 1 else limit
    return step if list(values) == list(range(0, limit, step)) else 0


def describe(schedule):
    """A short human description of any schedule ("Weekdays at 09:00")."""
    schedule = validate_schedule(schedule)
    if schedule["kind"] == "interval":
        minutes = schedule["every_minutes"]
        if minutes % 1440 == 0:
            return _every(minutes // 1440, "day")
        if minutes % 60 == 0:
            return _every(minutes // 60, "hour")
        return _every(minutes, "minute")
    if schedule["kind"] == "once":
        moment = _parse_iso(schedule["at"])
        return f"Once on {moment:%a} {moment.day} {moment:%b %Y} at {moment:%H:%M}"
    return _describe_cron(schedule["cron"])


# ---------- English ----------

DEFAULT_TIME = (9, 0)
PARTS_OF_DAY = {"morning": (8, 0), "afternoon": (14, 0), "evening": (18, 0), "night": (22, 0)}
DAY_WORDS = r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun)s?"
PLURAL_DAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)s\b")
TIME_RE = re.compile(r"(?:\bat\s+)?\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?(?![\d:])", re.I)
HOUR_AMPM_RE = re.compile(r"\b(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)(?![a-z])", re.I)
HOUR_AT_RE = re.compile(r"\bat\s+(\d{1,2})(?:\s*o'?clock)?\b(?!\s*(?:st|nd|rd|th)\b)(?![:\d-])", re.I)
DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?:[ t]+(\d{1,2}):(\d{2}))?", re.I)


def _clock(hour, minute, meridiem=None):
    hour, minute = int(hour), int(minute)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ScheduleError(f"“{hour} {meridiem}” is not a valid time")
        hour = hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"{hour}:{minute:02d} is not a valid time")
    return hour, minute


def _find_time(text):
    """(hour, minute) of the time mentioned in text, or None."""
    if re.search(r"\bnoon\b", text):
        return 12, 0
    if re.search(r"\bmidnight\b", text):
        return 0, 0
    for pattern in (TIME_RE, HOUR_AMPM_RE, HOUR_AT_RE):
        match = pattern.search(text)
        if match:
            groups = match.groups()
            if pattern is TIME_RE:
                return _clock(groups[0], groups[1], groups[2] and groups[2][0])
            if pattern is HOUR_AMPM_RE:
                return _clock(groups[0], 0, groups[1][0])
            return _clock(groups[0], 0)
    return None


def _day_index(word):
    return DAY_NAMES[word.lower()[:3]]


def _cron(minute, hour, days="*", weekdays="*"):
    return {"kind": "cron", "cron": f"{minute} {hour} {days} * {weekdays}"}


def _weekday_list(numbers):
    return ",".join(str(n) for n in sorted(set(numbers), key=lambda n: (n - 1) % 7))


def _next_weekday(now, weekday, at):
    """The next date (after now) falling on `weekday` (0 = Sunday) at the given time."""
    ahead = (weekday - now.isoweekday() % 7) % 7
    moment = (now + timedelta(days=ahead)).replace(hour=at[0], minute=at[1], second=0, microsecond=0)
    return moment if moment > now else moment + timedelta(days=7)


def _once(moment, now):
    if moment <= now:
        raise ScheduleError(f"{moment:%Y-%m-%d %H:%M} is already in the past")
    return {"kind": "once", "at": moment.replace(second=0, microsecond=0).isoformat(timespec="seconds")}


def _fail(text):
    raise ScheduleError(f"I couldn't understand “{text}”. Try: {EXAMPLES}.")


def parse_schedule(text, now=None):
    """Understand a schedule written in English or cron. Returns (schedule, description).

    Raises ScheduleError (with examples) when it cannot.
    """
    now = (now or datetime.now()).replace(microsecond=0)
    raw = " ".join(str(text or "").split())
    if not raw:
        _fail(raw)
    low = re.sub(r"[.!]+$", "", raw.lower())
    low = re.sub(r"^(?:please\s+|run\s+|repeat\s+)", "", low)

    words = low.split()
    if low.startswith("@") or (len(words) == 5 and re.fullmatch(r"[\d*/,\-]+", words[0])):
        schedule = validate_schedule({"kind": "cron", "cron": raw})  # a bad cron line reports the cron problem
        return schedule, describe(schedule)
    schedule = _english(low, now) or _fail(raw)
    schedule = validate_schedule(schedule)
    return schedule, describe(schedule)


def _english(low, now):
    # in 20 minutes / in an hour / in 2 days
    match = re.fullmatch(r"in\s+(\d+|an?|half an?)\s*(minutes?|mins?|hours?|hrs?|days?|weeks?)", low)
    if match:
        amount = {"a": 1, "an": 1, "half a": 0.5, "half an": 0.5}.get(match.group(1)) or int(match.group(1))
        unit = {"m": 1, "h": 60, "d": 1440, "w": 10080}[match.group(2)[0]]
        return _once(now + timedelta(minutes=amount * unit), now)

    # every 15 minutes / every 2 hours / every minute / every hour / every 3 days
    match = re.fullmatch(r"(?:every|each)\s+(\d+\s*)?(minutes?|mins?|hours?|hrs?|days?)", low)
    if match and (match.group(1) or match.group(2)[0] != "d"):
        count = int((match.group(1) or "1").strip())
        unit = {"m": 1, "h": 60, "d": 1440}[match.group(2)[0]]
        return {"kind": "interval", "every_minutes": count * unit}
    if re.fullmatch(r"every\s+half\s+(an\s+)?hour", low):
        return {"kind": "interval", "every_minutes": 30}
    if low == "hourly":
        return _cron(0, "*")

    # absolute date: at 2026-12-01 09:00
    match = DATE_RE.search(low)
    if match:
        year, month, day, hour, minute = match.groups()
        rest = low[:match.start()] + " " + low[match.end():]
        at = (int(hour), int(minute)) if hour else (_find_time(rest) or DEFAULT_TIME)
        try:
            return _once(datetime(int(year), int(month), int(day), *_clock(*at)), now)
        except ValueError:
            raise ScheduleError(f"{match.group(0)} is not a real date") from None

    at = _find_time(low)
    recurring = bool(re.search(r"\b(every|each|daily|weekly|monthly|weekdays?|weekends?|everyday)\b", low))
    part = next((p for p in PARTS_OF_DAY if re.search(rf"\b{p}s?\b", low)), None)
    time = at or (PARTS_OF_DAY[part] if part else DEFAULT_TIME)

    # tomorrow / today / tonight
    for word, offset, default in (("tomorrow", 1, None), ("today", 0, None), ("tonight", 0, (20, 0))):
        if re.search(rf"\b{word}\b", low):
            moment = (now + timedelta(days=offset)).replace(hour=0, minute=0)
            clock = at or default or (PARTS_OF_DAY[part] if part else DEFAULT_TIME)
            return _once(moment.replace(hour=clock[0], minute=clock[1]), now)

    # monthly: on the 1st / the 1st and 15th / monthly
    days = [int(n) for n in re.findall(r"\b(\d{1,2})(?:st|nd|rd|th)\b", low)]
    if days or re.search(r"\b(monthly|every month|each month)\b", low):
        days = sorted(set(days)) or [1]
        if not all(1 <= d <= 31 for d in days):
            raise ScheduleError("Days of the month must be between 1 and 31")
        return _cron(time[1], time[0], ",".join(str(d) for d in days))

    # weekdays / weekends / named days / weekly
    if re.search(r"\bweekdays?\b", low):
        return _cron(time[1], time[0], weekdays="1-5")
    if re.search(r"\bweekends?\b", low):
        return _cron(time[1], time[0], weekdays="0,6")
    named = [_day_index(m.group(0)) for m in re.finditer(rf"\b{DAY_WORDS}\b", low)]
    if named:
        if recurring or PLURAL_DAY_RE.search(low) or len(set(named)) > 1:
            return _cron(time[1], time[0], weekdays=_weekday_list(named))
        return _once(_next_weekday(now, named[0], time), now)
    if "weekly" in low:
        return _cron(time[1], time[0], weekdays="1")

    # daily / every day / every morning
    if re.search(r"\b(daily|everyday|every day|each day|every (morning|afternoon|evening|night))\b", low):
        return _cron(time[1], time[0])

    # just a time: the next occurrence, once
    leftover = HOUR_AT_RE.sub(" ", HOUR_AMPM_RE.sub(" ", TIME_RE.sub(" ", low)))
    if at and not recurring and not re.sub(r"\b(at|on|the|noon|midnight)\b|\s", "", leftover):
        moment = now.replace(hour=at[0], minute=at[1], second=0)
        return _once(moment if moment > now else moment + timedelta(days=1), now)
    return None
