"""Switches whose background work is heavy are tagged «پر مصرف» with a one-line reason."""

from __future__ import annotations

import html
import re
import unittest

from app.api.app import templates
from app.services.resource_cost import HEAVY_LABEL, HEAVY_SETTINGS, heavy_setting_note
from app.services.users import SETTING_GROUPS


def _render_field(field: tuple, values: dict[str, str] | None = None) -> str:
    return templates.env.get_template("_settings_field.html").render(field=field, values=values or {})


def _field(key: str) -> tuple:
    return next(f for fields in SETTING_GROUPS.values() for f in fields if f[0] == key)


class HeavyRegistryTests(unittest.TestCase):
    def test_every_tagged_key_is_a_switch_shown_in_settings(self) -> None:
        for key in HEAVY_SETTINGS:
            with self.subTest(key=key):
                self.assertEqual(_field(key)[2], "toggle")

    def test_untagged_setting_has_no_note(self) -> None:
        self.assertEqual(heavy_setting_note("menu_order"), "")


class HeavySwitchRenderTests(unittest.TestCase):
    def test_heavy_switch_shows_the_tag_and_the_reason_linked_to_the_checkbox(self) -> None:
        key = "user_alert_low_enabled"
        out = html.unescape(_render_field(_field(key), {key: "1"}))
        self.assertIn(HEAVY_LABEL, out)
        self.assertIn(HEAVY_SETTINGS[key], out)
        checkbox = re.search(rf'<input type="checkbox" name="s_{key}"[^>]*>', out).group(0)
        self.assertIn(f'aria-describedby="heavy-note-{key}"', checkbox)
        self.assertIn(f'id="heavy-note-{key}"', out)

    def test_ordinary_switch_has_no_tag(self) -> None:
        out = _render_field(("one_tap_renew_enabled", "دکمهٔ تمدید", "toggle", ""))
        self.assertNotIn(HEAVY_LABEL, html.unescape(out))
        self.assertNotIn("aria-describedby", out)

    def test_risk_and_heavy_notes_are_both_described(self) -> None:
        key = "user_alert_low_enabled"
        env = templates.env.overlay()
        env.globals["setting_risk_notes"] = {key: "ریسک"}
        out = env.get_template("_settings_field.html").render(field=_field(key), values={})
        checkbox = re.search(rf'<input type="checkbox" name="s_{key}"[^>]*>', out).group(0)
        self.assertIn(f'aria-describedby="risk-note-{key} heavy-note-{key}"', checkbox)


if __name__ == "__main__":
    unittest.main()
