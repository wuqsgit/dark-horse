import unittest

from ai_service.labels import label_path


class AIQualityLabelsTest(unittest.TestCase):
    def test_long_is_positive_when_plus_one_r_is_reached_first(self):
        result = label_path(100.0, 0.05, "LONG", [
            {"time": "2026-07-14T10:00:00Z", "high": 103, "low": 99},
            {"time": "2026-07-14T10:15:00Z", "high": 106, "low": 101},
            {"time": "2026-07-14T10:30:00Z", "high": 107, "low": 94},
        ])
        self.assertEqual(result["label"], 1)
        self.assertEqual(result["first_event"], "plus_1r")
        self.assertAlmostEqual(result["mfe_r"], 1.4)
        self.assertAlmostEqual(result["mae_r"], -1.2)

    def test_same_candle_collision_is_conservatively_negative(self):
        result = label_path(100.0, 0.05, "LONG", [
            {"time": "2026-07-14T10:00:00Z", "high": 106, "low": 94},
        ])
        self.assertEqual(result["label"], 0)
        self.assertEqual(result["first_event"], "same_bar_stop_first")

    def test_short_direction_is_reversed(self):
        result = label_path(100.0, 0.05, "SHORT", [
            {"time": "2026-07-14T10:00:00Z", "high": 102, "low": 94},
        ])
        self.assertEqual(result["label"], 1)
        self.assertAlmostEqual(result["mfe_r"], 1.2)
        self.assertAlmostEqual(result["mae_r"], -0.4)

    def test_no_plus_one_r_within_window_is_negative(self):
        result = label_path(100.0, 0.05, "LONG", [
            {"time": "2026-07-14T10:00:00Z", "high": 103, "low": 98},
            {"time": "2026-07-14T10:15:00Z", "high": 104, "low": 97},
        ])
        self.assertEqual(result["label"], 0)
        self.assertEqual(result["first_event"], "no_plus_1r")


if __name__ == "__main__":
    unittest.main()
