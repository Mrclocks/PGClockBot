"""Tests for restart helper + PWA + in-panel update wiring."""

from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ServiceControlApiTests(unittest.TestCase):
    def test_schedule_accepts_delay_and_returns_bool(self):
        from app.services import service_control as sc

        sig = inspect.signature(sc.schedule_panel_restart)
        self.assertIn("delay_sec", sig.parameters)
        self.assertIn("reason", sig.parameters)
        with mock.patch.object(sc, "ensure_restart_helper", return_value=(True, "ok")):
            with mock.patch.object(sc, "restart_panel_service", return_value=(True, "scheduled")):
                # reset lock
                with sc._restart_lock:
                    sc._restart_scheduled = False
                self.assertTrue(sc.schedule_panel_restart(delay_sec=1.0, reason="test"))


class PwaTests(unittest.TestCase):
    def test_manifest_defaults(self):
        from app.services.pwa import DEFAULT_NAME, build_manifest

        m = build_manifest({})
        self.assertEqual(m["name"], DEFAULT_NAME)
        self.assertEqual(m["start_url"], "/home")
        self.assertEqual(m["id"], "/home")
        self.assertEqual(m["display"], "standalone")
        self.assertTrue(any(i["src"].startswith("/pwa/icon/") for i in m["icons"]))
        from app.services.pwa import service_worker_js

        sw = service_worker_js()
        self.assertIn("pgclock-shell-v31", sw)
        self.assertIn("isVersionedPanelAsset", sw)
        self.assertIn("/static/fonts.css", sw)
        self.assertNotIn("self.clients.claim()", sw)
        self.assertNotIn("/pwa/icon/192", sw)

    def test_icon_from_logo(self):
        from app.services import pwa as pwa_mod

        logo = Path("app/web/static/logo.png")
        if not logo.is_file():
            self.skipTest("logo.png missing")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(pwa_mod, "PWA_DIR", Path(tmp)):
                data = pwa_mod.icon_bytes(192)
                self.assertGreater(len(data), 100)
                self.assertTrue(data[:8] == b"\x89PNG\r\n\x1a\n")


class UpdateWiringTests(unittest.TestCase):
    def test_update_api_enabled(self):
        src = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("start_update", src)
        self.assertIn("start_rollback", src)
        self.assertIn("start_rollback_to_version", src)
        self.assertNotIn("آپدیت از داخل پنل حذف شده است", src)
        tpl = Path("app/web/templates/_settings_update.html").read_text(encoding="utf-8")
        self.assertIn("/update/start", tpl)
        # Restart confirmation needs version/boot_id/pid — public /health is minimal
        self.assertIn("/health/detail", tpl)
        self.assertNotIn("fetch('/health?", tpl)
        self.assertNotIn('fetch("/health?', tpl)
        self.assertIn("upd-start", tpl)
        self.assertIn("rollback-version", tpl)
        self.assertIn("/update/rollback", tpl)

    def test_pwa_tab_wired(self):
        from app.services.users import PANEL_SETTINGS_TABS, SETTINGS_TABS, TAB_SETTING_GROUPS

        self.assertNotIn(("pwa", "وب‌اپ"), SETTINGS_TABS)
        self.assertIn(("pwa", "وب‌اپ"), PANEL_SETTINGS_TABS)
        self.assertEqual(TAB_SETTING_GROUPS.get("pwa"), [])
        self.assertTrue(Path("app/web/templates/_settings_pwa.html").is_file())
        self.assertTrue(Path("scripts/pgclockbot-ctl").is_file())
        sh = Path("pgclock.sh").read_text(encoding="utf-8")
        self.assertIn("install_restart_helper", sh)


if __name__ == "__main__":
    unittest.main()
