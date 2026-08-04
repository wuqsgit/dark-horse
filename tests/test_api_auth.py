import unittest
from unittest.mock import patch

from fastapi import HTTPException

import api.main as api_main


class APIAuthorizationTest(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_requires_configured_admin_token(self):
        with patch.object(api_main, "_admin_token", ""):
            with self.assertRaises(HTTPException) as raised:
                await api_main.require_admin(None)
        self.assertEqual(raised.exception.status_code, 503)

    async def test_mutation_rejects_wrong_token(self):
        with patch.object(api_main, "_admin_token", "correct-token"):
            with self.assertRaises(HTTPException) as raised:
                await api_main.require_admin("wrong-token")
        self.assertEqual(raised.exception.status_code, 401)

    async def test_mutation_accepts_matching_token(self):
        with patch.object(api_main, "_admin_token", "correct-token"):
            user = await api_main.require_admin("correct-token")
        self.assertEqual(user, "admin")


if __name__ == "__main__":
    unittest.main()
