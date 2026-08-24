import unittest
import tempfile
from pathlib import Path

from minute_pipeline.main import (
    MinutePipelineAlreadyRunning,
    acquire_instance_lock,
)


class MinutePipelineSingletonTest(unittest.TestCase):
    def test_second_process_lock_is_rejected_until_first_is_released(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "minute_pipeline.lock"
            first = acquire_instance_lock(lock_path)
            try:
                with self.assertRaises(MinutePipelineAlreadyRunning):
                    acquire_instance_lock(lock_path)
            finally:
                first.close()

            replacement = acquire_instance_lock(lock_path)
            replacement.close()


if __name__ == "__main__":
    unittest.main()
