import unittest

from datetime import datetime, timezone

from engine.run import next_hourly_run, register_retention_job


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

        register_retention_job(scheduler)

        self.assertEqual(len(scheduler.calls), 2)
        jobs = {kwargs["id"]: kwargs for _, kwargs in scheduler.calls}
        self.assertEqual(jobs["startup_data_retention"]["trigger"], "date")
        self.assertEqual(jobs["daily_data_retention"]["trigger"], "cron")
        self.assertEqual(jobs["daily_data_retention"]["hour"], 3)
        self.assertEqual(jobs["daily_data_retention"]["minute"], 30)
        self.assertEqual(jobs["daily_data_retention"]["timezone"], "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
