import unittest
from unittest.mock import patch

from ai_service.model import XGBoostBackend


class AIModelBackendTest(unittest.TestCase):
    def test_backend_can_start_collecting_before_optional_runtime_is_loaded(self):
        with patch("builtins.__import__", side_effect=ImportError("xgboost missing")):
            backend = XGBoostBackend()
        self.assertIsNotNone(backend)


if __name__ == "__main__":
    unittest.main()
