"""Marketing light panels in the bot: each actor reaches its own shop only, repeats are harmless, names escaped."""

from __future__ import annotations

import inspect
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import shop_marketing_bot as view
from app.bot.nav_inline import admin_marketing_hub_keyboard, reseller_group_hub_keyboard
from app.bot.reseller_hub_groups import reseller_group
from app.db import Base
from app.db.models import BotUser, CatalogDiscount, ChargeCode, FunnelEvent, TargetedCampaign
from app.services import bot_feedback_texts as feedback
from app.services.users import DEFAULT_SETTINGS

OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP = 5
OTHER = 6


def shop_staff(*permissions: str, shop_id: int = SHOP) -> dict:
    return {"role": "reseller", "bot_user_id": shop_id, "permissions": list(permissions)}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _discount(shop_id: int | None, title: str, **fields) -> CatalogDiscount:
    return CatalogDiscount(
        shop_key=shop_id or 0, reseller_id=shop_id, title=title, scope="all", mode="permanent",
        reduction_kind="percent", reduction_value=10, request_key=secrets.token_hex(16), **fields,
    )


class MarketingBotCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'm.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=OTHER, telegram_id=106, referral_code="s6", role="reseller"),
                BotUser(id=20, telegram_id=120, referral_code="c20", reseller_id=SHOP),
            ])
            await session.flush()
            session.add_all([
                ChargeCode(id=1, code="MINE", amount=1, reseller_id=SHOP, is_active=True),
                ChargeCode(id=2, code="THEIRS", amount=1, reseller_id=OTHER, is_active=True),
                ChargeCode(id=3, code="PLAT", amount=1, reseller_id=None, is_active=True),
                TargetedCampaign(id=1, reseller_id=SHOP, audience="trial", days=7, text="a", created_by="t", status="draft"),
                TargetedCampaign(id=2, reseller_id=OTHER, audience="trial", days=7, text="b", created_by="t", status="draft"),
                _discount(SHOP, "<b>Mine</b>", id=1, active_slot=1),
                _discount(OTHER, "Theirs", id=2, active_slot=1),
                _discount(None, "Platform", id=3, active_slot=1),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def press(self, handler, data: str, staff_by_perm: dict[str, dict | None]):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()

        async def resolve(_session, _user, permission, **_kw):
            return staff_by_perm.get(permission)

        async with self.Session() as session:
            with (
                patch.object(view, "resolve_feature_staff", side_effect=resolve),
                patch.object(view, "present_nav_panel", AsyncMock()) as present,
                patch.object(view, "panel_page_url", return_value=None),
            ):
                extra = {"state": AsyncMock()} if "state" in inspect.signature(handler).parameters else {}
                await handler(callback, session=session, db_user=SimpleNamespace(id=9), **extra)
        return callback, present

    async def get(self, model, row_id: int):
        async with self.Session() as session:
            return await session.get(model, row_id)


class GiftAndCampaignTests(MarketingBotCase):
    async def test_a_shop_turns_off_its_own_code_only(self) -> None:
        staff = {"gift_codes": shop_staff("gift_codes")}
        await self.press(view.set_shop_gift_state, "res:gc:set:1:0", staff)
        self.assertFalse((await self.get(ChargeCode, 1)).is_active)
        for code_id in (2, 3):
            callback, _ = await self.press(view.set_shop_gift_state, f"res:gc:set:{code_id}:0", staff)
            callback.answer.assert_awaited_once_with(view.GIFT_NOT_FOUND, show_alert=True)
            self.assertTrue((await self.get(ChargeCode, code_id)).is_active)

    async def test_the_shop_panel_lists_only_the_shops_codes(self) -> None:
        _, present = await self.press(view.open_shop_gift_cb, "res:gc", {"gift_codes": shop_staff("gift_codes")})
        found = callbacks(present.await_args.kwargs["inline"])
        self.assertIn("res:gc:set:1:0", found)
        self.assertNotIn("res:gc:set:2:0", found)
        self.assertNotIn("res:gc:set:3:0", found)

    async def test_without_permission_or_as_the_platform_nothing_changes(self) -> None:
        for staff in ({}, {"gift_codes": OWNER, "campaigns": OWNER}):
            for handler, data in (
                (view.open_shop_gift_cb, "res:gc"),
                (view.set_shop_gift_state, "res:gc:set:3:0"),
                (view.open_shop_campaign_cb, "res:cmp"),
                (view.change_shop_campaign, "res:cmp:start:1"),
            ):
                with self.subTest(staff=bool(staff), data=data):
                    callback, present = await self.press(handler, data, staff)
                    callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
                    present.assert_not_awaited()
        self.assertTrue((await self.get(ChargeCode, 3)).is_active)
        self.assertEqual((await self.get(TargetedCampaign, 1)).status, "draft")

    async def test_a_shop_starts_its_own_campaign_and_not_another(self) -> None:
        staff = {"campaigns": shop_staff("campaigns")}
        await self.press(view.change_shop_campaign, "res:cmp:start:1", staff)
        self.assertEqual((await self.get(TargetedCampaign, 1)).status, "running")
        callback, _ = await self.press(view.change_shop_campaign, "res:cmp:start:2", staff)
        callback.answer.assert_awaited_once_with(view.CAMPAIGN_NOT_FOUND, show_alert=True)
        self.assertEqual((await self.get(TargetedCampaign, 2)).status, "draft")


class DiscountTests(MarketingBotCase):
    async def test_each_actor_lists_its_own_active_discounts_escaped(self) -> None:
        _, present = await self.press(view.show_discounts, "dsc:l", {"catalog_discounts": shop_staff("catalog_discounts")})
        text = present.await_args.kwargs["text"]
        self.assertIn("&lt;b&gt;Mine&lt;/b&gt;", text)
        self.assertNotIn("<b>Mine</b>", text)
        self.assertNotIn("Theirs", text)
        self.assertNotIn("Platform", text)
        self.assertEqual([c for c in callbacks(present.await_args.kwargs["inline"]) if c.startswith("dsc:s:")], ["dsc:s:1"])
        _, present = await self.press(view.show_discounts, "dsc:l", {"catalog_discounts": OWNER})
        self.assertIn("Platform", present.await_args.kwargs["text"])
        self.assertNotIn("Mine", present.await_args.kwargs["text"])

    async def test_stopping_needs_a_confirmation_and_stops_once(self) -> None:
        staff = {"catalog_discounts": shop_staff("catalog_discounts")}
        _, present = await self.press(view.confirm_stop_discount, "dsc:s:1", staff)
        self.assertIn("dsc:S:1", callbacks(present.await_args.kwargs["inline"]))
        self.assertTrue((await self.get(CatalogDiscount, 1)).is_active)
        await self.press(view.run_stop_discount, "dsc:S:1", staff)
        self.assertFalse((await self.get(CatalogDiscount, 1)).is_active)
        callback, _ = await self.press(view.confirm_stop_discount, "dsc:s:1", staff)
        self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))

    async def test_another_shops_discount_cannot_be_stopped(self) -> None:
        staff = {"catalog_discounts": shop_staff("catalog_discounts")}
        for discount_id in (2, 3):
            callback, _ = await self.press(view.run_stop_discount, f"dsc:S:{discount_id}", staff)
            self.assertTrue(callback.answer.await_args.kwargs.get("show_alert"))
            self.assertTrue((await self.get(CatalogDiscount, discount_id)).is_active)

    async def test_without_permission_nothing_is_shown(self) -> None:
        for handler, data in ((view.show_discounts, "dsc:l"), (view.run_stop_discount, "dsc:S:1")):
            callback, present = await self.press(handler, data, {})
            callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
            present.assert_not_awaited()
        self.assertTrue((await self.get(CatalogDiscount, 1)).is_active)


