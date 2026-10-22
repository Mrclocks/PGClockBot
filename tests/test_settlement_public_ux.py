"""Settlement return pages use the branded panel status stage."""

from __future__ import annotations

import unittest
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parents[1]


class SettlementStatusTemplateTests(unittest.TestCase):
    def render(self, **kwargs) -> str:
        env = Environment(
            loader=FileSystemLoader(str(ROOT / "app" / "web" / "templates")),
            autoescape=select_autoescape(["html", "xml"]),
        )
        base = {
            "code": "ok",
            "title": "پرداخت تأیید شد",
            "message": "می‌توانید به تلگرام برگردید.",
            "watermark": "OK",
            "kicker": "پرداخت",
            "ref": None,
            "primary_href": "",
            "primary_label": "",
            "secondary_href": "",
            "secondary_label": "",
            "guide_heading": "",
            "guide_steps": [],
            "code_block": "",
            "guide_note": "",
            "body_html": "",
            "footer": "این صفحه را ببندید و به تلگرام برگردید.",
            "app_version": "0.0.0",
            "pwa_name": "MrClockBot",
        }
        base.update(kwargs)
        return env.get_template("panel_status.html").render(**base)

    def test_success_tone_uses_ok_status(self) -> None:
        html = self.render()
        self.assertIn('data-status="ok"', html)
        self.assertIn("پرداخت تأیید شد", html)
        self.assertIn("می‌توانید به تلگرام برگردید", html)

    def test_warn_and_error_tones(self) -> None:
        warn = self.render(code="warn", title="در حال بررسی", watermark="WARN")
        err = self.render(code="error", title="ناموفق", watermark="ERR")
        self.assertIn('data-status="warn"', warn)
        self.assertIn('data-status="error"', err)


if __name__ == "__main__":
    unittest.main()
