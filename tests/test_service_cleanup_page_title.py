"""Service cleanup page title matches other shop/bot pages (brand orange trash icon)."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

from app.services.formatting import format_number

ROOT = Path(__file__).resolve().parents[1]


def _render() -> str:
    env = Environment(
        loader=ChoiceLoader(
            [
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(ROOT / "app/web/templates"),
            ]
        ),
        autoescape=select_autoescape(),
    )
    env.filters["num"] = format_number
    preview = SimpleNamespace(
        cache_note="",
        expired_total=0,
        volume_total=0,
        time_due_now=0,
        volume_due_now=0,
        time_due_soon=0,
        volume_due_soon=0,
        rows=[],
        exempt_rows=[],
    )
    policy = SimpleNamespace(time_on=False, time_days=7, volume_on=False, volume_days=30,
                             warn_first=3, warn_last=1)
    return env.get_template("service_cleanup.html").render(
        request=SimpleNamespace(query_params={}),
        csrf_token="t",
        preview=preview,
        policy=policy,
        save_action="/service-cleanup",
        min_days=1,
        max_days=365,
    )


class ServiceCleanupPageTitleTests(unittest.TestCase):
    def test_title_uses_brand_trash_icon_not_neutral(self) -> None:
        html = _render()
        self.assertIn('class="page-title"', html)
        self.assertRegex(html, r"<h1>پاکسازی سرویس‌ها</h1>")
        self.assertIn("page-title-ico", html)
        self.assertNotIn("page-title-ico--neutral", html)
        self.assertNotIn("page-title-ico--pg", html)
        # Trash path (lid + can), not the clock fallback from an unknown icon name.
        self.assertIn("M4 7h16", html)
        self.assertNotIn("M12 8v4l2.5 2.5", html)


if __name__ == "__main__":
    unittest.main()
