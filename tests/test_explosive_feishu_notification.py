import os
import tempfile
import unittest
from unittest.mock import patch

import shared.db as db
import trader.notifications as notifications
from trader.notifications import ExplosiveFeishuNotifier


class ImmediateThread:
    def __init__(self, *, target, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"code": 0}


def explosive_result():
    return {
        "action": "open",
        "event_type": "explosive_breakout",
        "setup_id": "BTRUSDT:LONG:2026-08-26T02:30:00Z",
        "symbol": "BTRUSDT",
        "position_side": "LONG",
        "quantity": 1813,
        "entry_price": 0.03655,
        "score": 91.44,
        "status": "ok",
    }


class ExplosiveFeishuNotificationTest(unittest.TestCase):
    def test_one_explosive_setup_sends_exactly_three_messages(self):
        calls = []
        notifier = ExplosiveFeishuNotifier(
            webhook="https://open.feishu.cn/example",
            post=lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(),
            sleep=lambda seconds: None,
            thread_factory=ImmediateThread,
        )

        queued = notifier.notify(explosive_result())

        self.assertTrue(queued)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[1]["json"]["msg_type"] == "text" for call in calls))
        self.assertIn("BTRUSDT", calls[0][1]["json"]["content"]["text"])
        self.assertIn("1/3", calls[0][1]["json"]["content"]["text"])

    def test_duplicate_setup_is_not_sent_another_three_times(self):
        calls = []
        notifier = ExplosiveFeishuNotifier(
            webhook="https://open.feishu.cn/example",
            post=lambda *args, **kwargs: calls.append(1) or FakeResponse(),
            sleep=lambda seconds: None,
            thread_factory=ImmediateThread,
        )

        self.assertTrue(notifier.notify(explosive_result()))
        self.assertFalse(notifier.notify(explosive_result()))
        self.assertEqual(len(calls), 3)

    def test_missing_webhook_and_post_failures_do_not_raise(self):
        missing = ExplosiveFeishuNotifier(webhook="", thread_factory=ImmediateThread)
        self.assertFalse(missing.notify(explosive_result()))

        failing = ExplosiveFeishuNotifier(
            webhook="https://open.feishu.cn/example",
            post=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
            sleep=lambda seconds: None,
            thread_factory=ImmediateThread,
        )
        self.assertTrue(failing.notify(explosive_result()))

    def test_configured_notifier_reads_webhook_from_kv(self):
        previous_notifier = notifications._notifier
        previous_webhook = notifications._notifier_webhook
        notifications._notifier = None
        notifications._notifier_webhook = None
        try:
            with patch(
                "trader.notifications.get_trading_kv_value",
                return_value="https://open.feishu.cn/from-kv",
            ):
                notifier = notifications._configured_notifier()
            self.assertEqual(notifier.webhook, "https://open.feishu.cn/from-kv")
        finally:
            notifications._notifier = previous_notifier
            notifications._notifier_webhook = previous_webhook


class TradingKvTest(unittest.TestCase):
    def test_string_value_round_trip(self):
        original_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            db.DB_PATH = os.path.join(temp_dir, "kv.db")
            try:
                saved = db.set_trading_kv_value(
                    "feishu_explosive_webhook",
                    "https://open.feishu.cn/example",
                )
                self.assertEqual(saved, "https://open.feishu.cn/example")
                self.assertEqual(
                    db.get_trading_kv_value("feishu_explosive_webhook"),
                    "https://open.feishu.cn/example",
                )
            finally:
                db.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
