import unittest

from trader.roll_policy import (
    calculate_protected_stop,
    calculate_roll_quantity,
    evaluate_roll,
    is_residual_position,
)
from trader.execution import _position_r_state


CONFIG = {
    "max_layers": 3,
    "trigger_r": 1.5,
    "layer_trigger_r": [1.5, 2.5, 3.5],
    "add_initial_qty_pct": 0.25,
    "layer_add_initial_qty_pct": [0.25, 0.20, 0.15],
    "max_total_qty_multiple": 1.1,
    "repeat_pullback_atr": 0.75,
    "repeat_recover_to_peak_atr": 0.25,
    "repeat_min_r": 1.0,
    "break_even_buffer_pct": 0.0015,
    "min_remaining_margin": 5.0,
    "min_notional_multiplier": 1.5,
}


def complete_state(**overrides):
    state = {
        "initial_quantity": 10.0,
        "initial_stop_loss": 95.0,
        "atr_value": 2.0,
        "tp1_hit": 1,
        "roll_layer": 0,
    }
    state.update(overrides)
    return state


class RollPolicyTest(unittest.TestCase):
    def test_incomplete_legacy_state_cannot_roll(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 110},
            {"tp1_hit": 1, "roll_layer": 0},
            {"ema20": 105, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "roll_state_incomplete")

    def test_tp1_must_be_confirmed(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 110},
            complete_state(tp1_hit=0),
            {"ema20": 105, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )
        self.assertEqual(decision.status, "waiting_tp1")

    def test_profit_must_reach_one_and_half_r(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 107},
            complete_state(),
            {"ema20": 105, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )
        self.assertAlmostEqual(decision.current_r, 1.4)
        self.assertEqual(decision.status, "waiting_1_5r")

    def test_explosive_confirmation_add_uses_lower_r_after_tp1(self):
        decision = evaluate_roll(
            {
                "side": "LONG",
                "entry_price": 100,
                "mark_price": 104,
                "strategy_source": "alpha",
            },
            complete_state(
                entry_reason="explosive_breakout alpha_volume_price",
                alpha_score=89,
            ),
            {"ema20": 102, "ema20_slope": 1.0},
            alpha_sync=True,
            config={**CONFIG, "explosive_trigger_r": 0.75},
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.trigger_mode, "explosive_confirmation")

    def test_trend_must_match_position_side(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 110},
            complete_state(),
            {"ema20": 111, "ema20_slope": -0.2},
            alpha_sync=True,
            config=CONFIG,
        )
        self.assertEqual(decision.status, "trend_not_confirmed")

    def test_alpha_requires_dual_market_sync(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 110, "strategy_source": "alpha"},
            complete_state(alpha_profile="futures_mapped"),
            {"ema20": 105, "ema20_slope": 1.0},
            alpha_sync=False,
            config=CONFIG,
        )
        self.assertEqual(decision.status, "alpha_not_synced")

    def test_roll_quantity_is_twenty_five_percent_of_initial_quantity(self):
        qty = calculate_roll_quantity(
            10,
            {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5},
            mark_price=100,
            config=CONFIG,
        )
        self.assertEqual(qty, 2.5)

    def test_repeat_roll_arms_after_healthy_pullback(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 110},
            complete_state(
                roll_layer=1,
                roll_price=108,
                roll_cycle_peak_price=112,
                roll_pullback_armed=0,
            ),
            {"ema20": 105, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )

        self.assertFalse(decision.eligible)
        self.assertTrue(decision.pullback_armed)
        self.assertEqual(decision.status, "waiting_pullback_recovery")

    def test_second_roll_can_follow_sustained_profit_without_pullback(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 113},
            complete_state(
                roll_layer=1,
                roll_price=108,
                roll_cycle_peak_price=113,
                roll_pullback_armed=0,
            ),
            {"ema20": 108, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.trigger_mode, "sustained_profit")

    def test_third_short_roll_can_follow_sustained_profit(self):
        decision = evaluate_roll(
            {"side": "SHORT", "entry_price": 100, "mark_price": 82},
            complete_state(
                initial_stop_loss=105,
                roll_layer=2,
                roll_price=88,
                roll_cycle_peak_price=82,
            ),
            {"ema20": 86, "ema20_slope": -1.0},
            alpha_sync=True,
            config=CONFIG,
        )

        self.assertTrue(decision.eligible)
        self.assertAlmostEqual(decision.current_r, 3.6)
        self.assertEqual(decision.trigger_mode, "sustained_profit")

    def test_repeat_roll_enters_after_pullback_recovers_near_peak(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 111.6},
            complete_state(
                roll_layer=1,
                roll_price=108,
                roll_cycle_peak_price=112,
                roll_pullback_armed=1,
            ),
            {"ema20": 108, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.status, "ready")
        self.assertEqual(decision.trigger_mode, "pullback_recovery")

    def test_repeat_roll_stops_at_configured_layer_limit(self):
        decision = evaluate_roll(
            {"side": "LONG", "entry_price": 100, "mark_price": 115},
            complete_state(roll_layer=3),
            {"ema20": 110, "ema20_slope": 1.0},
            alpha_sync=True,
            config=CONFIG,
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "roll_completed")

    def test_roll_quantity_decreases_by_layer_and_respects_total_cap(self):
        second = calculate_roll_quantity(
            10,
            {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5},
            mark_price=100,
            config=CONFIG,
            roll_layer=2,
            current_quantity=7,
        )
        capped_third = calculate_roll_quantity(
            10,
            {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5},
            mark_price=100,
            config=CONFIG,
            roll_layer=3,
            current_quantity=10.4,
        )

        self.assertEqual(second, 2.0)
        self.assertEqual(capped_third, 0.6)

    def test_protected_stop_includes_cost_buffer(self):
        self.assertAlmostEqual(calculate_protected_stop("LONG", 100, CONFIG), 100.15)
        self.assertAlmostEqual(calculate_protected_stop("SHORT", 100, CONFIG), 99.85)

    def test_small_remaining_margin_is_residual(self):
        self.assertTrue(is_residual_position(
            quantity=0.3,
            mark_price=3.77,
            leverage=2,
            exchange_info={"min_notional": 0.1},
            config=CONFIG,
        ))

    def test_small_remaining_notional_is_residual(self):
        self.assertTrue(is_residual_position(
            quantity=1,
            mark_price=6,
            leverage=1,
            exchange_info={"min_notional": 5},
            config=dict(CONFIG, min_remaining_margin=1),
        ))

    def test_rolled_position_uses_two_atr_trailing_stop_without_loosening(self):
        first = _position_r_state(
            "LONG", 100, 108,
            {
                "stop_pct": 0.05, "current_stop_loss": 101,
                "roll_layer": 1, "trailing_atr_multiplier": 2,
            },
            atr=2, highest_price=110,
        )
        second = _position_r_state(
            "LONG", 100, 107,
            {
                "stop_pct": 0.05, "current_stop_loss": first["current_stop_loss"],
                "roll_layer": 1, "trailing_atr_multiplier": 2,
            },
            atr=2, highest_price=109,
        )

        self.assertTrue(first["trailing_enabled"])
        self.assertEqual(first["current_stop_loss"], 106)
        self.assertEqual(second["current_stop_loss"], 106)


if __name__ == "__main__":
    unittest.main()
