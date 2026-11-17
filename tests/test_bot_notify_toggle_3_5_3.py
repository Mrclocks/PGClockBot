"""3.5.3 — Fix admin bot notify toggles rejecting valid notify_* keys."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class AdminNotifyToggleAllowlistTests(unittest.TestCase):
    def test_all_notify_prefs_are_toggleable(self):
        from app.bot.handlers.admin_settings import (
            EXTRA_TOGGLE_KEYS,
            NOTIFY_KEYS,
            _is_toggleable_setting_key,
        )
        from app.services.notifications import NOTIFY_PREFS

        for key, *_ in NOTIFY_PREFS:
            self.assertIn(key, NOTIFY_KEYS)
            self.assertIn(key, EXTRA_TOGGLE_KEYS)
            self.assertTrue(
                _is_toggleable_setting_key(key),
                msg=f"{key} must be toggleable (was rejecting with کلید نامعتبر)",
            )

    def test_field_toggles_and_extras_allowed(self):
        from app.bot.handlers.admin_settings import (
            FIELDS,
            _is_toggleable_setting_key,
        )

        self.assertTrue(_is_toggleable_setting_key("trial_enabled"))
        self.assertTrue(_is_toggleable_setting_key("custom_plan_enabled"))
        self.assertTrue(_is_toggleable_setting_key("pay_wallet_enabled"))
        self.assertTrue(_is_toggleable_setting_key("qr_enabled"))
        # Text fields must not flip via tog handler
        text_keys = [k for k, meta in FIELDS.items() if meta[2] != "toggle"]
        self.assertTrue(text_keys)
        self.assertFalse(_is_toggleable_setting_key(text_keys[0]))
        self.assertFalse(_is_toggleable_setting_key("not_a_real_key"))

    def test_rendered_tog_callbacks_covered(self):
        """Every key used with adm:st:tog must pass the allowlist."""
        from app.bot.handlers.admin_settings import SECTIONS, _is_toggleable_setting_key
        from app.services.notifications import NOTIFY_PREFS

        keys: set[str] = {"custom_plan_enabled", "trial_enabled"}
        for key, *_ in NOTIFY_PREFS:
            keys.add(key)
        for sec in SECTIONS.values():
            for sub in sec.get("subs") or []:
                payload = sub[2]
                if isinstance(payload, list):
                    for f in payload:
                        if f[2] == "toggle":
                            keys.add(f[0])
        for key in keys:
            self.assertTrue(_is_toggleable_setting_key(key), msg=key)


class ResellerNotifyToggleTests(unittest.TestCase):

    def test_no_orphan_tog_literals(self):
        """Scan bot handlers for adm:st:tog:literal not in allowlist."""
        from app.bot.handlers.admin_settings import _is_toggleable_setting_key

        for path in (ROOT / "app/bot/handlers").glob("*.py"):
            text = path.read_text(encoding="utf-8")
            for key in re.findall(r"adm:st:tog:([a-z0-9_]+)", text):
                self.assertTrue(
                    _is_toggleable_setting_key(key),
                    msg=f"{path.name} renders adm:st:tog:{key} but allowlist rejects it",
                )


if __name__ == "__main__":
    unittest.main()