class FunnelTests(MarketingBotCase):
    async def test_the_funnel_counts_only_the_actors_shop(self) -> None:
        async with self.Session() as session:
            session.add_all([
                FunnelEvent(user_id=20, reseller_id=SHOP, step="shop_open", idempotency_key=f"a{i}") for i in range(4)
            ] + [FunnelEvent(user_id=20, reseller_id=OTHER, step="shop_open", idempotency_key=f"b{i}") for i in range(9)])
            await session.commit()
        _, present = await self.press(view.show_funnel, "fnl:v", {"orders": shop_staff("orders")})
        text = present.await_args.kwargs["text"]
        self.assertIn("<b>4</b>", text)
        self.assertNotIn("<b>9</b>", text)

    async def test_the_funnel_needs_orders_or_payments(self) -> None:
        callback, present = await self.press(view.show_funnel, "fnl:v", {"gift_codes": shop_staff("gift_codes")})
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        present.assert_not_awaited()
        _, present = await self.press(view.show_funnel, "fnl:v", {"payments": shop_staff("payments")})
        present.assert_awaited_once()

    def test_funnel_text_shows_each_steps_share_of_the_opens(self) -> None:
        text = view.funnel_text({"shop_open": 200, "plan_view": 50, "delivered": 10})
        self.assertIn("(25٪)", text)
        self.assertIn("(5٪)", text)
        self.assertNotIn("٪)", view.funnel_text({}))


class HubEntryTests(unittest.TestCase):
    def test_the_admin_marketing_hub_offers_discounts_and_funnel(self) -> None:
        found = callbacks(admin_marketing_hub_keyboard(dict(DEFAULT_SETTINGS)))
        self.assertIn("dsc:l", found)
        self.assertIn("fnl:v", found)

    def test_the_shop_group_opens_each_marketing_panel(self) -> None:
        entries = [(action, action) for action in view.SHOP_LEAF_OPENERS]
        with patch("app.bot.reply_keyboards._reseller_submenu_entries", return_value=entries):
            found = callbacks(reseller_group_hub_keyboard("shop", MagicMock(), dict(DEFAULT_SETTINGS)))
        for expected in ("res:gc", "res:cmp", "dsc:l", "fnl:v"):
            self.assertIn(expected, found)
        self.assertTrue(set(view.SHOP_LEAF_OPENERS) <= set(reseller_group("shop").actions))
