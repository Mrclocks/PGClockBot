"""Manual bot-user create + plan assign from web panel."""

from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select


class AdminCreateBotUserTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_invalid_telegram_id(self):
        from app.services.bot_user_admin import admin_create_bot_user

        session = AsyncMock()
        with self.assertRaises(ValueError):
            await admin_create_bot_user(session, telegram_id=0)
        with self.assertRaises(ValueError):
            await admin_create_bot_user(session, telegram_id=-5)

    async def test_rejects_duplicate(self):
        from app.services.bot_user_admin import admin_create_bot_user

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = MagicMock(id=1)
        session.execute = AsyncMock(return_value=result)
        with self.assertRaisesRegex(ValueError, "قبلاً"):
            await admin_create_bot_user(session, telegram_id=123456789)

    @patch("app.services.bot_user_admin.adapt_legacy_user", new_callable=AsyncMock, return_value=123)
    async def test_creates_platform_user(self, adapter: AsyncMock) -> None:
        from app.db.models import BotUser, Role
        from app.services.bot_user_admin import admin_create_bot_user

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda u: u)
        session.add = MagicMock()

        user = await admin_create_bot_user(
            session,
            telegram_id=987654321,
            username="@alice",
            full_name="Alice",
            reseller_id=None,
        )
        adapter.assert_awaited_once_with(session, user)
        self.assertEqual(user.telegram_id, 987654321)
        self.assertEqual(user.username, "alice")
        self.assertEqual(user.full_name, "Alice")
        self.assertEqual(user.role, Role.USER.value)
        self.assertIsNone(user.reseller_id)
        session.add.assert_called_once()
        added = session.add.call_args.args[0]
        self.assertIsInstance(added, BotUser)


class AdminProvisionServiceTests(unittest.IsolatedAsyncioTestCase):
    def _user(self, *, reseller_id=None):
        return SimpleNamespace(id=10, reseller_id=reseller_id)

    def _plan(self, **kw):
        defaults = dict(
            id=5,
            is_active=True,
            is_trial=False,
            owner_reseller_id=None,
            data_limit_gb=10.0,
            duration_days=30,
            pg_template_id=None,
            pg_group_ids="1",
            name="Test Plan",
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    async def test_rejects_trial_plan(self):
        from app.services.bot_user_admin import admin_provision_service

        with self.assertRaisesRegex(ValueError, "تست"):
            await admin_provision_service(
                AsyncMock(),
                self._user(),
                self._plan(is_trial=True),
                shop_id=None,
            )

    async def test_rejects_shop_mismatch(self):
        from app.services.bot_user_admin import admin_provision_service

        user = self._user(reseller_id=7)
        plan = self._plan(owner_reseller_id=None)
        with self.assertRaisesRegex(ValueError, "فروشگاه"):
            await admin_provision_service(AsyncMock(), user, plan, shop_id=7)

    async def test_rejects_staff_plan_access(self):
        from app.services.bot_user_admin import admin_provision_service

        staff = {"role": "admin", "principal_id": 1}
        user = self._user()
        plan = self._plan(owner_reseller_id=99)
        with self.assertRaisesRegex(ValueError, "فروشگاه"):
            await admin_provision_service(
                AsyncMock(), user, plan, shop_id=None, staff=staff
            )

    @patch("app.services.bot_user_admin.optional_legacy_customer", new_callable=AsyncMock, return_value=123)
    async def test_provisions_platform_user(self, adapter: AsyncMock) -> None:
        from app.services.bot_user_admin import admin_provision_service

        user = self._user()
        plan = self._plan()
        pg_user = {
            "id": 42,
            "username": "clk_abcd",
            "subscription_url": "https://sub.example/x",
        }
        pg = AsyncMock()
        pg.create_user = AsyncMock(return_value=pg_user)
        session = AsyncMock()
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda s: s)
        session.add = MagicMock()
        session.rollback = AsyncMock()

        with patch("app.services.pasarguard.get_pg", return_value=pg), patch(
            "app.services.orders.generate_pg_username",
            AsyncMock(return_value="clk_abcd"),
        ), patch(
            "app.services.orders._reseller_pg_link",
            AsyncMock(return_value=(None, None)),
        ):
            svc = await admin_provision_service(
                session, user, plan, shop_id=None, staff=None, actor="owner"
            )
        adapter.assert_awaited_once_with(session, user.id)
        self.assertEqual(svc.customer_id, 123)
        self.assertEqual(svc.pg_user_id, 42)
        self.assertEqual(svc.plan_id, 5)
        self.assertEqual(svc.bot_user_id, 10)
        pg.create_user.assert_awaited()


