import unittest

from datetime import datetime, timedelta, timezone

from engine.run import (
    next_hourly_run,
    register_retention_job,
    register_startup_scoring_retry,
)


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def add_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class RetentionScheduleTest(unittest.TestCase):
    def test_next_hourly_run_never_schedules_in_the_past(self):
        before_slot = datetime(2026, 8, 3, 16, 9, 30, tzinfo=timezone.utc)
        after_slot = datetime(2026, 8, 3, 16, 17, 40, tzinfo=timezone.utc)

        self.assertEqual(
            next_hourly_run(before_slot),
            datetime(2026, 8, 3, 16, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(
            next_hourly_run(after_slot),
            datetime(2026, 8, 3, 17, 10, tzinfo=timezone.utc),
        )

    def test_startup_catchup_and_daily_cleanup_are_registered(self):
        scheduler = FakeScheduler()
        before = datetime.now(tz=timezone.utc)

        register_retention_job(scheduler)
        after = datetime.now(tz=timezone.utc)

        self.assertEqual(len(scheduler.calls), 2)
        jobs = {kwargs["id"]: kwargs for _, kwargs in scheduler.calls}
        self.assertEqual(jobs["startup_data_retention"]["trigger"], "date")
        self.assertGreaterEqual(
            jobs["startup_data_retention"]["run_date"],
            before + timedelta(minutes=20),
        )
        self.assertLessEqual(
            jobs["startup_data_retention"]["run_date"],
            after + timedelta(minutes=20),
        )
        self.assertEqual(jobs["daily_data_retention"]["trigger"], "cron")
        self.assertEqual(jobs["daily_data_retention"]["hour"], 3)
        self.assertEqual(jobs["daily_data_retention"]["minute"], 30)
        self.assertEqual(jobs["daily_data_retention"]["timezone"], "Asia/Shanghai")

    def test_startup_scoring_retry_waits_for_universe_publication(self):
        scheduler = FakeScheduler()
        startup = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)

        register_startup_scoring_retry(scheduler, startup)

        self.assertEqual(len(scheduler.calls), 1)
        _, job = scheduler.calls[0]
        self.assertEqual(job["id"], "startup_scoring_retry")
        self.assertEqual(job["trigger"], "date")
        self.assertEqual(
            job["run_date"],
            datetime(2026, 8, 16, 0, 0, 45, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
