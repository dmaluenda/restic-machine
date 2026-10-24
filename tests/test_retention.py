# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 David Maluenda

"""restic's retention rules as copied in retention.py (test_restic.py holds them to restic itself)."""

import unittest
from datetime import date, datetime, timedelta, timezone

from restic_machine.retention import Duration, Snapshot, apply, projected

CET = timezone(timedelta(hours=2))


def at(text: str, tags=()) -> Snapshot:
    return Snapshot(datetime.fromisoformat(text).replace(tzinfo=CET), id=text, tags=list(tags))


def kept(snapshots, policy):
    return {snap.id: reasons for snap, keep, reasons in apply(snapshots, policy) if keep}


class RetentionTest(unittest.TestCase):
    def test_durations_subtract_like_go(self):
        moment = datetime(2026, 3, 31, 10, tzinfo=CET)
        self.assertEqual(Duration.parse("1m").before(moment), datetime(2026, 3, 3, 10, tzinfo=CET))  # Feb 31 rolls on
        self.assertEqual(Duration.parse("1y2d3h").before(moment), datetime(2025, 3, 29, 7, tzinfo=CET))
        self.assertEqual(str(Duration.parse("1y6m")), "1y6m")
        with self.assertRaises(ValueError):
            Duration.parse("6 months")

    def test_counted_rules_and_the_oldest_snapshot(self):
        snaps = [at("2026-10-24T10:00"), at("2026-10-24T09:00"), at("2026-10-23T10:00"),
                 at("2026-10-22T10:00"), at("2026-09-01T10:00")]
        self.assertEqual(kept(snaps, {"keep_last": 2, "keep_daily": 2}), {
            "2026-10-24T10:00": ["last snapshot", "daily snapshot"],
            "2026-10-24T09:00": ["last snapshot"],
            "2026-10-23T10:00": ["daily snapshot"]})
        # Rules with room left also keep the oldest snapshot, as restic >= 0.17 does.
        self.assertEqual(kept(snaps, {"keep_monthly": 5})["2026-09-01T10:00"], ["monthly snapshot"])
        self.assertEqual(kept(snaps, {"keep_yearly": 5}), {
            "2026-10-24T10:00": ["yearly snapshot"], "2026-09-01T10:00": ["oldest yearly snapshot"]})

    def test_tags_and_within(self):
        snaps = [at("2026-10-24T10:00"), at("2026-10-20T10:00", ["keep"]), at("2026-10-10T10:00")]
        self.assertEqual(kept(snaps, {"keep_within": "5d", "keep_tag": ["keep"]}), {
            "2026-10-24T10:00": ["within 5d"], "2026-10-20T10:00": ["has tags [keep]", "within 5d"]})
        self.assertEqual(set(kept(snaps, {"keep_within_daily": "10d"})), {"2026-10-24T10:00", "2026-10-20T10:00"})

    def test_no_rule_keeps_everything(self):
        snaps = [at("2026-10-24T10:00"), at("2026-10-23T10:00")]
        self.assertEqual(len(kept(snaps, {"prune": True})), 2)

    def test_projected_backups_repeat_the_recent_times_of_day(self):
        snaps = [at("2026-10-24T08:00"), at("2026-10-24T09:01"), at("2026-10-23T04:12"), at("2026-10-23T09:00")]
        future = projected(snaps, date(2026, 10, 25))
        self.assertEqual([f"{s.time:%d %H:%M}" for s in future], ["25 04:12", "25 08:00", "25 09:01"])
        self.assertTrue(all(s.id is None for s in future))
        self.assertEqual(projected(snaps, date(2026, 10, 23)), [])


if __name__ == "__main__":
    unittest.main()
