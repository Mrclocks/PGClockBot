"""v4.3.1 — reseller/user modal cleanup, dual notify, pending-order purge."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]


class VersionTests(unittest.TestCase):
    def test_version_4_3_1(self):
        from app.version import __version__

        self.assertGreaterEqual(
            tuple(int(x) for x in __version__.split(".")[:3]), (0, 1, 0)
        )
        notes = (ROOT / "app/services/release_notes.py").read_text(encoding="utf-8")
        self.assertIn('"0.1.0"', notes)


class BotResellerServicesTests(unittest.TestCase):
    def test_reseller_list_opens_reseller_view(self):
        src = (ROOT / "app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn('callback_data=f"adm:resellers:view:{u.id}"', src)
        self.assertIn("adm:resellers:svcs:", src)
        self.assertIn("admin_reseller_actions", src)

    def test_keyboards_have_reseller_pg_services(self):
        src = (ROOT / "app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("def admin_reseller_actions", src)
        self.assertIn("adm:resellers:svcs:", src)
        self.assertIn("سرویس‌های پاسارگارد", src)


class UsersListFilterTests(unittest.TestCase):
    def test_users_page_excludes_pure_resellers(self):
        # This filter was later extracted out of app.py into a reusable
        # pure_reseller_clause() in the users_ops service module.
        src = (ROOT / "app/services/users_ops.py").read_text(encoding="utf-8")
        self.assertIn("pure_reseller", src)
        self.assertIn("has_shop_service", src)
        self.assertIn("not_(pure_reseller_clause())", src)


class PendingOrderCleanupTests(unittest.TestCase):
    def test_settings_keys(self):
        from app.services.users import DEFAULT_SETTINGS, TAB_SETTING_GROUPS, keys_for_tab

        self.assertIn("pending_order_cleanup_enabled", DEFAULT_SETTINGS)
        self.assertIn("pending_order_ttl_hours", DEFAULT_SETTINGS)
        self.assertIn("pending_order_cleanup_enabled", keys_for_tab("cleanup"))
        self.assertIn("pending_order_approval_ttl_hours", keys_for_tab("cleanup"))
        self.assertEqual(TAB_SETTING_GROUPS["cleanup"], ["پاکسازی سفارش‌های معلق"])
        # The cleanup tab owns these keys; saving the payment tab must not touch them.
        self.assertNotIn("pending_order_cleanup_enabled", keys_for_tab("payment"))

    def test_cancel_helper_and_scheduler(self):
        orders = (ROOT / "app/services/orders.py").read_text(encoding="utf-8")
        self.assertIn("async def cancel_stale_pending_orders", orders)
        self.assertIn("OrderStatus.CANCELLED.value", orders)
        sched = (ROOT / "app/jobs/scheduler.py").read_text(encoding="utf-8")
        self.assertIn("pending_order_cleanup", sched)
        self.assertIn("cleanup_stale_pending_orders", sched)
        api = (ROOT / "app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("/settings/cancel-pending-orders", api)


class RoleAndReasonUiTests(unittest.TestCase):
    def test_users_list_no_role_form_delete_has_reason(self):
        users = (ROOT / "app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertNotIn("/users/{{ u.id }}/role", users)
        self.assertIn("/users/{{ u.id }}/block", users)
        self.assertIn("/users/{{ u.id }}/delete", users)
        delete = users.split("/users/{{ u.id }}/delete", 1)[1].split("</form>", 1)[0]
        self.assertIn("data-confirm-reason", delete)
        block = users.split("/users/{{ u.id }}/block", 1)[1].split("</form>", 1)[0]
        self.assertNotIn("data-confirm-reason", block)
        self.assertIn('data-modal-open="modal-user-edit"', users)
        self.assertNotIn("data-user-edit-open", users)
        # Flash rendered once in base.html (above title), not under page-head
        self.assertNotIn("{% if flash_ok %}<div class=\"flash ok\">{{ flash_ok }}</div>{% endif %}", users)

    def test_resellers_list_no_role_form(self):
        resellers = (ROOT / "app/web/templates/resellers.html").read_text(encoding="utf-8")
        self.assertNotIn("/resellers/{{ u.id }}/role", resellers)
        self.assertIn('data-modal-open="modal-reseller-edit"', resellers)
        # Delete actions keep reason; role is only in edit modal
        self.assertIn("data-confirm-reason", resellers)

    def test_role_lives_in_edit_bodies_without_reason(self):
        user_body = (ROOT / "app/web/templates/_user_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("/users/{{ user.id }}/role", user_body)
        user_role = user_body.split("/users/{{ user.id }}/role", 1)[1].split(
            "</form>", 1
        )[0]
        self.assertNotIn("data-confirm-reason", user_role)
        # Delete keeps reason; role form must not require one.
        self.assertIn("data-confirm-reason", user_body)
        reseller_body = (ROOT / "app/web/templates/_reseller_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("/resellers/{{ user.id }}/role", reseller_body)
        role = reseller_body.split("/resellers/{{ user.id }}/role", 1)[1].split(
            "</form>", 1
        )[0]
        self.assertNotIn("data-confirm-reason", role)
        self.assertIn("data-confirm-reason", reseller_body)

    def test_renew_plan_only_no_manual_days(self):
        body = (ROOT / "app/web/templates/_user_edit_body.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("تمدید", body)
        self.assertNotIn('name="days"', body)
        self.assertNotIn('name="data_limit_gb"', body)
        self.assertIn("افزایش", body)
        # Extend form uses num_stepper macro (renders name="extra_days" at runtime).
        self.assertIn("num_stepper('extra_days'", body)
        self.assertIn("num_stepper('extra_gb'", body)
        # Redesigned mobile-friendly service list
        self.assertIn("svc-list", body)
        self.assertIn("svc-item", body)
        self.assertNotIn("<table", body)


class DualNotifyTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_to_user_chat_tries_main_and_shop(self):
        from app.db.models import BotUser
        from app.services.notifications import _send_to_user_chat

        user = BotUser(
            id=7, telegram_id=555, full_name="U", role="user", username="u", reseller_id=3
        )
        session = AsyncMock()
        main_bot = MagicMock()
        main_bot.token = "MAIN"
        main_bot.send_message = AsyncMock()
        main_bot.session = MagicMock()
        main_bot.session.close = AsyncMock()
        shop_bot = MagicMock()
        shop_bot.token = "SHOP"
        shop_bot.send_message = AsyncMock()

        with patch("app.bot.create_bot", return_value=main_bot), patch(
            "app.services.reseller_bots.open_notify_bot_for_reseller",
            new=AsyncMock(return_value=(shop_bot, False)),
        ):
            ok = await _send_to_user_chat(session, user, "<b>hi</b>")
        self.assertTrue(ok)
        main_bot.send_message.assert_awaited()
        shop_bot.send_message.assert_awaited()


class UiSelectFixedMenuTests(unittest.TestCase):
    def test_fixed_position_escape_overflow(self):
        js = (ROOT / "app/web/static/panel.js").read_text(encoding="utf-8")
        # Menu is ported to body with fixed viewport coords (setProperty / set helper)
        self.assertIn("set('position', 'fixed')", js)
        self.assertIn("is-fixed-pos", js)
        self.assertIn("window.enhanceAllSelects", js)
        self.assertIn("window.openModal", js)
        css = (ROOT / "app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".ui-modal-panel-xl", css)


if __name__ == "__main__":
    unittest.main()
