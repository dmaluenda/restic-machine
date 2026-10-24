# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""restic's retention rules, reimplemented to look ahead.

`restic forget --dry-run` is the reference, and the Retention page uses it for "now". It can
only judge snapshots that exist, though; the calendar needs the ones still to come, to show
which of today's snapshots would be left on a future day. This is a copy of restic's rules
(restic 0.17-0.18, internal/restic/snapshot_policy.go) for that, and the tests hold it to
restic's own answers.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

_DURATION = re.compile(r"(?:(\d+)y)?(?:(\d+)m)?(?:(\d+)d)?(?:(\d+)h)?")


@dataclass
class Snapshot:
    time: datetime  # with its UTC offset: restic's day/week/month boundaries follow it
    id: str | None = None  # None for a projected, future one
    tags: list[str] = field(default_factory=list)


@dataclass
class Duration:
    years: int = 0
    months: int = 0
    days: int = 0
    hours: int = 0

    @classmethod
    def parse(cls, text: str) -> "Duration":
        match = _DURATION.fullmatch(text.strip())
        if not match or not text.strip():
            raise ValueError(f"not a duration: {text!r}")
        return cls(*(int(part or 0) for part in match.groups()))

    def __str__(self) -> str:  # as restic prints it in its reasons
        return "".join(f"{value}{unit}" for value, unit in
                       ((self.years, "y"), (self.months, "m"), (self.days, "d"), (self.hours, "h")) if value)

    def before(self, moment: datetime) -> datetime:
        """`moment` minus this duration, the way Go's AddDate does it (day overflow rolls on)."""
        months = moment.year * 12 + (moment.month - 1) - self.years * 12 - self.months
        year, month = divmod(months, 12)
        first = moment.replace(year=year, month=month + 1, day=1)
        return first + timedelta(days=moment.day - 1 - self.days, hours=-self.hours)


def _always(t: datetime, nr: int) -> int:
    return nr


def _hour(t: datetime, nr: int) -> int:
    return ((t.year * 100 + t.month) * 100 + t.day) * 100 + t.hour


def _day(t: datetime, nr: int) -> int:
    return (t.year * 100 + t.month) * 100 + t.day


def _week(t: datetime, nr: int) -> int:
    year, week, _ = t.isocalendar()
    return year * 100 + week


def _month(t: datetime, nr: int) -> int:
    return t.year * 100 + t.month


def _year(t: datetime, nr: int) -> int:
    return t.year


_COUNTED = (("last", _always), ("hourly", _hour), ("daily", _day), ("weekly", _week),
            ("monthly", _month), ("yearly", _year))
_WITHIN = (("hourly", _hour), ("daily", _day), ("weekly", _week), ("monthly", _month), ("yearly", _year))


def apply(snapshots: Iterable[Snapshot], policy: dict) -> list[tuple[Snapshot, bool, list[str]]]:
    """Every snapshot, newest first, with whether the policy keeps it and why (restic's words).
    One group of snapshots: restic applies a policy to each host+paths group separately."""
    ordered = sorted(snapshots, key=lambda s: s.time, reverse=True)
    if not any(key.startswith("keep_") for key in policy):
        return [(s, True, []) for s in ordered]  # restic: no policy, nothing is removed
    latest = ordered[0].time if ordered else None
    counted = [[policy.get(f"keep_{name}", 0), bucket, None, f"{name} snapshot"] for name, bucket in _COUNTED]
    within_all = Duration.parse(policy["keep_within"]) if policy.get("keep_within") else None
    within = [[Duration.parse(policy[f"keep_within_{name}"]), bucket, None, f"{name} within"]
              for name, bucket in _WITHIN if policy.get(f"keep_within_{name}")]
    tags = policy.get("keep_tag", [])

    result = []
    for nr, snap in enumerate(ordered):
        reasons = []
        for tag in tags:
            if tag in snap.tags:
                reasons.append(f"has tags [{tag}]")
        if within_all is not None and snap.time > within_all.before(latest):
            reasons.append(f"within {within_all}")
        for rule in counted:
            count, bucket, last, reason = rule
            if count > 0:
                value = bucket(snap.time, nr)
                # Like restic, the oldest snapshot is kept too while a rule has room left:
                # it keeps the history as long as the policy allows.
                if value != last or nr == len(ordered) - 1:
                    reasons.append(reason if value != last else f"oldest {reason}")
                    rule[2] = value
                    rule[0] = count - 1
        for rule in within:
            duration, bucket, last, reason = rule
            if snap.time > duration.before(latest):
                value = bucket(snap.time, nr)
                if value != last:
                    reasons.append(f"{reason} {duration}")
                    rule[2] = value
        result.append((snap, bool(reasons), reasons))
    return result


def projected(snapshots: Iterable[Snapshot], until: date, days: int = 7) -> list[Snapshot]:
    """The backups still to come until the end of `until`, assuming that the times of day of
    the last `days` days repeat (one per hour of the day, at its usual minute)."""
    real = sorted((s.time for s in snapshots), reverse=True)
    if not real:
        return []
    newest = real[0]
    recent = [t for t in real if t > newest - timedelta(days=days)]
    minutes: dict[int, list[int]] = {}
    for t in recent:
        minutes.setdefault(t.hour, []).append(t.minute)
    times = sorted((hour, sorted(values)[len(values) // 2]) for hour, values in minutes.items())
    future = []
    day = newest.date()
    while day <= until:
        for hour, minute in times:
            moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=newest.tzinfo)
            if moment > newest:
                future.append(Snapshot(moment))
        day += timedelta(days=1)
    return future
