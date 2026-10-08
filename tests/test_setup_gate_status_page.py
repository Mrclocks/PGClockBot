"""Setup-gate / panel status pages share the branded 404 look."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "app" / "api" / "app.py"
TPL = ROOT / "app" / "web" / "templates" / "panel_status.html"
CSS = ROOT / "app" / "web" / "static" / "panel.css"


class SetupGateStatusPageTests(unittest.TestCase):
    def test_setup_gate_uses_panel_status_not_inline_html(self):
        src = APP_PY.read_text(encoding="utf-8")
        self.assertIn("render_panel_status", src)
        self.assertIn("راه‌اندازی اولیه", src)
        self.assertIn("چطور توکن", src)
        self.assertIn("pgclock status", src)
        self.assertIn("bash pgclock.sh status", src)
        self.assertIn("data/setup_entry.url", src)
        self.assertIn("?gate=", src)
        # Old plain English wall removed
        self.assertNotIn("Setup link required", src)
        self.assertNotIn("First-run setup", src)
        self.assertNotIn("font-family:system-ui,sans-serif", src)

    def test_template_supports_token_guide(self):
        from jinja2 import Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=FileSystemLoader(str(ROOT / "app" / "web" / "templates")),
            autoescape=select_autoescape(["html", "xml"]),
        )
        html = env.get_template("panel_status.html").render(
            code=403,
            title="راه‌اندازی اولیه",
            message="برای امنیت…",
            ref=None,
            primary_href="",
            primary_label="",
            secondary_href="",
            secondary_label="",
            guide_heading="چطور توکن / لینک راه‌اندازی بگیرم؟",
            guide_steps=[
                {"title": "۱. به سرور وصل شوید", "body": "با SSH وارد شوید."},
                {
                    "title": "۲. وضعیت را چاپ کنید",
                    "body": "دستور زیر را اجرا کنید:",
                },
                {
                    "title": "۳. لینک را باز کنید",
                    "body": 'پارامتر <code dir="ltr">?gate=…</code> توکن شماست.',
                },
            ],
            code_block=(
                "pgclock status\n"
                "# اگر pgclock پیدا نشد:\n"
                "cd /path/to/PGClockBot && bash pgclock.sh status\n"
                "cat data/setup_entry.url"
            ),
            guide_note='از <code dir="ltr">127.0.0.1</code> بدون لینک.',
            footer="بعد از اتمام راه‌اندازی…",
            app_version="0.1.10",
            pwa_name="MrClockBot",
        )
        self.assertIn("panel-status--guided", html)
        self.assertIn("panel-status-guide", html)
        self.assertIn("چطور توکن", html)
        self.assertIn("pgclock status", html)
        self.assertIn("bash pgclock.sh status", html)
        self.assertIn("?gate=", html)
        self.assertIn('data-status="403"', html)
        self.assertIn("راه‌اندازی اولیه", html)

    def test_status_css_has_guide_and_403(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".panel-status-guide", css)
        self.assertIn(".panel-status-code", css)
        self.assertIn('.panel-status[data-status="403"]', css)
        self.assertIn(".panel-status--guided", css)

    def test_middleware_html_errors_use_panel_status(self):
        src = APP_PY.read_text(encoding="utf-8")
        self.assertNotIn('HTMLResponse("Request too large"', src)
        self.assertNotIn('HTMLResponse(label, status_code=403)', src)
        self.assertIn("درخواست خیلی بزرگ است", src)
        self.assertIn("درخواست امنیتی رد شد", src)


if __name__ == "__main__":
    unittest.main()
