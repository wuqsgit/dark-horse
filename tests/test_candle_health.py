import unittest

from pipeline.candle_health import retry_async


class RetryAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_retries_twice_after_initial_failure(self):
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError("temporary")
            return "ok"

        result = await retry_async(operation, retries=2, delay=0)

        self.assertEqual(result, "ok")
        self.assertEqual(attempts, 3)


if __name__ == "__main__":
    unittest.main()
