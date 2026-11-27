"""Broadcast audience targeting — dual-role reseller receives «کاربران عادی»."""

from __future__ import annotations

import unittest
from pathlib import Path


class BroadcastAudienceContractTests(unittest.TestCase):
    def test_users_audience_includes_reseller_role(self):
        src = Path("app/services/broadcast.py").read_text(encoding="utf-8")
        block = src.split("async def list_broadcast_targets", 1)[1].split(
            "async def list_broadcast_history", 1
        )[0]
        self.assertIn("Role.USER.value", block)
        self.assertIn("Role.RESELLER.value", block)
        self.assertIn("role.in_", block)
        # Must not be USER-only filter for «کاربران عادی»
        self.assertNotIn(
            "q = q.where(BotUser.role == Role.USER.value)",
            block,
        )

    def test_reward_delete_label_not_archive(self):
        html = Path("app/web/templates/_loyalty_rewards_panel.html").read_text(
            encoding="utf-8"
        )
        self.assertRegex(html, r">(?:\{\{[^}]*\}\}|<svg[^>]*>.*?</svg>)?حذف</button>")
        self.assertIn('data-confirm-label="حذف"', html)
        self.assertNotIn(">بایگانی</button>", html)

    def test_payment_settings_stack_separates_pending_box(self):
        form = Path("app/web/templates/_domain_settings_form.html").read_text(
            encoding="utf-8"
        )
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("settings-domain-stack", form)
        self.assertIn(".settings-domain-stack", css)
        self.assertIn("gap: var(--section-gap)", css)


if __name__ == "__main__":
    unittest.main()
