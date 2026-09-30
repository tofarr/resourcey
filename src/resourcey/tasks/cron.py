"""The internal 5-field cron parser and next-fire computation (issue #15).

Scoped deliberately: a schedule is a standard **5-field** expression — ``minute
hour day-of-month month day-of-week`` — plus the ``@``-shorthands
(``@hourly`` / ``@daily`` / ``@weekly`` / ``@monthly`` / ``@yearly``). A seconds
field, ``@reboot``, and quartz extensions (``?`` / ``L`` / ``W``) are out of
scope, so this stays a small, dependency-free algorithm.

Each field supports ``*`` (any), ``*/n`` (every ``n``), a fixed value, a range
(``a-b``), a step over a range (``a-b/n``), and comma lists of those. Month and
day-of-week also accept the conventional three-letter names (``jan`` /
``mon``). Day-of-week is ``0``-``6`` with ``0`` = Sunday (``7`` is accepted as
Sunday too).

The one rule worth stating: when **both** day-of-month and day-of-week are
restricted, a day matches if *either* does — the standard cron OR. When only one
is restricted, that one must match. The rule is pinned by
``specs/background_tasks.qnt``.

This module imports no code outside the framework (the standard library only).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

# The bound on the roll-forward search. Each iteration advances at least a
# minute and usually far more, so this is generous enough for a leap-year
# schedule (~1461 day-steps) while still failing fast on an unsatisfiable one.
_MAX_ITERATIONS = 200_000

_MONTH_NAMES = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DOW_NAMES = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}

# The standard @-shorthands, as the 5-field expressions they stand for.
_SHORTHANDS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

# Field bounds are declared inline in ``parse_cron`` so each field carries its
# own accepted names (month / weekday only).

_ITEM_RE = re.compile(r"^(?P<base>[^/]+)(?:/(?P<step>\d+))?$")


class CronError(ValueError):
    """A cron expression could not be parsed."""


@dataclass(frozen=True)
class CronSchedule:
    """A parsed 5-field cron expression.

    The ``restricted`` flags record whether a field was a literal ``*`` — the
    day-of-month / day-of-week OR rule keys off exactly that.
    """

    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    def matches(self, moment: datetime) -> bool:
        """Whether ``moment`` (to the minute) satisfies the schedule."""
        if moment.minute not in self.minutes:
            return False
        if moment.hour not in self.hours:
            return False
        if moment.month not in self.months:
            return False
        return self._day_matches(moment)

    def _day_matches(self, moment: datetime) -> bool:
        """The day rule: both restricted is an OR, one restricted is that one."""
        dom_ok = moment.day in self.days
        dow_ok = _cron_weekday(moment) in self.weekdays
        if self.day_restricted and self.weekday_restricted:
            return dom_ok or dow_ok
        if self.day_restricted:
            return dom_ok
        if self.weekday_restricted:
            return dow_ok
        return True

    def next_fire(self, after: datetime) -> datetime:
        """The first minute strictly after ``after`` that satisfies the schedule.

        ``after`` must be timezone-aware; the result carries the same tzinfo.
        The search rolls forward a whole unit at a time (month, day, hour,
        minute), so it stays cheap even for a sparse schedule.
        """
        candidate = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
        for _ in range(_MAX_ITERATIONS):
            if candidate.month not in self.months:
                candidate = _first_of_next_month(candidate)
                continue
            if not self._day_matches(candidate):
                candidate = (candidate + timedelta(days=1)).replace(hour=0, minute=0)
                continue
            if candidate.hour not in self.hours:
                candidate = (candidate + timedelta(hours=1)).replace(minute=0)
                continue
            if candidate.minute not in self.minutes:
                candidate = candidate + timedelta(minutes=1)
                continue
            return candidate
        raise CronError(f"Could not find a next fire for schedule within {_MAX_ITERATIONS} steps")


def _cron_weekday(moment: datetime) -> int:
    """The cron day-of-week for ``moment`` (0 = Sunday, Python's Monday = 0)."""
    return (moment.weekday() + 1) % 7


def _first_of_next_month(moment: datetime) -> datetime:
    """The first minute of the month after ``moment``."""
    if moment.month == 12:
        return moment.replace(year=moment.year + 1, month=1, day=1, hour=0, minute=0)
    return moment.replace(month=moment.month + 1, day=1, hour=0, minute=0)


def parse_cron(expression: str) -> CronSchedule:
    """Parse a 5-field cron expression (or an ``@``-shorthand).

    Raises :class:`CronError` for an unknown shorthand, the wrong number of
    fields, an out-of-range value, or a malformed step / range. Callers are
    expected to surface that at startup rather than silently never firing.
    """
    text = expression.strip()
    if not text:
        raise CronError("Empty cron expression")
    resolved = _SHORTHANDS.get(text.lower(), text.lower() if text.startswith("@") else text)
    if resolved.startswith("@"):
        raise CronError(f"Unknown cron shorthand {text!r}")
    parts = resolved.split()
    if len(parts) != 5:
        raise CronError(
            f"Cron expression {expression!r} must have 5 fields "
            "(minute hour day-of-month month day-of-week)"
        )
    bounds: tuple[tuple[int, int, dict[str, int]], ...] = (
        (0, 59, {}),
        (0, 23, {}),
        (1, 31, {}),
        (1, 12, _MONTH_NAMES),
        (0, 6, _DOW_NAMES),
    )
    expanded = [
        _parse_field(part, lo, hi, names)
        for part, (lo, hi, names) in zip(parts, bounds, strict=True)
    ]
    minutes, hours, days, months, weekdays = expanded
    return CronSchedule(
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        days=frozenset(days),
        months=frozenset(months),
        weekdays=frozenset(weekdays),
        day_restricted=parts[2] != "*",
        weekday_restricted=parts[4] != "*",
    )


def _parse_field(spec: str, lo: int, hi: int, names: dict[str, int]) -> set[int]:
    """Expand one field into the set of integer values it selects."""
    values: set[int] = set()
    for item in spec.split(","):
        if not item:
            raise CronError(f"Empty entry in cron field {spec!r}")
        match = _ITEM_RE.match(item)
        if match is None:  # pragma: no cover - the regex is total over non-empty
            raise CronError(f"Malformed cron field entry {item!r}")
        base = match.group("base")
        step_text = match.group("step")
        step = int(step_text) if step_text is not None else 1
        if step < 1:
            raise CronError(f"Cron step must be >= 1 in {item!r}")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_text, _, end_text = base.partition("-")
            start = _resolve(start_text, lo, hi, names)
            end = _resolve(end_text, lo, hi, names)
            if start > end:
                raise CronError(f"Cron range start after end in {item!r}")
        else:
            start = end = _resolve(base, lo, hi, names)
            if step_text is not None:
                raise CronError(f"A step is only valid on a range or '*' in {item!r}")
        values.update(range(start, end + 1, step))
    return values


def _resolve(token: str, lo: int, hi: int, names: dict[str, int]) -> int:
    """Resolve a value token (numeric or a known name) within ``[lo, hi]``."""
    key = token.strip().lower()
    if key in names:
        return names[key]
    try:
        value = int(key)
    except ValueError as exc:
        raise CronError(f"Invalid cron value {token!r}") from exc
    # Cron accepts 7 as Sunday in the day-of-week field.
    if names is _DOW_NAMES and value == 7:
        value = 0
    if value < lo or value > hi:
        raise CronError(f"Cron value {token!r} out of range [{lo}, {hi}]")
    return value
