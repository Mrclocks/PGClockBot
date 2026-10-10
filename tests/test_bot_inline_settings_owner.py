"""Settings inline buttons must work inside owner-gated reply navigation.

Entry to Settings is checked on the reply keyboard. Re-running the Owner
Principal gate on every ``adm:st:*`` inline callback (or SettingsStates FSM
message) falsely denied the real operator with «دسترسی مالک سیستم لازم است».
"""

from __future__ import annotations

import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession

from aiogram.dispatcher.event.handler import CallableObject

from app.bot import _RequireBotOwnerPrincipal
from app.bot.auth import OWNER_REQUIRED_MESSAGE, bot_admin_settings_in_flow
from app.bot.handlers.admin_settings import settings_hub, support_add_start


def _state(*, level: str = "admin_settings", fsm: str | None = None):
    st = AsyncMock()

    async def get_data():
        return {"_kb_nav": level, "_kb_stack": []}

    st.get_data = AsyncMock(side_effect=get_data)
    st.get_state = AsyncMock(return_value=fsm)
    return st


def _cb(data: str = "adm:st:hub"):
    cb = AsyncMock()
    cb.data = data
    cb.message = AsyncMock()
    cb.message.edit_text = AsyncMock()
    cb.message.answer = AsyncMock()
    cb.answer = AsyncMock()
    return cb


class InlineSettingsFlowTests(unittest.IsolatedAsyncioTestCase):
    def test_support_add_has_session_for_aiogram_di(self):
        co = CallableObject(callback=support_add_start)
        self.assertIn("session", co.params)

    def test_settings_handlers_use_actor_guard_not_owner_decorator(self):
        src = open("app/bot/handlers/admin_settings.py", encoding="utf-8").read()
        self.assertNotIn("@require_bot_owner_handler", src)
        self.assertIn("@settings_actor_required", src)
        self.assertIn("async def _ensure_settings_actor", src)

    async def test_in_flow_add_support_handler_runs(self):
        cb = _cb("adm:st:sup:add")
        session = MagicMock(spec=AsyncSession)
        db_user = SimpleNamespace(role="admin", telegram_id=42)
        state = _state(level="admin_settings")
        state.set_state = AsyncMock()
        state.update_data = AsyncMock()
        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=False)) as gate,
            patch("app.bot.nav_input.ask_text", new_callable=AsyncMock) as ask,
        ):
            await support_add_start(cb, state, session, db_user)
        gate.assert_not_awaited()
        cb.answer.assert_awaited()
        ask.assert_awaited_once()
        self.assertEqual(ask.await_args.kwargs.get("cancel_code"), "adm_set")
        self.assertTrue(ask.await_args.kwargs.get("edit"))

    async def test_in_flow_add_support_skips_owner_middleware(self):
        ran = {}

        async def nxt(event, data):
            ran["ok"] = True
            return "add"

        mw = _RequireBotOwnerPrincipal()
        cb = _cb("adm:st:sup:add")
        state = _state(level="admin_settings")
        out = await mw(
            nxt,
            cb,
            {"session": None, "db_user": None, "state": state, "is_reseller_bot": False},
        )
        self.assertEqual(out, "add")
        self.assertTrue(ran.get("ok"))
        cb.answer.assert_not_awaited()

    async def test_in_flow_back_reaches_hub(self):
        cb = _cb("adm:st:hub")
        session = MagicMock(spec=AsyncSession)
        db_user = SimpleNamespace(role="admin", telegram_id=42)
        await settings_hub(cb, session, db_user, state=_state())
        cb.answer.assert_awaited()
        cb.message.edit_text.assert_awaited()
        cb.message.answer.assert_not_awaited()

    async def test_forged_settings_callback_outside_nav_still_denied(self):
        async def nxt(event, data):
            raise AssertionError("must not run")

        mw = _RequireBotOwnerPrincipal()
        cb = _cb("adm:st:sup:add")
        out = await mw(
            nxt,
            cb,
            {
                "session": None,
                "db_user": None,
                "state": _state(level="main"),
                "is_reseller_bot": False,
            },
        )
        self.assertIsNone(out)
        cb.answer.assert_awaited()
        self.assertEqual(cb.answer.await_args.args[0], OWNER_REQUIRED_MESSAGE)

    async def test_settings_fsm_message_in_flow(self):
        msg = SimpleNamespace(text="title", answer=AsyncMock())
        state = _state(level="admin_settings", fsm="SettingsStates:support_title")
        self.assertTrue(
            await bot_admin_settings_in_flow(
                msg,
                {"state": state},
            )
        )

    async def test_unrelated_admin_callback_still_denied(self):
        async def nxt(event, data):
            raise AssertionError("must not run")

        mw = _RequireBotOwnerPrincipal()
        cb = _cb("adm:backup:create")
        cb.data = "adm:backup:create"
        out = await mw(
            nxt,
            cb,
            {
                "session": None,
                "db_user": None,
                "state": _state(level="admin_settings"),
                "is_reseller_bot": False,
            },
        )
        self.assertIsNone(out)
        cb.answer.assert_awaited()


if __name__ == "__main__":
    unittest.main()
