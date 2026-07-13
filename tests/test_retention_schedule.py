import unittest

from engine.run import register_retention_job


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def add_job(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class RetentionScheduleTest(unittest.TestCase):
    def test_daily_cleanup_is_registered_once_at_0330(self):
        scheduler = FakeScheduler()

        register_retention_job(scheduler)

        self.assertEqual(len(scheduler.calls), 1)
        _, kwargs = scheduler.calls[0]
        self.assertEqual(kwargs["trigger"], "cron")
        self.assertEqual(kwargs["hour"], 3)
        self.assertEqual(kwargs["minute"], 30)
        self.assertEqual(kwargs["timezone"], "Asia/Shanghai")
        self.assertEqual(kwargs["id"], "daily_data_retention")


if __name__ == "__main__":
    unittest.main()
