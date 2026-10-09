"""Bot user admin (web + telegram) — wallet & services (v4.3.0)."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class BotUserAdminServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_credit_rejects_non_positive(self):
        from app.services.bot_user_admin import admin_credit_user_wallet

        with self.assertRaises(ValueError):
            await admin_credit_user_wallet(
                AsyncMock(), MagicMock(), 0, actor="a"
            )

    async def test_credit_calls_wallet(self):
        from app.services.bot_user_admin import admin_credit_user_wallet

        user = MagicMock(id=3, wallet_balance=1000)
        with patch(
            "app.services.bot_user_admin.credit_wallet",
            AsyncMock(return_value=user),
        ) as credit:
            out = await admin_credit_user_wallet(
                AsyncMock(), user, 5000, actor="admin", note="test"
            )
        self.assertIs(out, user)
        credit.assert_awaited()
        args = credit.await_args
        self.assertEqual(args.args[2], 5000)
        self.assertIn("شارژ ادمین", args.args[3])

    async def test_get_owned_service_guards(self):
        from app.services.bot_user_admin import get_owned_service
        from unittest.mock import MagicMock

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        with self.assertRaises(ValueError):
            await get_owned_service(session, bot_user_id=1, service_id=9)

        svc = MagicMock(bot_user_id=2)
        result.scalar_one_or_none.return_value = svc
        session.execute = AsyncMock(return_value=result)
        with self.assertRaises(ValueError):
            await get_owned_service(session, bot_user_id=1, service_id=9)

    def test_get_owned_service_eager_loads_plan(self):
        """Regression: session.get without selectinload → MissingGreenlet on svc.plan
        when rendering Telegram service cards after commit (async SQLAlchemy)."""
        src = Path("app/services/bot_user_admin.py").read_text(encoding="utf-8")
        fn = src.split("async def get_owned_service", 1)[1].split(
            "\nasync def ", 1
        )[0]
        self.assertIn("selectinload(UserService.plan)", fn)
        self.assertNotIn("session.get(UserService", fn)
        self.assertIn("snapshot_telegram_lines", src)
        # Card reads plan.name — must stay compatible with eager-loaded plan
        lines_fn = src.split("def snapshot_telegram_lines", 1)[1].split(
            "\ndef ", 1
        )[0]
        self.assertIn("svc.plan", lines_fn)


class UserEditUiTests(unittest.TestCase):
    def test_template_and_routes(self):
        edit = Path("app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("wallet-credit", edit)
        self.assertIn("تمدید با پلن", edit)
        self.assertIn("تغییر مانده", edit)
        self.assertIn("کپی لینک", edit)
        self.assertIn("svc-list", edit)
        self.assertIn("svc-item", edit)
        self.assertIn("wallet-tx-list", edit)

        users = Path("app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("modal-user-edit", users)
        self.assertIn("/users/{{ u.id }}/edit?fragment=1", users)

        pages = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        self.assertIn("register_user_pages", pages)
        self.assertIn("/users/{user_id}/wallet-credit", pages)
        self.assertIn("/services/{service_id}/renew", pages)
        self.assertIn("fragment", pages)

        app = Path("app/api/app.py").read_text(encoding="utf-8")
        self.assertIn("register_user_pages", app)

    def test_staff_note_redirects_to_users_edit_modal(self):
        """Saving staff note must reopen /users?edit=… — GET /users/{id} does not exist."""
        ux = Path("app/api/ux20_pages.py").read_text(encoding="utf-8")
        self.assertIn('/users/{user_id}/staff-note', ux)
        self.assertIn("_redirect_user", ux)
        self.assertNotIn('f"/users/{user_id}?ok=', ux)
        edit_body = Path("app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("یادداشت داخلی", edit_body)
        self.assertIn("تگ ریسک", edit_body)
        self.assertIn("_color_tag_picker.html", edit_body)

    def test_panel_persian_form_validation(self):
        js = Path("app/web/static/panel.js").read_text(encoding="utf-8")
        self.assertIn("setupPanelFormValidation", js)
        self.assertIn("پر کردن این فیلد الزامی است.", js)
        self.assertIn("panelValidateForm", js)
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".form-field.is-invalid", css)
        self.assertIn(".field-error", css)

    def test_users_list_risk_tag_and_search_pad(self):
        users = Path("app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("badge-risk-tag", users)
        self.assertIn("u.color_tag", users)
        self.assertIn("cell-name", users)
        self.assertNotIn("risk-dot", users)
        picker = Path("app/web/templates/_color_tag_picker.html").read_text(encoding="utf-8")
        # --tag-color must live on the option so selected border/label use the tag hex.
        self.assertIn('class="color-tag-option', picker)
        self.assertIn('style="--tag-color: {{ t.hex }}"', picker)
        css = Path("app/web/static/panel.css").read_text(encoding="utf-8")
        self.assertIn(".badge.badge-risk-tag", css)
        risk = css.split(".badge.badge-risk-tag {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 999px;", risk)
        self.assertIn("color: var(--tag-color", risk)
        opt = css.split(".color-tag-option {", 1)[1].split("}", 1)[0]
        self.assertIn("border-radius: 999px;", opt)
        self.assertIn('html[data-theme="light"] .badge.badge-risk-tag', css)
        light_risk = css.split('html[data-theme="light"] .badge.badge-risk-tag {', 1)[1].split("}", 1)[0]
        self.assertIn("color-mix(in srgb, var(--tag-color", light_risk)
        self.assertNotIn(".risk-dot {", css)
        self.assertIn(".card.card-flush > .search-bar:first-child", css)
        self.assertIn("padding-top: var(--card-pad)", css)

    def test_reseller_edit_wallet_balance_column(self):
        src = Path("app/web/templates/_reseller_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("wallet_txs", src)
        self.assertIn("تراکنش‌های کیف پول", src)
        self.assertIn("wallet-tx-list", src)
        self.assertIn("is-credit", src)
        self.assertIn("is-debit", src)

    def test_bot_keyboards_and_handlers(self):
        kb = Path("app/bot/keyboards.py").read_text(encoding="utf-8")
        self.assertIn("adm:users:wcredit:", kb)
        self.assertIn("adm:users:svcs:", kb)
        self.assertIn("admin_user_service_actions", kb)

        admin = Path("app/bot/handlers/admin.py").read_text(encoding="utf-8")
        self.assertIn("adm_users_wallet_credit", admin)
        self.assertIn("adm_users_services", admin)
        self.assertIn("admin_renew_service", admin)
        self.assertIn("admin_extend_service", admin)

    def test_service_module_exists(self):
        src = Path("app/services/bot_user_admin.py").read_text(encoding="utf-8")
        self.assertIn("admin_credit_user_wallet", src)
        self.assertIn("list_service_snapshots", src)
        self.assertIn("admin_renew_service", src)
        self.assertIn("MAX_ADMIN_WALLET_CREDIT", src)


if __name__ == "__main__":
    unittest.main()
