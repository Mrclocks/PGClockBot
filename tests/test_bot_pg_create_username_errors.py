"""Bot PG create-user: FSM mode + duplicate username must not misroute errors."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.credential_policy import friendly_pg_error
from app.services.pasarguard import PasarGuardError
from app.services.pg_quota import PgQuotaError


def _fake_state(data: dict, *, current_state=None):
    st = AsyncMock()
    st._data = dict(data)
    st._state = current_state

    async def get_data():
        return dict(st._data)

    async def update_data(**kwargs):
        st._data.update(kwargs)

    async def clear():
        st._data.clear()
        st._state = None

    async def set_state(value):
        st._state = value

    st.get_data = AsyncMock(side_effect=get_data)
    st.update_data = AsyncMock(side_effect=update_data)
    st.clear = AsyncMock(side_effect=clear)
    st.set_state = AsyncMock(side_effect=set_state)
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
            "pg_admin_username": "owner",
            "pg_access": {"allowed_template_ids": [5], "allowed_group_ids": [1]},
        },
        pg_client=fake_pg,
        pg_user=None,
    )


class FriendlyPgErrorTests(unittest.TestCase):
    def test_duplicate_username_409(self):
        msg = friendly_pg_error("POST /api/user failed (409)", status_code=409)
        self.assertIn("قبلاً ثبت شده", msg)
        self.assertNotIn("تمپلیت", msg)

    def test_duplicate_body_text(self):
        msg = friendly_pg_error("User already exists")
        self.assertIn("قبلاً ثبت شده", msg)

    def test_pasar_guard_user_message_duplicate(self):
        exc = PasarGuardError(
            "POST /api/user failed (409)",
            409,
            {"detail": "Username already exists"},
        )
        self.assertIn("قبلاً ثبت شده", exc.user_message())

    def test_validation_still_wrapped(self):
        msg = friendly_pg_error(
            "body.username: Value error, Username only can be 3 to 128 characters."
        )
        self.assertIn("پاسارگارد", msg)
        self.assertIn("۳", msg)


class BotCreateUsernameErrorRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_mode_missing_template_does_not_fire(self):
        """Custom create asks for username without template — must go to GB step."""
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        state = _fake_state(
            {"pg_create_mode": "custom", "pg_selected_groups": [1], "pg_template_id": None}
        )
        message = _fake_message("Parham")
        with patch.object(
            mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))
        ):
            await mod.pg_create_username(
                message, state, db_user=SimpleNamespace(telegram_id=1)
            )
        fake_pg.create_user_from_template.assert_not_called()
        text = message.answer.await_args.args[0]
        self.assertIn("حجم", text)
        self.assertNotIn("تمپلیت", text)

    async def test_missing_mode_does_not_claim_missing_template(self):
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        state = _fake_state({"pg_selected_groups": [1]})
        message = _fake_message("Parham")
        with (
            patch.object(
                mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))
            ),
            patch.object(mod, "filtered_pg_reply_keyboard", new=AsyncMock(return_value=None)),
        ):
            await mod.pg_create_username(
                message, state, db_user=SimpleNamespace(telegram_id=1)
            )
        text = message.answer.await_args.args[0]
        self.assertIn("ناقص", text)
        self.assertNotIn("تمپلیت انتخاب نشده", text)

    async def test_duplicate_username_shows_friendly_not_template(self):
        import app.bot.handlers.admin_pg_users as mod

        fake_pg = AsyncMock()
        fake_pg.create_user_from_template = AsyncMock(
            side_effect=PasarGuardError(
                "POST /api/user/from_template failed (409)",
                409,
                {"detail": "User already exists"},
            )
        )
        state = _fake_state({"pg_create_mode": "template", "pg_template_id": 5})
        message = _fake_message("Parham")
        with (
            patch.object(
                mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))
            ),
            patch.object(mod, "assert_can_create_user", new=AsyncMock(return_value=None)),
            patch(
                "app.services.bot_pg_catalog_authz.catalog_template_allowed",
                return_value=True,
            ),
        ):
            await mod.pg_create_username(
                message, state, db_user=SimpleNamespace(telegram_id=1)
            )
        text = message.answer.await_args.args[0]
        self.assertIn("قبلاً ثبت شده", text)
        self.assertNotIn("تمپلیت", text)
        # Recoverable: FSM not cleared so operator can retry
        self.assertTrue(state._data.get("pg_template_id") == 5)

    async def test_create_tpl_clears_mid_create_state(self):
        import app.bot.handlers.admin_pg_users as mod
        from app.bot.handlers.admin_pg_users import PgUserStates

        fake_pg = AsyncMock()
        fake_pg.get_user_templates_simple = AsyncMock(return_value=[])
        state = _fake_state(
            {"pg_create_mode": "custom", "pg_selected_groups": [1]},
            current_state=PgUserStates.create_username,
        )
        callback = SimpleNamespace(
            data="adm:pg:create:tpl",
            answer=AsyncMock(),
            message=SimpleNamespace(),
        )
        with (
            patch.object(
                mod, "_pg_user_gate", new=AsyncMock(return_value=_allowed_gate(fake_pg))
            ),
            patch.object(mod, "safe_edit_text", new=AsyncMock()),
            patch.object(mod, "_filter_staff_templates", return_value=[]),
        ):
            await mod.pg_create_tpl_pick(
                callback, state, db_user=SimpleNamespace(telegram_id=1)
            )
        self.assertIsNone(state._state)
        self.assertEqual(state._data.get("pg_create_mode"), "template")
        self.assertIsNone(state._data.get("pg_template_id"))


if __name__ == "__main__":
    unittest.main()
