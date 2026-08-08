"""Guards for 2.6.12 panel/bot fixes."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class PaymentsShortcutTests(unittest.TestCase):
    def test_payments_link_opens_payment_tab(self):
        src = Path("app/web/templates/orders.html").read_text(encoding="utf-8")
        self.assertIn('href="/orders?tab=settings"', src)
        dash = Path("app/web/templates/dashboard.html").read_text(encoding="utf-8")
        self.assertIn('href="/orders?tab=payments"', dash)


class PlansModalTests(unittest.TestCase):
    def test_plans_use_modals(self):
        src = Path("app/web/templates/plans.html").read_text(encoding="utf-8")
        self.assertIn('data-modal-open="modal-plan-unified"', src)
        self.assertIn("modal-plan-unified", src)
        self.assertIn("ui-modal", src)
        self.assertIn("table-compact", src)
        # Legacy separate create/trial/custom modal buttons removed
        self.assertNotIn('data-modal-open="modal-plan-create"', src)
        self.assertNotIn('data-modal-open="modal-trial"', src)


class BroadcastHistoryTests(unittest.TestCase):
    def test_broadcast_has_delete_actions(self):
        src = Path("app/web/templates/broadcast.html").read_text(encoding="utf-8")
        self.assertIn("/broadcast/history/clear", src)
        self.assertIn("/broadcast/history/", src)
        api = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("broadcast_history_clear", api)
        self.assertIn("delete_broadcast_log", api)


class ForceJoinMultiTests(unittest.TestCase):
    def test_parse_channels(self):
        from app.services.users import parse_force_join_channels

        self.assertEqual(
            parse_force_join_channels("@a\n@b, @c\n@a"),
            ["@a", "@b", "@c"],
        )
        self.assertEqual(parse_force_join_channels(""), [])

    def test_check_all_missing(self):
        import asyncio
        from app.bot.middlewares import check_force_join_all, _FORCE_JOIN_MEMBER_CACHE

        _FORCE_JOIN_MEMBER_CACHE.clear()

        async def _run():
            with patch(
                "app.bot.middlewares.check_force_join_member",
                new=AsyncMock(side_effect=[True, False, True]),
            ):
                missing, unverified = await check_force_join_all(
                    AsyncMock(), 1, ["@a", "@b", "@c"]
                )
            self.assertEqual(missing, ["@b"])
            self.assertEqual(unverified, [])

        asyncio.run(_run())


class SettingsTabScrollTests(unittest.TestCase):
    def test_panel_js_scrolls_active_tab(self):
        src = Path("app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("scrollActiveTabIntoView", src)
        self.assertIn("a.active", src)


class BotListParityTests(unittest.TestCase):
    def test_admin_users_list_callback(self):
        src = Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn('adm:users:list:', src)
        self.assertIn('adm:resellers:list:', src)
        self.assertIn("USERS_PAGE_SIZE = 10", src)

    def test_reseller_customers_list(self):
        src = Path("app/bot/handlers/reseller.py").read_text(encoding="utf-8")
        self.assertIn('res:users:', src)
        self.assertIn("BotUser.reseller_id == owner_id", src)
        kb = Path("app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("مشتریان من", kb)

    def test_help_command_and_cancel_fallback(self):
        src = Path("app/bot/handlers/start.py").read_text(encoding="utf-8")
        self.assertIn('Command("help")', src)
        self.assertIn("orphan_cancel", src)
        self.assertIn("guide_text", src)


class UploadBoxThemeTests(unittest.TestCase):
    def test_upload_outer_body_color(self):
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn("\n.upload-box {\n  position: relative;", css)
        block = css.split("\n.upload-box {", 1)[1].split(".upload-box:hover", 1)[0]
        self.assertIn("background: var(--background);", block)
        img = css.split(".image-setting {", 1)[1].split("}", 1)[0]
        self.assertIn("background: var(--background);", img)


if __name__ == "__main__":
    unittest.main()
