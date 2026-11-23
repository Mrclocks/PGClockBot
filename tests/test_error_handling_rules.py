"""Failures that are tolerated are logged, and a translated error keeps its cause (AGENTS.md §6)."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.api import login_guard
from app.services import secret_box
from app.services.principal_lifecycle import PrincipalLifecycleError, reject_hierarchy_mutation


class LoginLockWithoutFlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, value in (("DATA_DIR", Path(self.tmp.name)),
                            ("_LOGIN_LOCK_FILE", Path(self.tmp.name) / "login_lockouts.json")):
            patcher = patch.object(login_guard, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_filesystem_without_flock_still_counts_failures_and_says_so(self) -> None:
        with patch("fcntl.flock", side_effect=OSError("flock not supported")), \
                self.assertLogs(login_guard.logger, logging.WARNING) as logs:
            login_guard.login_fail("203.0.113.9")
        self.assertIn("lock unavailable", logs.output[0])
        self.assertTrue((Path(self.tmp.name) / "login_lockouts.json").is_file())


class DecryptSecretTests(unittest.TestCase):
    def test_a_secret_under_the_legacy_key_still_decrypts(self) -> None:
        current, legacy = secret_box._fernet_keys()[:2]
        token = secret_box._fernet(legacy).encrypt(b"panel-pass").decode("ascii")
        self.assertNotEqual(current, legacy)
        self.assertEqual(secret_box.decrypt_secret(token), "panel-pass")

    def test_a_token_no_key_opens_gives_none_and_a_warning(self) -> None:
        with self.assertLogs(secret_box.logger, logging.WARNING):
            self.assertIsNone(secret_box.decrypt_secret("gAAAAAnot-a-real-token"))
        with self.assertLogs(secret_box.logger, logging.WARNING):
            self.assertIsNone(secret_box.decrypt_secret("نامعتبر"))


class TranslatedErrorCauseTests(unittest.TestCase):
    def test_refusing_a_hierarchy_change_keeps_why_the_actor_was_refused(self) -> None:
        with self.assertRaises(PrincipalLifecycleError) as caught:
            asyncio.run(reject_hierarchy_mutation({"role": "reseller"}, parent_id=1, depth=1))
        self.assertEqual(caught.exception.code, "hierarchy_immutable")
        self.assertIsInstance(caught.exception.__cause__, PrincipalLifecycleError)
        self.assertEqual(caught.exception.__cause__.code, "not_owner")


if __name__ == "__main__":
    unittest.main()