class ManualUserProvisionUiTests(unittest.TestCase):
    def test_routes_and_templates(self):
        pages = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        self.assertIn("/users/create", pages)
        self.assertIn("/users/{user_id}/services", pages)
        self.assertIn("list_catalog_plans", pages)
        self.assertIn("get_owned_plan", pages)

        users = Path("app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("modal-user-create", users)
        self.assertIn("/users/create", users)

        edit = Path("app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("اختصاص پلن جدید", edit)
        self.assertIn("/users/{{ user.id }}/services", edit)

    def test_create_route_uses_scope(self):
        src = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        fn = src.split("async def user_create", 1)[1].split("\n    @app.", 1)[0]
        self.assertIn("resolve_shop_scope_id", fn)
        self.assertIn("admin_create_bot_user", fn)
        self.assertIn("is_explicit_owner_staff", fn)
        # Reseller-capable: dashboard ops dep, not Owner-only require_admin
        self.assertIn("Depends(require_ops)", fn)
        self.assertNotIn("Depends(require_admin)", fn)

    def test_provision_route_checks_scope(self):
        src = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        fn = src.split("async def user_provision_service", 1)[1].split(
            "\n    @app.", 1
        )[0]
        self.assertIn("_require_scoped_user", fn)
        self.assertIn("get_owned_plan", fn)
        self.assertIn("admin_provision_service", fn)
        self.assertIn("Depends(require_ops)", fn)
        self.assertNotIn("Depends(require_admin)", fn)

    def test_reseller_ui_provision_without_owner_manage(self):
        users = Path("app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("can_provision_users", users)
        self.assertIn("{% if provision %}", users)
        # Create/edit for provision; block/delete remain manage (Owner)
        create_btn = users.split('modal-user-create', 1)[0]
        self.assertIn("provision", create_btn[-200:])
        edit = Path("app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("can_manage_users", edit)
        self.assertIn("اختصاص پلن جدید", edit)
        # Plan assign must stay outside Owner-only gate
        gated = edit.split("{% if manage_user %}", 1)[1].split("{% endif %}", 1)[0]
        self.assertNotIn("اختصاص پلن جدید", gated)
        after = edit.split("{% endif %}", 1)[1]
        self.assertIn("اختصاص پلن جدید", after)

    def test_create_forces_staff_shop_scope(self):
        src = Path("app/services/bot_user_admin.py").read_text(encoding="utf-8")
        fn = src.split("async def admin_create_bot_user", 1)[1].split(
            "\nasync def ", 1
        )[0]
        self.assertIn("staff: dict | None = None", fn)
        self.assertIn("is_explicit_owner_staff", fn)
        self.assertIn("resolve_shop_scope_id", fn)


if __name__ == "__main__":
    unittest.main()


class AdminCreateStaffScopeTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.bot_user_admin.adapt_legacy_user", new_callable=AsyncMock, return_value=123)
    async def test_reseller_staff_forces_own_shop(self, adapter: AsyncMock) -> None:
        from app.services.bot_user_admin import admin_create_bot_user

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda u: u)
        session.add = MagicMock()
        owner = SimpleNamespace(id=77, role="reseller")
        session.get = AsyncMock(return_value=owner)

        staff = {"role": "reseller", "bot_user_id": 77}
        with patch(
            "app.services.platform_identity.is_explicit_owner_staff",
            return_value=False,
        ), patch(
            "app.services.shop_scope.resolve_shop_scope_id",
            return_value=77,
        ):
            # Even if caller tries reseller_id=None / wrong id, staff wins.
            user = await admin_create_bot_user(
                session,
                telegram_id=111222333,
                reseller_id=None,
                staff=staff,
            )
        adapter.assert_awaited_once_with(session, user)
        self.assertEqual(user.reseller_id, 77)
        self.assertEqual(user.role, "user")

    @patch("app.services.bot_user_admin.adapt_legacy_user", new_callable=AsyncMock, return_value=123)
    async def test_owner_staff_forces_platform_shop(self, adapter: AsyncMock) -> None:
        from app.services.bot_user_admin import admin_create_bot_user

        session = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        session.execute = AsyncMock(return_value=result)
        session.commit = AsyncMock()
        session.refresh = AsyncMock(side_effect=lambda u: u)
        session.add = MagicMock()

        staff = {"role": "admin", "principal_id": 1}
        with patch(
            "app.services.platform_identity.is_explicit_owner_staff",
            return_value=True,
        ):
            user = await admin_create_bot_user(
                session,
                telegram_id=444555666,
                reseller_id=99,  # must be ignored for Owner
                staff=staff,
            )
        adapter.assert_awaited_once_with(session, user)
        self.assertIsNone(user.reseller_id)


class ShopPgAclGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_provision_denied_without_pg_create(self):
        from app.services.bot_user_admin import admin_provision_service

        user = SimpleNamespace(id=10, reseller_id=7)
        plan = SimpleNamespace(
            id=5,
            is_active=True,
            is_trial=False,
            owner_reseller_id=7,
            data_limit_gb=10.0,
            duration_days=30,
            pg_template_id=None,
            pg_group_ids="1",
            name="P",
        )
        staff = {"role": "reseller", "bot_user_id": 7, "pg_user_actions": {"create": False}}
        with patch(
            "app.services.pg_access.staff_user_actions",
            return_value={"create": False, "update": False, "delete": False},
        ), patch(
            "app.services.plans_catalog.plan_belongs_to_staff",
            return_value=True,
        ):
            with self.assertRaisesRegex(ValueError, "پاسارگارد"):
                await admin_provision_service(
                    AsyncMock(), user, plan, shop_id=7, staff=staff, actor="r"
                )

    async def test_delete_denied_without_pg_delete(self):
        from app.services.bot_user_admin import admin_delete_service

        svc = SimpleNamespace(
            id=3, bot_user_id=10, pg_user_id=99, pg_username="u", integration_id=None, customer_id=None
        )
        staff = {"role": "reseller", "bot_user_id": 7}
        session = AsyncMock()
        with patch(
            "app.services.pg_access.staff_user_actions",
            return_value={"create": True, "update": True, "delete": False, "disable": False},
        ):
            with self.assertRaisesRegex(ValueError, "حذف"):
                await admin_delete_service(session, svc, delete_pg=True, staff=staff)

    def test_ui_gates_follow_pg_actions(self):
        edit = Path("app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        self.assertIn("can_pg_create", edit)
        self.assertIn("can_pg_update", edit)
        self.assertIn("can_pg_delete", edit)
        pages = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        self.assertIn("staff_user_actions", pages)
        self.assertIn("staff=staff", pages)
        src = Path("app/services/bot_user_admin.py").read_text(encoding="utf-8")
        self.assertIn("assert_staff_pg_user_action", src)
        self.assertIn("_pg_client_for_bot_service", src)
        self.assertNotIn("def _pg_client_for_service", src)

    def test_quick_renew_threads_staff(self):
        src = Path("app/services/users_quick.py").read_text(encoding="utf-8")
        fn = src.split("async def quick_renew_user", 1)[1].split("\nasync def ", 1)[0]
        self.assertIn("staff: dict | None = None", fn)
        self.assertIn("staff=staff", fn)
        pages = Path("app/api/user_pages.py").read_text(encoding="utf-8")
        qfn = pages.split("async def user_quick_renew", 1)[1].split("\n    @app.", 1)[0]
        self.assertIn("staff_user_actions", qfn)
        self.assertIn("staff=staff", qfn)
        users = Path("app/web/templates/users.html").read_text(encoding="utf-8")
        self.assertIn("pg_update and row.service_count", users)

