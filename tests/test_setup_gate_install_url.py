"""Setup gate URL at install / first-run."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.security_policy import is_public_ip


class SetupEntryUrlTests(unittest.TestCase):
    def test_build_setup_entry_url_includes_gate(self):
        from app.services import setup_wizard as sw

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with (
                patch.object(sw, "DATA_DIR", data),
                patch.object(sw, "SETUP_GATE_FILE", data / "setup_gate.token"),
                patch.object(sw, "SETUP_GATE_META_FILE", data / "setup_gate.json"),
                patch.object(sw, "SETUP_FLAG", data / "setup_complete.flag"),
                patch.object(sw, "is_setup_complete", return_value=False),
                patch.object(sw, "default_panel_base_url", return_value="http://10.0.0.5:9000"),
            ):
                url = sw.build_setup_entry_url()
                self.assertIn("?gate=", url)
                self.assertTrue(url.startswith("http://10.0.0.5:9000/?gate="))

    def test_persist_writes_entry_file(self):
        from app.services import setup_wizard as sw

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            entry = data / "setup_entry.url"
            with (
                patch.object(sw, "DATA_DIR", data),
                patch.object(sw, "SETUP_GATE_FILE", data / "setup_gate.token"),
                patch.object(sw, "SETUP_GATE_META_FILE", data / "setup_gate.json"),
                patch.object(sw, "SETUP_ENTRY_FILE", entry),
                patch.object(sw, "SETUP_FLAG", data / "setup_complete.flag"),
                patch.object(sw, "is_setup_complete", return_value=False),
                patch.object(sw, "default_panel_base_url", return_value="http://srv:9000"),
            ):
                written = sw.persist_setup_entry_url()
                self.assertEqual(entry.read_text(encoding="utf-8").strip(), written)
                self.assertIn("?gate=", written)


class LocalClientTests(unittest.TestCase):
    def test_loopback_is_local(self):
        from app.services.setup_wizard import is_local_setup_client

        self.assertTrue(is_local_setup_client("127.0.0.1"))
        self.assertTrue(is_local_setup_client("::1"))
        self.assertFalse(is_public_ip("127.0.0.1"))

    def test_public_is_not_local(self):
        from app.services.setup_wizard import is_local_setup_client

        self.assertFalse(is_local_setup_client("8.8.8.8"))

    def test_private_lan_is_not_local(self):
        from app.services.setup_wizard import is_local_setup_client

        # VPC/LAN peers must present the one-time gate token
        self.assertFalse(is_local_setup_client("10.0.0.5"))
        self.assertFalse(is_local_setup_client("192.168.1.20"))


class PgclockInstallHintTests(unittest.TestCase):
    def test_install_prints_setup_entry_url_helper(self):
        src = Path("pgclock.sh").read_text(encoding="utf-8")
        self.assertIn("setup_wizard_url", src)
        # Reuse active gate on reprint/status — do not mint a fresh token every call.
        self.assertIn("ensure_setup_gate_token", src)
        self.assertIn("build_setup_entry_url", src)
        self.assertIn("setup_gate.json", src)
        self.assertIn("Setup URL (one-time, 15 min)", src)
        self.assertIn('print_success "Install complete"', src)
        self.assertIn("?gate=", src)
        # Must not call is_setup_complete() when minting install URL (auto-flag side effect).
        self.assertIn("Do NOT call is_setup_complete()", src)
        self.assertIn("ensure_public_web_host", src)
        self.assertIn('WEB_HOST="0.0.0.0"', src)
        # status must print HTTPS base when TLS is live (same as SUCCESS banner).
        self.assertIn('setup_wizard_url "$(panel_public_base_url)/"', src)
        # Install finish copy must stay English (CLI is English).
        self.assertNotIn("لینک یک‌بارمصرف", src)
        self.assertNotIn("فایروال ابری", src)

    def test_banner_shows_dynamic_release_version(self):
        src = Path("pgclock.sh").read_text(encoding="utf-8")
        self.assertIn("read_app_version", src)
        self.assertIn("Release v", src)
        self.assertNotIn("One command for everything", src)


class SetupGateTtlTests(unittest.TestCase):
    def test_gate_expires_after_fifteen_minutes(self):
        from app.services import setup_wizard as sw

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            t0 = 1_700_000_000.0
            with (
                patch.object(sw, "DATA_DIR", data),
                patch.object(sw, "SETUP_GATE_FILE", data / "setup_gate.token"),
                patch.object(sw, "SETUP_GATE_META_FILE", data / "setup_gate.json"),
                patch.object(sw, "SETUP_ENTRY_FILE", data / "setup_entry.url"),
                patch.object(sw, "SETUP_FLAG", data / "setup_complete.flag"),
                patch.object(sw, "is_setup_complete", return_value=False),
                patch.object(sw, "time") as mock_time,
            ):
                mock_time.time.return_value = t0
                token = sw.create_setup_gate_session()
                self.assertTrue(sw.setup_gate_ok(token))
                mock_time.time.return_value = t0 + sw.SETUP_GATE_TTL_SEC + 1
                self.assertFalse(sw.setup_gate_ok(token))

    def test_ensure_reuses_active_token_without_rotating(self):
        from app.services import setup_wizard as sw

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            with (
                patch.object(sw, "DATA_DIR", data),
                patch.object(sw, "SETUP_GATE_FILE", data / "setup_gate.token"),
                patch.object(sw, "SETUP_GATE_META_FILE", data / "setup_gate.json"),
                patch.object(sw, "SETUP_ENTRY_FILE", data / "setup_entry.url"),
                patch.object(sw, "SETUP_FLAG", data / "setup_complete.flag"),
                patch.object(sw, "is_setup_complete", return_value=False),
            ):
                first = sw.ensure_setup_gate_token()
                second = sw.ensure_setup_gate_token()
                self.assertEqual(first, second)
                self.assertTrue(sw.setup_gate_ok(first))
                # First open still rotates the URL token; cookie keeps the new one.
                rotated = sw.rotate_setup_gate_token()
                self.assertNotEqual(rotated, first)
                self.assertFalse(sw.setup_gate_ok(first))
                self.assertTrue(sw.setup_gate_ok(rotated))

    def test_mark_setup_complete_revokes_gate(self):
        from app.services import setup_wizard as sw

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            flag = data / "setup_complete.flag"
            with (
                patch.object(sw, "DATA_DIR", data),
                patch.object(sw, "SETUP_FLAG", flag),
                patch.object(sw, "SETUP_IN_PROGRESS", data / "setup_in_progress.flag"),
                patch.object(sw, "SETUP_GATE_FILE", data / "setup_gate.token"),
                patch.object(sw, "SETUP_GATE_META_FILE", data / "setup_gate.json"),
                patch.object(sw, "SETUP_ENTRY_FILE", data / "setup_entry.url"),
            ):
                token = sw.create_setup_gate_session()
                sw.persist_setup_entry_url("http://srv:9000")
                sw.mark_setup_complete()
                self.assertTrue(flag.is_file())
                self.assertFalse(sw.SETUP_GATE_META_FILE.exists())
                self.assertFalse(sw.setup_gate_ok(token))
                self.assertIsNone(sw.read_setup_entry_url())


if __name__ == "__main__":
    unittest.main()
