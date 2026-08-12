import unittest
from unittest.mock import patch

from shared.directional_scoring import compute_short_entry_alpha
from trader.entry_profiles import evaluate_profile_entry
from trader.execution import _market_phase_entry_decision, _score_layer_gate
from trader.risk import (
    determine_side,
    evaluate_short_setup,
    funding_position_factor,
)
from trader.roll_policy import calculate_roll_quantity, evaluate_roll
from trader.selection import CandidateSelector


def short_row(symbol="SHORTUSDT", *, rsi=38, ret_6h=-0.03, ret_24h=-0.05):
    technical = {
        "trend_direction": "向下",
        "chip_phase": "筹码松动",
        "price_position": "中位",
        "return_1h": -0.01,
        "return_6h": ret_6h,
        "return_24h": ret_24h,
        "volume_change_pct": 1.6,
        "rsi_14": rsi,
        "ema20": 95,
        "ema20_slope": -0.5,
        "current_price": 90,
    }
    futures = {"funding_rate": 0.0012, "oi_change_pct": 0.02}
    depth = {"depth_ratio": 0.8}
    phase = {"phase": "distribution"}
    short_alpha = compute_short_entry_alpha(
        technical, futures, depth, phase, relative_strength=20
    )
    return {
        "symbol": symbol,
        "composite_score": 52,
        "entry_alpha": 30,
        "relative_strength": 20,
        "trend_direction": "向下",
        "chip_phase": "筹码松动",
        "price_position": "中位",
        "volatility_level": "正常",
        "market_price": 90,
        "raw_features": {
            "technical": technical,
            "futures": futures,
            "depth": depth,
            "phase": phase,
            "short_entry_alpha": short_alpha,
        },
    }


class ShortTradingStrategyTest(unittest.TestCase):
    def test_short_score_and_direction_use_bearish_features(self):
        row = short_row()

        self.assertGreaterEqual(row["raw_features"]["short_entry_alpha"], 72)
        self.assertEqual(determine_side(row), "SHORT")
        self.assertTrue(evaluate_short_setup(row)["eligible"])

    def test_oversold_move_is_not_chased(self):
        row = short_row(rsi=20, ret_6h=-0.10, ret_24h=-0.15)

        setup = evaluate_short_setup(row)

        self.assertTrue(setup["anti_chase"])
        self.assertFalse(setup["eligible"])
        self.assertIsNone(determine_side(row))

    def test_short_direction_forces_short_template(self):
        row = short_row()
        profile = evaluate_profile_entry(
            row,
            {
                "breakout": {"ok": False, "volume_ratio": 1.6},
                "rr": {"rr_used": 2.0},
                "cooldown": {},
            },
            "SHORT",
        )

        self.assertEqual(profile["template"], "short_breakdown")
        self.assertEqual(profile["status"], "pass")
        self.assertEqual(profile["metrics"]["short_setup"]["setup"], "failed_rebound")

    def test_candidate_selector_reserves_a_valid_short(self):
        short = short_row()
        long = {
            **short_row("LONGUSDT"),
            "composite_score": 90,
            "entry_alpha": 90,
            "relative_strength": 90,
            "trend_direction": "向上",
            "chip_phase": "中性震荡",
            "price_position": "偏低",
        }
        long["raw_features"] = {
            "technical": {
                "trend_direction": "向上",
                "return_6h": 0.03,
                "return_24h": 0.05,
            },
            "futures": {},
            "depth": {"depth_ratio": 1.1},
        }
        with patch.object(CandidateSelector, "_load_blacklist", return_value=set()), \
             patch.object(CandidateSelector, "_load_token_map", return_value={}), \
             patch.object(CandidateSelector, "_liquidity_score", return_value=80), \
             patch.object(CandidateSelector, "_get_historical_performance", return_value={
                 "total": 0, "win_rate": 50, "total_pnl": 0,
                 "profit_factor": 1, "expectancy": 0,
             }), \
             patch("trader.selection.get_symbol_risk", side_effect=lambda symbol: {
                 "class": "narrative" if symbol == "SHORTUSDT" else "large_cap",
                 "max_position_factor": 0.5,
             }):
            selected = CandidateSelector().select_candidates([long, short], [], 2)

        self.assertEqual(selected[0]["symbol"], "SHORTUSDT")
        self.assertEqual(selected[0]["candidate_side"], "SHORT")

    def test_short_score_layer_ignores_long_opportunity_layers(self):
        row = {
            "raw_features": {
                "score_layers": {
                    "layers": {
                        "opportunity": {"score": 10},
                        "entry": {"score": 10},
                        "risk": {"score": 20},
                        "execution": {"score": 80},
                    },
                    "thresholds": {
                        "min_opportunity_score": 60,
                        "min_entry_score": 60,
                        "max_risk_score": 70,
                        "min_execution_score": 50,
                    },
                }
            }
        }

        ok, reason, _ = _score_layer_gate(row, {"status": "pass"}, "SHORT")

        self.assertTrue(ok, reason)

    def test_breakdown_market_phase_supports_short_only(self):
        long_ok, _, _ = _market_phase_entry_decision(
            {"phase": "breakdown_risk"}, "pass", "LONG"
        )
        short_ok, _, reason = _market_phase_entry_decision(
            {"phase": "breakdown_risk"}, "pass", "SHORT"
        )

        self.assertFalse(long_ok)
        self.assertTrue(short_ok)
        self.assertIn("supports_short", reason)

    def test_negative_funding_reduces_then_blocks_short(self):
        self.assertEqual(funding_position_factor(-0.0015, "SHORT"), 0.5)
        self.assertEqual(funding_position_factor(-0.0031, "SHORT"), 0.0)
        self.assertEqual(funding_position_factor(0.0015, "SHORT"), 1.0)

    def test_short_roll_uses_two_layers_and_larger_adds(self):
        config = {
            "max_layers": 3,
            "short_max_layers": 2,
            "trigger_r": 1.5,
            "short_layer_add_initial_qty_pct": [0.5, 0.35],
            "short_max_total_qty_multiple": 1.5,
        }
        quantity = calculate_roll_quantity(
            10,
            {"step_size": 0.1, "min_qty": 0.1, "min_notional": 1},
            80,
            config,
            roll_layer=1,
            current_quantity=10,
            side="SHORT",
        )
        decision = evaluate_roll(
            {"side": "SHORT", "entry_price": 100, "mark_price": 80},
            {
                "initial_quantity": 10,
                "initial_stop_loss": 110,
                "atr_value": 2,
                "tp1_hit": 1,
                "roll_layer": 2,
            },
            {"ema20": 90, "ema20_slope": -1},
            alpha_sync=True,
            config=config,
        )

        self.assertEqual(quantity, 5.0)
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.status, "roll_completed")


if __name__ == "__main__":
    unittest.main()
