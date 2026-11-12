"""Admin gift-code and campaign panels in the bot: shop scoping, Owner gate, idempotent actions."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import marketing_panels as panels
from app.bot.handlers import marketing_tools
from app.db.models import BotUser, CampaignRecipient, ChargeCode, TargetedCampaign
from app.services.formatting import format_number
from app.services.campaigns import campaign_progress_map, list_recent_campaigns
from app.services.gift_codes import set_gift_code_active


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


class PanelBuilderTests(unittest.TestCase):
    def _code(self, **kw):
        base = dict(id=1, code="SPRING", kind="wallet", amount=5000, percent=None, max_uses=10,
                    used_count=2, is_active=True)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_each_code_button_targets_the_opposite_state(self):
        on, off = self._code(id=1, is_active=True), self._code(id=2, code="OFF", is_active=False)
        _, markup = panels.gift_codes_panel([on, off], {})
        self.assertIn("adm:gc:set:1:0", _callbacks(markup))
        self.assertIn("adm:gc:set:2:1", _callbacks(markup))

    def test_hostile_code_text_is_escaped_in_the_message(self):
        text, _ = panels.gift_codes_panel([self._code(code="<b>x</b>")], {})
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)

    def test_callback_data_fits_telegram_limit_for_large_ids(self):
        _, markup = panels.gift_codes_panel([self._code(id=999_999_999)], {})
        self.assertTrue(all(len(data.encode()) <= 64 for data in _callbacks(markup)))

    def test_empty_state_and_web_button(self):
        text, markup = panels.gift_codes_panel([], {}, web_url="https://panel.example/gift-codes")
        self.assertIn("هنوز کدی ساخته نشده", text)
        urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
        self.assertEqual(urls, ["https://panel.example/gift-codes"])
        _, no_url = panels.gift_codes_panel([], {})
        self.assertFalse([b for row in no_url.inline_keyboard for b in row if b.url])

    def test_campaign_buttons_follow_the_status(self):
        rows = [
            SimpleNamespace(id=1, status="running", audience="trial"),
            SimpleNamespace(id=2, status="paused", audience="inactive"),
            SimpleNamespace(id=3, status="draft", audience="catalog_discount"),
            SimpleNamespace(id=4, status="completed", audience="trial"),
        ]
        _, markup = panels.campaigns_panel(rows, {1: {"sent": 3, "pending": 7}})
        data = _callbacks(markup)
        self.assertIn("adm:cmp:pause:1", data)
        self.assertIn("adm:cmp:start:2", data)
        self.assertIn("adm:cmp:start:3", data)
        self.assertFalse([d for d in data if d.endswith(":4")])

    def test_campaign_progress_is_sent_over_total(self):
        rows = [SimpleNamespace(id=1, status="running", audience="trial")]
        text, _ = panels.campaigns_panel(rows, {1: {"sent": 3, "pending": 7}})
        self.assertIn(f"{format_number(3)}/{format_number(10)}", text)

    def test_action_patterns_reject_malformed_data(self):
        for bad in ("adm:gc:set:1:2", "adm:gc:set:-1:1", "adm:gc:set:1234567890:1", "adm:gc:set:1:1\n",
                    "adm:gc:set::1", "adm:gc:set:1:1:extra"):
            self.assertIsNone(panels.GIFT_SET_PATTERN.match(bad), bad)
        for bad in ("adm:cmp:delete:1", "adm:cmp:start:x", "adm:cmp:start:1\n"):
            self.assertIsNone(panels.CAMPAIGN_ACTION_PATTERN.match(bad), bad)


class MarketingDbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'm.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()


class GiftCodeServiceTests(MarketingDbCase):
    async def _seed(self):
        async with self.Session() as session:
            session.add_all([
                ChargeCode(code="PLAT", amount=1, reseller_id=None, is_active=True),
                ChargeCode(code="SHOP5", amount=1, reseller_id=5, is_active=True),
            ])
            await session.commit()
            rows = (await session.execute(select(ChargeCode).order_by(ChargeCode.id))).scalars().all()
            return {row.code: row.id for row in rows}

    async def test_platform_call_cannot_change_a_shop_code(self):
        ids = await self._seed()
        async with self.Session() as session:
            self.assertFalse(await set_gift_code_active(session, ids["SHOP5"], shop_id=None, active=False))
            self.assertTrue((await session.get(ChargeCode, ids["SHOP5"])).is_active)

    async def test_shop_call_cannot_change_a_platform_or_other_shop_code(self):
        ids = await self._seed()
        async with self.Session() as session:
            self.assertFalse(await set_gift_code_active(session, ids["PLAT"], shop_id=5, active=False))
            self.assertFalse(await set_gift_code_active(session, ids["SHOP5"], shop_id=6, active=False))
            self.assertTrue((await session.get(ChargeCode, ids["PLAT"])).is_active)

    async def test_setting_the_same_state_twice_is_harmless(self):
        ids = await self._seed()
        async with self.Session() as session:
            for _ in range(2):
                self.assertTrue(await set_gift_code_active(session, ids["PLAT"], shop_id=None, active=False))
            self.assertFalse((await session.get(ChargeCode, ids["PLAT"])).is_active)
            self.assertFalse(await set_gift_code_active(session, 9999, shop_id=None, active=True))


class CampaignServiceTests(MarketingDbCase):
    async def test_list_is_scoped_bounded_and_progress_is_one_grouped_query(self):
        async with self.Session() as session:
            session.add_all([
                TargetedCampaign(reseller_id=None, audience="trial", days=7, text="a", created_by="t", status="draft"),
                TargetedCampaign(reseller_id=None, audience="trial", days=7, text="b", created_by="t", status="running"),
                TargetedCampaign(reseller_id=5, audience="trial", days=7, text="shop", created_by="t"),
            ])
            await session.commit()
            ids = [c.id for c in (await session.execute(select(TargetedCampaign))).scalars().all()]
            platform = await list_recent_campaigns(session, shop_id=None, limit=1)
            self.assertEqual([c.text for c in platform], ["b"])
            both = await list_recent_campaigns(session, shop_id=None)
            self.assertEqual({c.text for c in both}, {"a", "b"})
            self.assertEqual(await campaign_progress_map(session, []), {})
            session.add_all([
                CampaignRecipient(campaign_id=ids[1], user_id=1, status="sent"),
                CampaignRecipient(campaign_id=ids[1], user_id=2, status="sent"),
                CampaignRecipient(campaign_id=ids[1], user_id=3, status="pending"),
            ])
            await session.commit()
            progress = await campaign_progress_map(session, ids)
            self.assertEqual(set(progress), set(ids))
            self.assertEqual(progress[ids[1]], {"sent": 2, "pending": 1})
            self.assertEqual(progress[ids[0]], {})


def _callback(data: str):
    message = AsyncMock()
    message.from_user = SimpleNamespace(is_bot=True)  # a bot-owned panel bubble, as on a real tap
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


def _state():
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


class OwnerGateHandlerTests(MarketingDbCase):
    async def _owner(self, allowed: bool):
        return patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=allowed))

    async def test_owner_can_switch_a_platform_code_off_and_the_panel_refreshes(self):
        async with self.Session() as session:
            session.add(ChargeCode(code="PLAT", amount=1, reseller_id=None, is_active=True))
            await session.commit()
            code_id = (await session.scalar(select(ChargeCode.id)))
            callback = _callback(f"adm:gc:set:{code_id}:0")
            with await self._owner(True), patch.object(marketing_tools, "panel_page_url", return_value=None):
                await marketing_tools.set_gift_state(callback, session=session, db_user=BotUser(telegram_id=1), state=_state())
            self.assertFalse((await session.get(ChargeCode, code_id)).is_active)
            callback.message.edit_text.assert_awaited()
            callback.message.answer.assert_not_awaited()  # same bubble, no new message

    async def test_panels_edit_the_tapped_message_and_never_send_a_new_one(self):
        async with self.Session() as session:
            session.add(ChargeCode(code="PLAT", amount=1, reseller_id=None, is_active=True))
            session.add(TargetedCampaign(reseller_id=None, audience="trial", days=7, text="a", created_by="t", status="draft"))
            await session.commit()
            campaign_id = await session.scalar(select(TargetedCampaign.id))
            taps = [
                (marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK),
                (marketing_tools.open_campaign_panel, marketing_tools.CAMPAIGN_PANEL_CALLBACK),
                (marketing_tools.change_campaign, f"adm:cmp:start:{campaign_id}"),
            ]
            with await self._owner(True), patch.object(marketing_tools, "panel_page_url", return_value=None):
                for handler, data in taps:
                    callback = _callback(data)
                    await handler(callback, session=session, db_user=BotUser(telegram_id=1), state=_state())
                    callback.message.edit_text.assert_awaited_once()
                    callback.message.answer.assert_not_awaited()
                    callback.answer.assert_awaited()

    async def test_every_panel_has_a_back_button_to_the_hub(self):
        async with self.Session() as session:
            with await self._owner(True), patch.object(marketing_tools, "panel_page_url", return_value=None):
                for handler, data in (
                    (marketing_tools.open_gift_panel, marketing_tools.GIFT_PANEL_CALLBACK),
                    (marketing_tools.open_campaign_panel, marketing_tools.CAMPAIGN_PANEL_CALLBACK),
                ):
                    callback = _callback(data)
                    await handler(callback, session=session, db_user=BotUser(telegram_id=1), state=_state())
                    markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
                    self.assertEqual(_callbacks(markup)[-1], panels.MARKETING_HUB_CALLBACK)

    async def test_non_owner_changes_nothing(self):
        async with self.Session() as session:
            session.add(ChargeCode(code="PLAT", amount=1, reseller_id=None, is_active=True))
            session.add(TargetedCampaign(reseller_id=None, audience="trial", days=7, text="a", created_by="t", status="running"))
            await session.commit()
            code_id = await session.scalar(select(ChargeCode.id))
            campaign_id = await session.scalar(select(TargetedCampaign.id))
            gift = _callback(f"adm:gc:set:{code_id}:0")
            campaign = _callback(f"adm:cmp:pause:{campaign_id}")
            with await self._owner(False):
                await marketing_tools.set_gift_state(gift, session=session, db_user=BotUser(telegram_id=2), state=_state())
                await marketing_tools.change_campaign(campaign, session=session, db_user=BotUser(telegram_id=2), state=_state())
                await marketing_tools.open_gift_panel(_callback(marketing_tools.GIFT_PANEL_CALLBACK), session=session, db_user=BotUser(telegram_id=2), state=_state())
            await session.rollback()
            self.assertTrue((await session.get(ChargeCode, code_id)).is_active)
            self.assertEqual((await session.get(TargetedCampaign, campaign_id)).status, "running")
            gift.message.edit_text.assert_not_awaited()
            campaign.message.edit_text.assert_not_awaited()

    async def test_shop_bot_is_refused_even_for_an_owner_account(self):
        async with self.Session() as session:
            session.add(ChargeCode(code="PLAT", amount=1, reseller_id=None, is_active=True))
            await session.commit()
            code_id = await session.scalar(select(ChargeCode.id))
            callback = _callback(f"adm:gc:set:{code_id}:0")
            await marketing_tools.set_gift_state(
                callback, session=session, db_user=BotUser(telegram_id=1), state=_state(), is_reseller_bot=True
            )
            await session.rollback()
            self.assertTrue((await session.get(ChargeCode, code_id)).is_active)

    async def test_campaign_pause_is_idempotent_and_never_touches_a_shop_campaign(self):
        async with self.Session() as session:
            session.add_all([
                TargetedCampaign(reseller_id=None, audience="trial", days=7, text="a", created_by="t", status="running"),
                TargetedCampaign(reseller_id=5, audience="trial", days=7, text="s", created_by="t", status="running"),
            ])
            await session.commit()
            rows = (await session.execute(select(TargetedCampaign).order_by(TargetedCampaign.id))).scalars().all()
            platform_id, shop_id = rows[0].id, rows[1].id
            with await self._owner(True), patch.object(marketing_tools, "panel_page_url", return_value=None):
                first, second, shop = (_callback(f"adm:cmp:pause:{platform_id}"), _callback(f"adm:cmp:pause:{platform_id}"), _callback(f"adm:cmp:pause:{shop_id}"))
                for cb in (first, second, shop):
                    await marketing_tools.change_campaign(cb, session=session, db_user=BotUser(telegram_id=1), state=_state())
            await session.rollback()
            self.assertEqual((await session.get(TargetedCampaign, platform_id)).status, "paused")
            self.assertEqual((await session.get(TargetedCampaign, shop_id)).status, "running")
            second.answer.assert_awaited_with(marketing_tools.CAMPAIGN_NOT_FOUND, show_alert=True)
            shop.answer.assert_awaited_with(marketing_tools.CAMPAIGN_NOT_FOUND, show_alert=True)


class HubEntryTests(unittest.TestCase):
    def test_hub_offers_the_two_new_panels(self):
        from app.bot.nav_inline import admin_marketing_hub_keyboard

        data = _callbacks(admin_marketing_hub_keyboard({}))
        self.assertEqual(
            data,
            [
                "nv:adm:loy",
                "nv:adm:broadcast",
                "nv:adm:gc",
                "nv:adm:cmp",
                "nv:adm:cb",
                "dsc:l",
                "fnl:v",
                "tru:l:all:0",
                "nv:adm:home",
            ],
        )


if __name__ == "__main__":
    unittest.main()
