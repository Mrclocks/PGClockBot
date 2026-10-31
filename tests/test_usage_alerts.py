"""Low-time and low-volume alerts: add-on pack, one-tap renewal and wallet top-up offers, per shop."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models import (
    Base,
    BotUser,
    Plan,
    ResellerProfile,
    ServiceAddonPack,
    ServiceAutomation,
    ShopWallet,
    UserService,
)
from app.jobs import scheduler as jobs
from app.services.service_automation import notify_once
from app.services.usage_alerts import TOPUP_CALLBACK, AlertExtras, alert_extras, time_alert_markup, traffic_alert_markup
from app.services.users import _SETTINGS_CACHE, clear_settings_cache, set_setting


def buttons(markup) -> list[str]:
    return [b.callback_data for row in (markup.inline_keyboard if markup else []) for b in row]


class AlertCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name) / 'alerts.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.Session()
        _SETTINGS_CACHE.clear()
        self.now = datetime.now(timezone.utc)
        self.session.add_all([
            BotUser(id=2, telegram_id=1002, referral_code="shop2", role="reseller"),
            BotUser(id=3, telegram_id=1003, referral_code="shop3", role="reseller"),
        ])
        await self.session.flush()
        self.session.add_all([
            BotUser(id=1, telegram_id=1001, referral_code="c1", wallet_balance=5000),
            BotUser(id=4, telegram_id=1004, referral_code="c4", reseller_id=2, wallet_balance=9000),
            BotUser(id=5, telegram_id=1005, referral_code="c5", reseller_id=3),
            Plan(id=1, name="Platform plan", price=300, duration_days=30, data_limit_gb=10),
            Plan(id=2, name="Shop plan", price=200, duration_days=30, data_limit_gb=10, owner_reseller_id=2),
            Plan(id=3, name="Shop3 plan", price=200, duration_days=30, data_limit_gb=10, owner_reseller_id=3),
        ])
        await self.session.flush()
        created = self.now - timedelta(days=29)
        self.session.add_all([
            UserService(id=10, bot_user_id=1, plan_id=1, pg_user_id=500, pg_username="plat", subscription_token="tokplat", created_at=created),
            UserService(id=11, bot_user_id=4, plan_id=2, pg_user_id=501, pg_username="shop2svc", shop_id=2, subscription_token="tokshop2", created_at=created),
            UserService(id=12, bot_user_id=5, plan_id=3, pg_user_id=502, pg_username="shop3svc", shop_id=3, subscription_token="tokshop3", created_at=created),
        ])
        await self.session.flush()
        self.session.add_all([
            ServiceAutomation(service_id=10, shop_id=None, renew_enabled=True, renew_plan_id=1),
            ServiceAutomation(service_id=11, shop_id=2, renew_enabled=True, renew_plan_id=2),
            ServiceAutomation(service_id=12, shop_id=3, renew_enabled=True, renew_plan_id=3),
        ])
        # The platform wallet is rich, but the shop purses are empty: only the shop purse may count.
        self.session.add(ShopWallet(user_id=4, reseller_id=2, balance=50))
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()
        clear_settings_cache()
        self.tmp.cleanup()

    async def service(self, service_id):
        return await self.session.get(UserService, service_id)


class AlertExtrasTests(AlertCase):
    async def test_a_shop_counts_only_its_own_volume_packs(self):
        self.session.add(ServiceAddonPack(name="Shop2 pack", kind="volume", amount=5, price=100, owner_reseller_id=2, is_active=True))
        await self.session.commit()
        for shop_id, service_id, expected in ((2, 11, True), (3, 12, False), (None, 10, False)):
            extras = await alert_extras(self.session, [await self.service(service_id)], shop_id=shop_id)
            self.assertEqual(extras.sells_volume_packs, expected, shop_id)

    async def test_a_stale_plan_from_another_catalog_is_ignored_instead_of_breaking_the_batch(self):
        await self.session.execute(ServiceAutomation.__table__.update().where(ServiceAutomation.service_id == 11).values(renew_plan_id=3))
        await self.session.commit()
        extras = await alert_extras(self.session, [await self.service(11)], shop_id=2)
        self.assertEqual(extras.renew_shortfalls, {})

    async def test_inactive_and_duration_packs_do_not_count(self):
        self.session.add_all([
            ServiceAddonPack(name="Off", kind="volume", amount=5, price=100, owner_reseller_id=2, is_active=False),
            ServiceAddonPack(name="Days", kind="duration", amount=5, price=100, owner_reseller_id=2, is_active=True),
        ])
        await self.session.commit()
        self.assertFalse((await alert_extras(self.session, [await self.service(11)], shop_id=2)).sells_volume_packs)

    async def test_shortfall_uses_the_wallet_of_that_shop_only(self):
        extras = await alert_extras(self.session, [await self.service(11)], shop_id=2)
        self.assertEqual(extras.renew_shortfalls, {11: 200})
        other = await alert_extras(self.session, [await self.service(12)], shop_id=3)
        self.assertEqual(other.renew_shortfalls, {12: 200})

    async def test_platform_wallet_pays_the_platform_renewal(self):
        self.assertEqual((await alert_extras(self.session, [await self.service(10)], shop_id=None)).renew_shortfalls, {})
        await self.session.execute(BotUser.__table__.update().where(BotUser.id == 1).values(wallet_balance=100))
        await self.session.commit()
        self.assertEqual((await alert_extras(self.session, [await self.service(10)], shop_id=None)).renew_shortfalls, {10: 300})

    async def test_enough_balance_automation_off_or_review_means_no_shortfall(self):
        await self.session.execute(ShopWallet.__table__.update().values(balance=500))
        await self.session.commit()
        self.assertEqual((await alert_extras(self.session, [await self.service(11)], shop_id=2)).renew_shortfalls, {})
        await self.session.execute(ShopWallet.__table__.update().values(balance=0))
        await self.session.execute(ServiceAutomation.__table__.update().where(ServiceAutomation.service_id == 11).values(renew_enabled=False))
        await self.session.commit()
        self.assertEqual((await alert_extras(self.session, [await self.service(11)], shop_id=2)).renew_shortfalls, {})
        await self.session.execute(ServiceAutomation.__table__.update().where(ServiceAutomation.service_id == 11).values(renew_enabled=True, needs_review=True))
        await self.session.commit()
        self.assertEqual((await alert_extras(self.session, [await self.service(11)], shop_id=2)).renew_shortfalls, {})

    async def test_a_batch_costs_a_fixed_number_of_queries(self):
        services = [await self.service(11)] * 1
        statements = []
        event.listen(self.engine.sync_engine, "before_cursor_execute", lambda *args: statements.append(args[2]))
        await alert_extras(self.session, services, shop_id=2)
        small = len(statements)
        statements.clear()
        await alert_extras(self.session, [await self.service(11)] * 40, shop_id=2)
        self.assertEqual(len(statements), small)


class AlertMarkupTests(unittest.TestCase):
    UI = {"one_tap_renew_enabled": "1"}

    def test_time_alert_offers_renewal_and_topup_only_when_short(self):
        self.assertEqual(buttons(time_alert_markup(self.UI, 7, AlertExtras())), ["svc:renew:7"])
        short = AlertExtras(renew_shortfalls={7: 100})
        self.assertEqual(buttons(time_alert_markup(self.UI, 7, short)), ["svc:renew:7", TOPUP_CALLBACK])
        self.assertIsNone(time_alert_markup({"one_tap_renew_enabled": "0"}, 7, AlertExtras()))

    def test_traffic_alert_offers_the_pack_renewal_and_topup(self):
        extras = AlertExtras(sells_volume_packs=True, renew_shortfalls={7: 100})
        self.assertEqual(buttons(traffic_alert_markup(self.UI, 7, extras)), ["svc:addon:7:volume", "svc:renew:7", TOPUP_CALLBACK])
        self.assertEqual(buttons(traffic_alert_markup(self.UI, 7, AlertExtras())), ["svc:renew:7"])
        self.assertEqual(buttons(traffic_alert_markup({"one_tap_renew_enabled": "0"}, 7, AlertExtras(sells_volume_packs=True))), ["svc:addon:7:volume"])
        self.assertIsNone(traffic_alert_markup({"one_tap_renew_enabled": "0"}, 7, AlertExtras()))

    def test_only_the_short_service_gets_the_wallet_note(self):
        extras = AlertExtras(renew_shortfalls={7: 100})
        self.assertIn("کیف پول", extras.note_for(7))
        self.assertEqual(extras.note_for(8), "")


class AutomationNoticeTests(AlertCase):
    async def notice_markup(self, code):
        bot = AsyncMock()
        row = await self.session.get(ServiceAutomation, 10)
        await notify_once(self.session, row, await self.service(10), await self.session.get(BotUser, 1), bot, "renew", code, "body")
        return bot.send_message.await_args.kwargs["reply_markup"]

    async def test_the_insufficient_wallet_notice_offers_a_topup(self):
        self.assertEqual(buttons(await self.notice_markup("low_wallet")), [TOPUP_CALLBACK, "svc:auto:10"])

    async def test_other_notices_keep_their_single_button(self):
        self.assertEqual(buttons(await self.notice_markup("review")), ["svc:auto:10"])


class AlertJobTests(AlertCase):
    def info(self, *, traffic_low: bool):
        used, limit = (95, 100) if traffic_low else (10, 100)
        return {
            "status": "active", "expire": (self.now + timedelta(days=1)).isoformat(),
            "used_traffic": used, "data_limit": limit,
        }

    async def run_job(self, *, traffic_low=True):
        async with self.Session() as session:
            await set_setting(session, "user_alert_low_enabled", "1")
            await set_setting(session, "one_tap_renew_enabled", "1")
        clear_settings_cache()
        pg = SimpleNamespace(subscription_info=AsyncMock(side_effect=lambda token: self.info(traffic_low=traffic_low)))
        bot = AsyncMock()
        with patch.object(jobs, "SessionLocal", self.Session), patch.object(jobs, "get_pg", return_value=pg):
            await jobs.check_expiring_services(bot)
        return bot

    def sent(self, bot):
        return {call.args[0]: call for call in bot.send_message.await_args_list}

    async def test_platform_customer_with_a_short_wallet_is_warned_with_every_offer(self):
        await self.session.execute(BotUser.__table__.update().where(BotUser.id == 1).values(wallet_balance=100))
        self.session.add(ServiceAddonPack(name="Pack", kind="volume", amount=5, price=100, owner_reseller_id=None, is_active=True))
        await self.session.commit()
        bot = await self.run_job()
        calls = [c for c in bot.send_message.await_args_list if c.args[0] == 1001]
        self.assertEqual(len(calls), 2)
        time_call = next(c for c in calls if "زمان سرویس" in c.args[1])
        traffic_call = next(c for c in calls if "حجم باقی‌مانده" in c.args[1])
        self.assertEqual(buttons(time_call.kwargs["reply_markup"]), ["svc:renew:10", TOPUP_CALLBACK])
        self.assertIn("کیف پول برای تمدید", time_call.args[1])
        self.assertEqual(buttons(traffic_call.kwargs["reply_markup"]), ["svc:addon:10:volume", "svc:renew:10", TOPUP_CALLBACK])
        self.assertIn("کیف پول برای تمدید", traffic_call.args[1])

    async def test_a_customer_with_enough_wallet_gets_no_topup_offer(self):
        bot = await self.run_job()
        for call in (c for c in bot.send_message.await_args_list if c.args[0] == 1001):
            self.assertNotIn(TOPUP_CALLBACK, buttons(call.kwargs["reply_markup"]))
            self.assertNotIn("کیف پول برای تمدید", call.args[1])

    async def test_a_shop_customer_is_not_alerted_from_the_platform_bot(self):
        bot = await self.run_job()
        self.assertEqual({c.args[0] for c in bot.send_message.await_args_list}, {1001})

    async def test_shop_alerts_use_the_shop_purse_and_the_shop_catalog(self):
        async with self.Session() as session:
            session.add_all([
                ResellerProfile(user_id=2, is_active=True, bot_token="token2"),
                ServiceAddonPack(name="Shop pack", kind="volume", amount=5, price=100, owner_reseller_id=2, is_active=True),
            ])
            await set_setting(session, "user_alert_low_enabled", "1", reseller_id=2)
            await set_setting(session, "one_tap_renew_enabled", "1", reseller_id=2)
            await session.commit()
        clear_settings_cache()
        shop_bot = AsyncMock()
        pg = SimpleNamespace(subscription_info=AsyncMock(side_effect=lambda token: self.info(traffic_low=True)))
        with patch.object(jobs, "SessionLocal", self.Session), patch.object(jobs, "get_pg", return_value=pg), \
                patch.object(jobs, "_resolve_send_bot", lambda main, rid, profiles: shop_bot if rid == 2 else None):
            await jobs.check_expiring_services(AsyncMock())
        calls = {c.args[0]: c for c in shop_bot.send_message.await_args_list}
        self.assertEqual(set(calls), {1004})
        traffic = next(c for c in shop_bot.send_message.await_args_list if "حجم باقی‌مانده" in c.args[1])
        self.assertEqual(buttons(traffic.kwargs["reply_markup"]), ["svc:addon:11:volume", "svc:renew:11", TOPUP_CALLBACK])

    def numbered_bot(self) -> AsyncMock:
        bot = AsyncMock()
        bot.id = 1
        ids = iter(range(100, 200))
        bot.send_message.side_effect = lambda chat_id, *a, **k: SimpleNamespace(
            chat=SimpleNamespace(id=chat_id), message_id=next(ids)
        )
        return bot

    async def run_with(self, bot):
        pg = SimpleNamespace(subscription_info=AsyncMock(side_effect=lambda token: self.info(traffic_low=True)))
        with patch.object(jobs, "SessionLocal", self.Session), patch.object(jobs, "get_pg", return_value=pg):
            await jobs.check_expiring_services(bot)

    async def test_after_a_renewal_the_next_alert_replaces_the_previous_one(self):
        async with self.Session() as session:
            await set_setting(session, "user_alert_low_enabled", "1")
            await session.commit()
        clear_settings_cache()
        bot = self.numbered_bot()
        await self.run_with(bot)
        self.assertEqual(bot.send_message.await_count, 2)
        bot.delete_message.assert_not_awaited()
        async with self.Session() as session:  # a renewal re-arms both alerts
            await session.execute(UserService.__table__.update().where(UserService.id == 10).values(
                notified_expire=False, notified_traffic=False))
            await session.commit()
        await self.run_with(bot)
        deleted = sorted(c.kwargs["message_id"] for c in bot.delete_message.await_args_list)
        self.assertEqual(deleted, [100, 101])
        self.assertTrue(all(c.kwargs["chat_id"] == 1001 for c in bot.delete_message.await_args_list))


if __name__ == "__main__":
    unittest.main()
