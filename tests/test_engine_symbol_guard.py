import unittest

import engine.run as engine_run


class EngineSymbolGuardTest(unittest.TestCase):
    def setUp(self):
        engine_run._consecutive_empty_symbol_runs = 0

    def tearDown(self):
        engine_run._consecutive_empty_symbol_runs = 0

    def test_empty_universe_requires_two_consecutive_runs_to_degrade(self):
        first = engine_run._register_symbol_count(0)
        second = engine_run._register_symbol_count(0)

        self.assertLess(first, engine_run.EMPTY_SYMBOL_DEGRADE_AFTER)
        self.assertGreaterEqual(second, engine_run.EMPTY_SYMBOL_DEGRADE_AFTER)

    def test_healthy_universe_resets_empty_run_counter(self):
        engine_run._register_symbol_count(0)
        healthy = engine_run._register_symbol_count(146)
        next_empty = engine_run._register_symbol_count(0)

        self.assertEqual(healthy, 0)
        self.assertEqual(next_empty, 1)


if __name__ == "__main__":
    unittest.main()
