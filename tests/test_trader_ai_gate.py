import unittest
from unittest.mock import patch


from trader.ai_client import (
    AIEntryQualityClient,
    apply_entry_quality_gate,
    build_candidate,
    build_learning_action,
    observe_entry_quality_candidates,
)


class FakeExchange:
    def adjust_quantity(self, symbol, quantity):
        return round(quantity, 3)


def open_action(symbol="B2USDT", source="alpha"):
    return {
        "action": "open",
        "symbol": symbol,
        "position_side": "LONG",
        "quantity": 1000.0,
        "entry_price": 0.5,
        "leverage": 3,
        "stop_pct": 0.10,
        "strategy_source": source,
        "entry_mode": "alpha_trend_probe" if source == "alpha" else "trend_confirmed",
        "score": 82.0,
    }


class TraderAIGateTest(unittest.TestCase):
    @patch("trader.ai_client.httpx.post")
    def test_batch_observation_has_a_realistic_timeout_budget(self, post):
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"received": 40, "created": 40}

        AIEntryQualityClient(timeout_seconds=0.3).observe_many([{"symbol": "B2USDT"}])

        self.assertGreaterEqual(post.call_args.kwargs["timeout"], 3.0)

    def test_candidate_flattens_scan_features_and_uses_separate_model(self):
        action = open_action(source="normal")
        rows = [{
            "symbol": "B2USDT",
            "composite_score": 84,
            "entry_alpha": 70,
            "relative_strength": 62,
            "raw_features": {
                "technical": {"trend_score": 76, "atr_ratio": 0.04},
                "futures": {"funding_rate": 0.0001},
                "depth": {"spread_pct": 0.0005},
            },
        }]

        candidate = build_candidate(action, rows, account_id=7)

        self.assertEqual(candidate["model_key"], "normal")
        self.assertEqual(candidate["account_id"], 7)
        self.assertEqual(candidate["features"]["trend_score"], 76)
        self.assertEqual(candidate["features"]["spread_pct"], 0.0005)
        self.assertEqual(candidate["features"]["score"], 82.0)

    def test_collecting_and_allow_keep_original_entry(self):
        actions = [open_action(), {"action": "close", "symbol": "ETHUSDT"}]
        for decision in ("collecting", "allow"):
            result = apply_entry_quality_gate(
                actions, [], balance=5000, exchange=FakeExchange(), account_id=1,
                evaluate=lambda candidate, d=decision: {"status": "collecting" if d == "collecting" else "live", "decision": d},
            )
            self.assertEqual(len(result), 2)
            self.assertEqual(result[0]["quantity"], actions[0]["quantity"])
            self.assertEqual(result[0]["ai_quality_decision"], decision)
            self.assertEqual(result[1], actions[1])

    def test_reject_removes_only_new_entry(self):
        close = {"action": "close", "symbol": "ETHUSDT"}
        result = apply_entry_quality_gate(
            [open_action(), close], [], balance=5000, exchange=FakeExchange(), account_id=1,
            evaluate=lambda candidate: {"status": "live", "decision": "reject", "quality_score": 42},
        )
        self.assertEqual(result, [close])

    def test_probe_resizes_entry_to_five_percent_current_balance_margin(self):
        action = open_action()
        action["quantity"] = 2000.0
        result = apply_entry_quality_gate(
            [action], [], balance=5000, exchange=FakeExchange(), account_id=1,
            evaluate=lambda candidate: {
                "status": "live", "decision": "probe", "quality_score": 58,
                "target_margin_pct": 0.05,
            },
        )

        action = result[0]
        self.assertEqual(action["quantity"], 1500.0)  # 5000 * 5% * 3 / 0.5
        self.assertEqual(action["invested"], 750.0)
        self.assertEqual(action["ai_quality_score"], 58)

    def test_ai_failure_blocks_open_but_keeps_position_management(self):
        close = {"action": "partial_close", "symbol": "ETHUSDT"}

        def unavailable(candidate):
            raise TimeoutError("AI timeout")

        result = apply_entry_quality_gate(
            [open_action(), close], [], balance=5000, exchange=FakeExchange(), account_id=1,
            evaluate=unavailable,
        )
        self.assertEqual(result, [close])

    def test_candidate_observation_is_batched_and_does_not_change_actions(self):
        observed = []
        actions = [open_action(), open_action("ETHUSDT", "normal")]

        result = observe_entry_quality_candidates(
            actions, [], account_id=7, observe=lambda payload: observed.extend(payload),
        )

        self.assertEqual(result["sent"], 2)
        self.assertEqual(len(observed), 2)
        self.assertEqual(observed[0]["account_id"], 7)
        self.assertEqual(observed[0]["template"], "alpha_entry")
        self.assertEqual(observed[1]["template"], "normal_entry")

    def test_candidate_observation_failure_is_non_blocking(self):
        result = observe_entry_quality_candidates(
            [open_action()], [], account_id=1,
            observe=lambda payload: (_ for _ in ()).throw(TimeoutError("offline")),
        )
        self.assertEqual(result, {"sent": 0, "error": "offline"})

    def test_learning_action_uses_candidate_features_before_legacy_gates(self):
        action = build_learning_action(
            {
                "symbol": "THEUSDT", "market_price": 0.42, "composite_score": 77,
                "raw_features": {"technical": {"atr_ratio": 0.04}},
            },
            side="LONG", strategy_source="normal", category="discovery",
        )

        self.assertEqual(action["symbol"], "THEUSDT")
        self.assertEqual(action["stop_pct"], 0.08)
        self.assertEqual(action["ai_sample_template"], "normal_entry")

    def test_learning_action_rejects_missing_direction_or_price(self):
        self.assertIsNone(build_learning_action({"symbol": "X", "market_price": 1}, side=None))
        self.assertIsNone(build_learning_action({"symbol": "X", "market_price": 0}, side="LONG"))


if __name__ == "__main__":
    unittest.main()
