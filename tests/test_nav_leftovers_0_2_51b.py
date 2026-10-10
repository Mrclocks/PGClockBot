"""Post-0.2.51 leftovers: admin hubs edit-in-place, apply gate, pay reopen summary."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
ADMIN_PY = ROOT / "app" / "bot" / "handlers" / "admin.py"


def _bot_callback_message() -> MagicMock:
    message = MagicMock()
    message.from_user = MagicMock(is_bot=True)
    message.message_id = 42
    message.chat = MagicMock(id=7)
    message.edit_text = AsyncMock()
    message.answer = AsyncMock()
    return message


class AdmResellersEditInPlaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_adm_resellers_opens_inline_hub_no_reply_kb(self):
        from app.bot.handlers.admin import adm_resellers

        callback = AsyncMock()
        callback.message = _bot_callback_message()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.role = "admin"
        state = AsyncMock()

        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch("app.bot.auth.is_platform_admin", return_value=True),
            patch(
                "app.bot.handlers.admin._is_admin",
                return_value=True,
            ),
            patch(
                "app.bot.auth.platform_can_manage_representatives",
                AsyncMock(return_value=True),
            ),
            patch(
                "app.bot.handlers.reply_nav.open_admin_resellers_hub",
                new_callable=AsyncMock,
            ) as open_hub,
        ):
            await adm_resellers(callback, session=session, db_user=db_user, state=state)

        open_hub.assert_awaited_once()
        callback.message.answer.assert_not_awaited()

    async def test_adm_home_opens_inline_hub_no_reply_kb(self):
        from app.bot.handlers.admin import adm_home

        callback = AsyncMock()
        callback.message = _bot_callback_message()
        callback.answer = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        db_user.role = "admin"
        state = AsyncMock()

        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch("app.bot.handlers.admin._is_admin", return_value=True),
            patch(
                "app.bot.handlers.reply_nav.open_admin_home",
                new_callable=AsyncMock,
            ) as open_home,
        ):
            await adm_home(callback, session=session, db_user=db_user, state=state)

        open_home.assert_awaited_once()
        callback.message.answer.assert_not_awaited()

    def test_resapp_list_back_uses_nv_adm_resellers(self):
        text = ADMIN_PY.read_text(encoding="utf-8")
        # Applications list back button must reopen the inline hub, not adm:resellers.
        start = text.index("status_fa = {")
        end = text.index("📋 درخواست‌های باز")
        window = text[start:end]
        self.assertIn('callback_data="nv:adm:resellers"', window)
        self.assertNotIn('callback_data="adm:resellers"', window)


class AdminCallbackNoReplyKeyboardContract(unittest.TestCase):
    """Callback handlers in admin.py must not answer with a ReplyKeyboardMarkup."""

    def test_no_callback_answers_staff_reply_keyboard(self):
        tree = ast.parse(ADMIN_PY.read_text(encoding="utf-8"))
        # Map function name -> is under @router.callback_query
        callback_fns: set[str] = set()
        for node in tree.body:
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                src = ast.unparse(dec) if hasattr(ast, "unparse") else ""
                if "callback_query" in src:
                    callback_fns.add(node.name)
                    break

        violations: list[str] = []
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef):
                continue
            if node.name not in callback_fns:
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                # message.answer(..., reply_markup= await _staff_reply / _admin_hub_kb)
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == "answer":
                    for kw in child.keywords:
                        if kw.arg != "reply_markup":
                            continue
                        mark = ast.unparse(kw.value) if hasattr(ast, "unparse") else ""
                        if any(
                            tip in mark
                            for tip in (
                                "_staff_reply",
                                "_admin_hub_kb",
                                "admin_resellers_reply",
                                "admin_users_reply",
                                "admin_reply_keyboard",
                                "ReplyKeyboardMarkup",
                            )
                        ):
                            violations.append(f"{node.name}:{child.lineno}")
        self.assertEqual(violations, [], msg=f"callback→ReplyKB: {violations}")


class ResellerApplyRoleGateTests(unittest.TestCase):
    def test_main_reply_hides_apply_for_admin_role(self):
        from app.bot import keyboards as kb

        ui = {
            "menu_order": "shop,support,reseller_apply",
            "show_reseller_apply": "1",
            "btn_reseller_apply": "🤝 درخواست نمایندگی",
            "btn_shop": "فروشگاه",
            "btn_support": "پشتیبانی",
            "btn_admin": "پنل ادمین",
        }
        markup = kb.main_reply_keyboard(
            "admin",
            has_services=False,
            ui=ui,
            show_reseller_apply=True,
            as_user=False,
        )
        labels = [b.text for row in markup.keyboard for b in row]
        self.assertNotIn("🤝 درخواست نمایندگی", labels)

    def test_main_reply_shows_apply_in_as_user_preview(self):
        from app.bot import keyboards as kb

        ui = {
            "menu_order": "shop,support,reseller_apply",
            "show_reseller_apply": "1",
            "btn_reseller_apply": "🤝 درخواست نمایندگی",
            "btn_shop": "فروشگاه",
            "btn_support": "پشتیبانی",
            "btn_adm_exit_preview": "خروج از پیش‌نمایش",
        }
        markup = kb.main_reply_keyboard(
            "admin",
            has_services=False,
            ui=ui,
            show_reseller_apply=True,
            as_user=True,
            preview_exit_action=kb.REPLY_ACTION_ADMIN,
        )
        labels = [b.text for row in markup.keyboard for b in row]
        self.assertIn("🤝 درخواست نمایندگی", labels)


class NavPayReopenSummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_back_nav_pay_passes_summary(self):
        """Legacy reply Back into NAV_PAY must pass order summary when known."""
        from app.bot import menu_nav as nav
        from app.bot.handlers.reply_nav import handle_back
        from app.db.models import Order

        message = AsyncMock()
        session = AsyncMock()
        db_user = MagicMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value={nav.PAY_ORDER_ID: 42})
        state.set_state = AsyncMock()
        state.set_data = AsyncMock()
        order = MagicMock()
        order.id = 42
        order.amount = 50_000
        session.get = AsyncMock(return_value=order)

        with (
            patch("app.bot.menu_nav.get_nav_level", AsyncMock(return_value=nav.NAV_PAY)),
            patch("app.bot.menu_nav.pop_nav_level", AsyncMock(return_value=nav.NAV_PAY)),
            patch(
                "app.bot.menu_nav.present_order_pay",
                new_callable=AsyncMock,
            ) as pay,
            patch(
                "app.config.get_settings",
                return_value=MagicMock(currency="IRT"),
            ),
            patch(
                "app.services.formatting.format_toman",
                return_value="۵۰٬۰۰۰ تومان",
            ),
        ):
            await handle_back(
                message,
                session,
                db_user,
                state,
                is_reseller_bot=False,
                reseller_owner_id=None,
            )

        pay.assert_awaited_once()
        summary = pay.await_args.kwargs.get("summary") or ""
        self.assertIn("42", summary)
        self.assertIn("۵۰٬۰۰۰", summary)
        session.get.assert_awaited_with(Order, 42)


if __name__ == "__main__":
    unittest.main()
