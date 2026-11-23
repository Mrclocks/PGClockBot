"""«بزن به کارت» cannot be switched on from the bots while the panel has no https address.

The web payment save refuses this (the gateway calls back and returns over https only); the owner
and shop bot switches used to skip the check and turn on a gateway every checkout would fail on.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot.handlers import admin_settings, reseller_settings
from app.services import bbc_settlement
from app.services import bot_feedback_texts as feedback

SHOP_ID = 13
DATA = "pay_bbc_enabled"


class OwnerToggleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)

    async def toggle(self, *, current: str, https: bool) -> tuple[AsyncMock, MagicMock]:
        callback = MagicMock(data=f"adm:st:tog:{DATA}", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value=current)),
            patch("app.services.users.get_setting", AsyncMock(return_value=current)),
            patch.object(bbc_settlement, "https_base_ready", return_value=https),
            patch.object(admin_settings, "set_setting", AsyncMock()) as saved,
            patch.object(admin_settings, "_rerender_after_key", AsyncMock()),
        ):
            await admin_settings.settings_toggle(callback, MagicMock(), SimpleNamespace(id=1))
        return saved, callback

    async def test_turning_on_without_https_is_refused(self) -> None:
        saved, callback = await self.toggle(current="0", https=False)
        saved.assert_not_awaited()
        callback.answer.assert_awaited_once_with(bbc_settlement.HTTPS_REQUIRED, show_alert=True)

    async def test_turning_on_with_https_is_saved(self) -> None:
        saved, callback = await self.toggle(current="0", https=True)
        self.assertEqual(saved.await_args.args[1:], (DATA, "1"))
        callback.answer.assert_awaited_once_with(feedback.SAVED)

    async def test_turning_off_is_always_allowed(self) -> None:
        saved, _ = await self.toggle(current="1", https=False)
        self.assertEqual(saved.await_args.args[1:], (DATA, "0"))


class ShopToggleTests(unittest.IsolatedAsyncioTestCase):
    async def toggle(self, *, current: str, https: bool) -> tuple[AsyncMock, MagicMock]:
        callback = MagicMock(data=f"res:st:tog:{DATA}", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(SimpleNamespace(user_id=SHOP_ID), None))),
            patch.object(reseller_settings, "get_setting", AsyncMock(return_value=current)),
            patch("app.services.users.get_setting", AsyncMock(return_value=current)),
            patch.object(bbc_settlement, "https_base_ready", return_value=https),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_render_sub", AsyncMock()),
        ):
            await reseller_settings.settings_toggle(callback, MagicMock(), SimpleNamespace(id=1))
        return saved, callback

    async def test_turning_on_without_https_is_refused(self) -> None:
        saved, callback = await self.toggle(current="0", https=False)
        saved.assert_not_awaited()
        callback.answer.assert_awaited_once_with(bbc_settlement.HTTPS_REQUIRED, show_alert=True)

    async def test_turning_on_with_https_is_saved_for_the_shop(self) -> None:
        saved, _ = await self.toggle(current="0", https=True)
        self.assertEqual(saved.await_args.args[1:], (DATA, "1"))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_turning_off_is_always_allowed(self) -> None:
        saved, _ = await self.toggle(current="1", https=False)
        self.assertEqual(saved.await_args.args[1:], (DATA, "0"))


if __name__ == "__main__":
    unittest.main()
