"""A "Hybrid Owner" (``role="admin"`` whose ``.env`` PasarGuard account is
itself a *limited* admin, ``pg_is_owner=False``) must get the same friendly
max_users / data-cap pre-check as any reseller/pg_staff — both on the web
panel (``pg_quota.staff_needs_quota_check``, already wired into every
``/pg/users`` create/modify/mutate call site) and in the bot's own
PasarGuard user-creation flow (``admin_pg_users.py``) — instead of only
discovering the cap via PasarGuard's raw rejection.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.pg_quota import PgQuotaError, assert_can_create_user, staff_needs_quota_check


class HybridOwnerQuotaGateTests(unittest.TestCase):
    def test_true_owner_still_skips(self):
        """Missing / True pg_is_owner ⇒ genuine sudo owner, unchanged behavior."""
        self.assertFalse(staff_needs_quota_check({"role": "admin", "pg_admin_username": "x"}))
        self.assertFalse(
            staff_needs_quota_check(
                {"role": "admin", "pg_admin_username": "x", "pg_is_owner": True}
            )
        )

    def test_hybrid_owner_needs_check(self):
        """pg_is_owner explicitly False (Hybrid Owner) must be gated like anyone else."""
        self.assertTrue(
            staff_needs_quota_check(
                {"role": "admin", "pg_admin_username": "x", "pg_is_owner": False}
            )
        )

    def test_non_admin_roles_unaffected(self):
        self.assertTrue(staff_needs_quota_check({"role": "reseller", "pg_admin_username": "r1"}))
        self.assertTrue(staff_needs_quota_check({"role": "pg_staff", "pg_admin_username": "a1"}))


class HybridOwnerQuotaEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_hybrid_owner_blocked_at_max_users(self):
        """A limited env PG admin (3 groups / 20 users cap) hitting the cap must
        get the friendly PgQuotaError — not a silent bypass."""
        admin = {
            "username": "envlimited",
            "status": "active",
            "total_users": 20,
            "permission_overrides": {"max_users": 20},
        }
        with patch("app.services.pasarguard.get_pg") as get_pg:
            client = AsyncMock()
            client.get_admin = AsyncMock(return_value=admin)
            client.get_admin_role = AsyncMock(return_value={"limits": {}})
            get_pg.return_value = client
            with self.assertRaises(PgQuotaError) as ctx:
                await assert_can_create_user(
                    {
                        "role": "admin",
                        "pg_is_owner": False,
                        "pg_admin_username": "envlimited",
                    },
                    from_template=True,
                )
            self.assertIn("سقف تعداد کاربران", ctx.exception.message)

    async def test_true_owner_admin_bypasses_lookup_entirely(self):
        """A genuine sudo owner must never even call PasarGuard for a quota check."""
        with patch("app.services.pasarguard.get_pg") as get_pg:
            await assert_can_create_user({"role": "admin"}, from_template=True)
            get_pg.assert_not_called()


def _fake_state(data: dict):
    st = AsyncMock()
    st.get_data = AsyncMock(return_value=dict(data))
    st.update_data = AsyncMock()
    st.clear = AsyncMock()
    st.set_state = AsyncMock()
    return st


def _fake_message(text: str):
    msg = SimpleNamespace()
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _allowed_gate(fake_pg):
    return SimpleNamespace(
        allowed=True,
        reason="ok",
        user_message="",
        staff={
            "role": "admin",
            "web_owner": True,
            "pg_is_owner": True,
            "org_principal_id": 1,
            "org_depth": 0,
            "org_parent_id": None,
            "org_status": "active",
            "pg_admin_username": "envlimited",
        },
        pg_client=fake_pg,
        pg_user=None,
    )


class BotCreateUserQuotaWiringTests(unittest.IsolatedAsyncioTestCase):
    """The bot's own PG user-creation flow (platform-admin-only) must run the
    same quota pre-check as the web panel instead of only relying on
    PasarGuard's raw create-user rejection."""

    async def test_template_create_blocked_by_quota(self):
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        state = _fake_state({"pg_create_mode": "template", "pg_template_id": 5})
        message = _fake_message("testuser1")
        with (
            patch.object(mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))),
            patch.object(
                mod,
                "assert_can_create_user",
                new=AsyncMock(side_effect=PgQuotaError("سقف تعداد کاربران شما پر شده است")),
            ),
        ):
            await mod.pg_create_username(message, state, db_user=SimpleNamespace(telegram_id=999999))
        fake_pg.create_user_from_template.assert_not_called()
        message.answer.assert_awaited()
        self.assertIn("سقف", message.answer.await_args.args[0])

    async def test_template_create_allowed_when_quota_ok(self):
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        fake_pg.create_user_from_template = AsyncMock(return_value={"id": 42})
        state = _fake_state({"pg_create_mode": "template", "pg_template_id": 5})
        message = _fake_message("testuser1")
        with (
            patch.object(mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))),
            patch.object(mod, "assert_can_create_user", new=AsyncMock(return_value=None)),
            patch.object(mod, "_show_user_card", new=AsyncMock()),
        ):
            await mod.pg_create_username(message, state, db_user=SimpleNamespace(telegram_id=999999))
        fake_pg.create_user_from_template.assert_awaited_once()

    async def test_custom_create_blocked_by_quota(self):
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        # Owner-visible groups so selection resolves; quota must be what blocks.
        fake_pg.get_groups_simple = AsyncMock(
            return_value=[{"id": 1, "name": "g1"}, {"id": 2, "name": "g2"}]
        )
        state = _fake_state(
            {
                "pg_create_username": "testuser1",
                "pg_selected_groups": [1, 2],
                "pg_create_gb": 10,
            }
        )
        message = _fake_message("30")
        with (
            patch.object(mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))),
            patch.object(
                mod,
                "assert_can_create_user",
                new=AsyncMock(side_effect=PgQuotaError("حجم کاربر نمی‌تواند بیشتر از حد مجاز باشد")),
            ),
        ):
            await mod.pg_create_days(message, state, db_user=SimpleNamespace(telegram_id=999999))
        fake_pg.create_user.assert_not_called()
        message.answer.assert_awaited()

    async def test_custom_create_allowed_when_quota_ok(self):
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        fake_pg.create_user = AsyncMock(return_value={"id": 43})
        fake_pg.get_groups_simple = AsyncMock(
            return_value=[{"id": 1, "name": "g1"}, {"id": 2, "name": "g2"}]
        )
        state = _fake_state(
            {
                "pg_create_username": "testuser1",
                "pg_selected_groups": [1, 2],
                "pg_create_gb": 10,
            }
        )
        message = _fake_message("30")
        with (
            patch.object(mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))),
            patch.object(mod, "assert_can_create_user", new=AsyncMock(return_value=None)),
            patch.object(mod, "_show_user_card", new=AsyncMock()),
        ):
            await mod.pg_create_days(message, state, db_user=SimpleNamespace(telegram_id=999999))
        fake_pg.create_user.assert_awaited_once()


class PlatformPgQuotaStaffTests(unittest.IsolatedAsyncioTestCase):
    async def test_builds_staff_dict_from_platform_capabilities(self):
        from app.bot.auth import platform_pg_quota_staff

        caps = {
            "ok": True,
            "pg_is_owner": False,
            "username": "envlimited",
            "pg_role_id": 7,
            "features": ["pg_overview", "pg_users"],
            "role": {"limits": {"max_users": 20}},
        }
        with patch(
            "app.services.pg_access.resolve_platform_pg_capabilities",
            new=AsyncMock(return_value=caps),
        ):
            staff = await platform_pg_quota_staff()
        self.assertEqual(
            staff,
            {
                "role": "admin",
                "pg_is_owner": False,
                "pg_admin_username": "envlimited",
                "pg_role_id": 7,
            },
        )


if __name__ == "__main__":
    unittest.main()
