"""A plan name is shop-editable text; every Telegram HTML message must escape it."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.bot.handlers import shop

HOSTILE_NAME = "VIP <a href='https://evil.example'>x</a> & <b>"
ESCAPED_NAME = "VIP &lt;a href=&#x27;https://evil.example&#x27;&gt;x&lt;/a&gt; &amp; &lt;b&gt;"


def _plan() -> SimpleNamespace:
    return SimpleNamespace(
        id=1, name=HOSTILE_NAME, price=100_000, is_active=True, is_trial=False,
        discount_percent=0, discount_amount=0, sale_price=None,
    )


class WholesaleScreenEscapingTests(unittest.IsolatedAsyncioTestCase):
    async def _rendered_text(self, handler, *, state_data: dict | None = None, callback_data: str) -> str:
        edit = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(return_value=state_data or {})
        callback = SimpleNamespace(data=callback_data, answer=AsyncMock(), message=SimpleNamespace())
        ui = {"wholesale_enabled": "1", "wholesale_min_qty": "1", "wholesale_max_qty": "50"}
        with patch.multiple(
            shop,
            get_all_settings=AsyncMock(return_value=ui),
            get_catalog_plan=AsyncMock(return_value=_plan()),
            safe_edit_text=edit,
        ):
            await handler(callback, AsyncMock(), state)
        return edit.await_args.args[1]

    def _assert_escaped(self, text: str) -> None:
        self.assertIn(ESCAPED_NAME, text)
        self.assertNotIn("evil.example'>", text)
        self.assertNotIn("<a href", text)

    async def test_picking_a_plan_escapes_its_name(self) -> None:
        text = await self._rendered_text(shop.wholesale_pick_plan, callback_data="shop:wholesale:plan:1")
        self._assert_escaped(text)

    async def test_returning_to_quantity_escapes_the_plan_name(self) -> None:
        text = await self._rendered_text(
            shop.wholesale_qty_back, callback_data="shop:wholesale:qty", state_data={"wholesale_plan_id": 1},
        )
        self._assert_escaped(text)

    async def test_stepping_the_quantity_escapes_the_plan_name(self) -> None:
        text = await self._rendered_text(
            shop.wholesale_qty_step, callback_data="shop:wholesale:qty:+", state_data={"wholesale_plan_id": 1},
        )
        self._assert_escaped(text)


if __name__ == "__main__":
    unittest.main()
