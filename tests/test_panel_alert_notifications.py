"""Panel alerts pushed to Telegram: announced once per change, to the right people only."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, DeliveryFailure, Order, PanelAlertState
from app.jobs import panel_alert_notifications as job
from app.services import panel_alert_notifications as svc
from app.services.notifications import PLATFORM_NOTIFY_PREFS, SHOP_NOTIFY_PERM_MAP, shop_notify_catalog
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

GB = 1024**3
T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def sig(count=1, *, shop=None, alert=svc.ALERT_FAILURES, fingerprint="", key=svc.KEY_FAILURES):
    return svc.Signal(shop_id=shop, alert=alert, notify_key=key, count=count, text="t", path="/x", fingerprint=fingerprint)


class DbCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def claim(self, signal, at):
        async with self.Session() as session:
            return await svc.claim_signal(session, signal, now=at)


class StateMachineTests(DbCase):
    async def test_the_first_occurrence_is_announced_and_repeats_are_not(self):
        self.assertTrue(await self.claim(sig(2), T0))
        self.assertFalse(await self.claim(sig(2), T0 + timedelta(minutes=5)))
        self.assertFalse(await self.claim(sig(2), T0 + timedelta(days=2)))

    async def test_a_growing_count_is_repeated_only_after_the_pause(self):
        await self.claim(sig(2), T0)
        self.assertFalse(await self.claim(sig(5), T0 + timedelta(hours=1)))
        self.assertTrue(await self.claim(sig(5), T0 + svc.REPEAT_AFTER + timedelta(minutes=1)))
        self.assertFalse(await self.claim(sig(5), T0 + svc.REPEAT_AFTER + timedelta(minutes=10)))

    async def test_a_new_fingerprint_is_announced_at_once(self):
        await self.claim(sig(1, alert=svc.ALERT_UPDATE, fingerprint="0.4.4"), T0)
        self.assertTrue(await self.claim(sig(1, alert=svc.ALERT_UPDATE, fingerprint="0.4.5"), T0 + timedelta(minutes=5)))

    async def test_a_cleared_alert_is_announced_again_when_it_returns(self):
        await self.claim(sig(3), T0)
        self.assertFalse(await self.claim(sig(0), T0 + timedelta(minutes=5)))
        self.assertTrue(await self.claim(sig(1), T0 + timedelta(minutes=10)))

    async def test_a_message_nobody_received_is_tried_again(self):
        await self.claim(sig(3), T0)
        async with self.Session() as session:
            await svc.release_signal(session, sig(3))
        self.assertTrue(await self.claim(sig(3), T0 + timedelta(minutes=5)))

    async def test_scopes_and_alerts_do_not_share_state(self):
        self.assertTrue(await self.claim(sig(1, shop=None), T0))
        self.assertTrue(await self.claim(sig(1, shop=5), T0))
        self.assertTrue(await self.claim(sig(1, shop=6), T0))
        self.assertTrue(await self.claim(sig(1, shop=5, alert=svc.ALERT_CAPACITY, key=svc.KEY_CAPACITY), T0))
        self.assertFalse(await self.claim(sig(1, shop=5), T0))

    async def test_simultaneous_workers_announce_once(self):
        results = await asyncio.gather(*(self.claim(sig(4), T0) for _ in range(4)))
        self.assertEqual(sum(results), 1)

    async def test_a_cleared_alert_that_was_never_on_creates_nothing(self):
        await self.claim(sig(0), T0)
        async with self.Session() as session:
            self.assertEqual((await session.scalars(select(PanelAlertState))).all(), [])


class SignalSourceTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
                BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="s6", role="reseller"),
            ])
            await session.flush()
            session.add_all([Order(id=i, user_id=1, amount=1) for i in (10, 11, 12, 20, 30)])
            await session.flush()
            session.add_all([
                DeliveryFailure(order_id=10), DeliveryFailure(order_id=10), DeliveryFailure(order_id=11),
                DeliveryFailure(order_id=12, resolved_at=T0),
                DeliveryFailure(order_id=20, reseller_id=5), DeliveryFailure(order_id=30, reseller_id=6),
            ])
            await session.commit()

    async def test_failures_are_counted_per_owner_and_per_order(self):
        async with self.Session() as session:
            signals = await svc.failure_signals(session)
        self.assertEqual({s.shop_id: s.count for s in signals}, {None: 2, 5: 1, 6: 1})

    async def test_an_owner_whose_failures_are_gone_is_cleared(self):
        await self.claim(sig(1, shop=5), T0)
        async with self.Session() as session:
            from sqlalchemy import update

            await session.execute(update(DeliveryFailure).where(DeliveryFailure.reseller_id == 5).values(resolved_at=T0))
            await session.commit()
            signals = {s.shop_id: s.count for s in await svc.failure_signals(session)}
        self.assertEqual(signals[5], 0)

    async def test_capacity_checks_are_spread_out(self):
        async with self.Session() as session:
            self.assertEqual(await svc.capacity_due(session, [5, 6, 7], now=T0), [5, 6, 7])
            await svc.mark_capacity_checked(session, 5, now=T0)
            self.assertEqual(await svc.capacity_due(session, [5, 6, 7], now=T0 + timedelta(minutes=10)), [6, 7])
            self.assertEqual(await svc.capacity_due(session, [5, 6, 7], now=T0 + svc.CAPACITY_RECHECK), [6, 7, 5])
            self.assertEqual(await svc.capacity_due(session, [5, 6, 7], now=T0, limit=1), [6])


class PureTests(unittest.TestCase):
    def test_update_signal_escapes_and_follows_availability(self):
        on = svc.update_signal({"update_available": True, "remote_version": "<b>9</b>", "version": "0.4.3"})
        self.assertEqual((on.count, on.fingerprint, on.shop_id), (1, "<b>9</b>", None))
        self.assertNotIn("<b>9</b>", on.text)
        self.assertEqual(svc.update_signal({"update_available": False, "remote_version": "1", "version": "1"}).count, 0)
        self.assertIsNone(svc.update_signal(None))

    def test_capacity_percent_reads_every_limit_key_and_ignores_unlimited(self):
        self.assertEqual(svc.capacity_percent({"max_users": 50, "total_users": 40}), 80.0)
        self.assertEqual(svc.capacity_percent({"data_limit": 100 * GB, "used_traffic": 90 * GB, "max_users": 10, "total_users": 1}), 90.0)
        self.assertEqual(svc.capacity_percent({"role": {"limits": {"max_traffic": 10 * GB}}, "traffic_used": 5 * GB}), 50.0)
        self.assertIsNone(svc.capacity_percent({"total_users": 500}))
        self.assertIsNone(svc.capacity_percent(None))

    def test_capacity_signal_has_a_hysteresis_band(self):
        self.assertEqual(svc.capacity_signal(5, 85.0, 80.0).count, 1)
        self.assertEqual(svc.capacity_signal(5, 50.0, 80.0).count, 0)
        self.assertEqual(svc.capacity_signal(5, None, 80.0).count, 0)
        self.assertIsNone(svc.capacity_signal(5, 77.0, 80.0))

    def test_switches_are_default_on_and_in_the_right_lists(self):
        from app.services.notifications import NOTIFY_PREFS
        from app.services.users import DEFAULT_SETTINGS

        for key in (svc.KEY_UPDATE, svc.KEY_CAPACITY, svc.KEY_FAILURES):
            self.assertEqual(DEFAULT_SETTINGS.get(key), "1", key)
            self.assertIn(key, {k for k, *_ in NOTIFY_PREFS})
        shop_keys = {k for k, *_ in shop_notify_catalog(["dashboard", "orders"])}
        self.assertNotIn(svc.KEY_UPDATE, shop_keys)
        self.assertEqual({svc.KEY_CAPACITY, svc.KEY_FAILURES} - shop_keys, set())
        self.assertNotIn(svc.KEY_CAPACITY, {k for k, *_ in PLATFORM_NOTIFY_PREFS})
        self.assertIn(svc.KEY_UPDATE, {k for k, *_ in PLATFORM_NOTIFY_PREFS})
        self.assertNotIn(svc.KEY_UPDATE, SHOP_NOTIFY_PERM_MAP)


class AnnounceTests(DbCase):
    async def announce(self, signal, *, delivered=1, side_effect=None):
        dispatch = AsyncMock(return_value=delivered, side_effect=side_effect)
        async with self.Session() as session:
            with patch.object(job, "_dispatch_dual_notify", dispatch), patch.object(job, "SEND_PAUSE_SECONDS", 0), \
                    patch.object(job, "public_panel_base_url", return_value=""):
                await job.announce(MagicMock(), session, signal)
        return dispatch

    async def test_a_new_alert_is_dispatched_with_its_owner_and_a_repeat_is_not(self):
        first = await self.announce(sig(2, shop=5))
        self.assertEqual(first.await_args.kwargs["ticket_reseller_id"], 5)
        self.assertEqual(first.await_args.args[2], svc.KEY_FAILURES)
        again = await self.announce(sig(2, shop=5))
        again.assert_not_awaited()

    async def test_a_platform_alert_is_dispatched_as_platform(self):
        dispatch = await self.announce(sig(1, shop=None))
        self.assertIsNone(dispatch.await_args.kwargs["ticket_reseller_id"])

    async def test_a_delivery_nobody_received_is_retried_on_the_next_check(self):
        await self.announce(sig(2), delivered=0)
        retry = await self.announce(sig(2))
        retry.assert_awaited_once()

    async def test_a_telegram_error_does_not_raise_and_is_retried(self):
        from aiogram.exceptions import TelegramAPIError

        await self.announce(sig(2), side_effect=TelegramAPIError(MagicMock(), "down"))
        retry = await self.announce(sig(2))
        retry.assert_awaited_once()


class UpdateAlertButtonTests(unittest.IsolatedAsyncioTestCase):
    def update_signal(self) -> svc.Signal:
        return svc.update_signal({"update_available": True, "remote_version": "0.8.0", "version": "0.7.8"})

    async def markup(self, *, public_base_url: str, panel_base: str):
        settings = SimpleNamespace(public_base_url=public_base_url)
        with patch.object(job, "get_settings", return_value=settings), \
                patch.object(job, "public_panel_base_url", return_value=panel_base):
            return await job._markup(self.update_signal())

    async def test_update_alert_button_opens_the_update_page(self):
        markup = await self.markup(public_base_url="https://panel.example.com/", panel_base="")
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.url, "https://panel.example.com/update")
        self.assertEqual(button.text, svc.UPDATE_BUTTON_LABEL)

    async def test_update_button_uses_the_panel_address_without_public_base_url(self):
        markup = await self.markup(public_base_url="", panel_base="http://203.0.113.7:9000")
        self.assertEqual(markup.inline_keyboard[0][0].url, "http://203.0.113.7:9000/update")

    async def test_no_button_when_only_a_loopback_address_is_known(self):
        self.assertIsNone(await self.markup(public_base_url="http://x", panel_base="http://127.0.0.1:9000"))

    async def test_other_alerts_keep_the_generic_label(self):
        settings = SimpleNamespace(public_base_url="https://panel.example.com")
        with patch.object(job, "get_settings", return_value=settings):
            markup = await job._markup(sig(1))
        self.assertEqual(markup.inline_keyboard[0][0].text, job.BUTTON_LABEL)


class CapacityJobTests(DbCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        from app.db.models import ResellerProfile, ResellerSetting

        async with self.Session() as session:
            session.add_all([
                BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="s6", role="reseller"),
                BotUser(id=7, telegram_id=107, referral_code="s7", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                ResellerProfile(user_id=5, is_active=True, pg_admin_username="shop-five"),
                ResellerProfile(user_id=6, is_active=True, pg_admin_username="shop-six"),
                ResellerProfile(user_id=7, is_active=False, pg_admin_username="shop-seven"),
                ResellerSetting(reseller_user_id=6, key=svc.KEY_CAPACITY, value="0"),
            ])
            await session.commit()

    async def test_only_active_shops_with_the_switch_on_are_checked(self):
        async with self.Session() as session:
            self.assertEqual(await job._capacity_shops(session), {5: "shop-five"})

    async def check(self, admin):
        pg = MagicMock(get_admin=AsyncMock(side_effect=admin if isinstance(admin, Exception) else None, return_value=None if isinstance(admin, Exception) else admin))
        announce = AsyncMock()
        async with self.Session() as session:
            with patch("app.services.pasarguard.get_pg", lambda: pg), patch.object(job, "announce", announce):
                await job._check_capacity(session, MagicMock(), 5, "shop-five", asyncio.Semaphore(1))
            due = await svc.capacity_due(session, [5], now=datetime.now(timezone.utc))
        return announce, due

    async def test_a_shop_over_the_threshold_is_announced_and_not_checked_again_for_a_while(self):
        announce, due = await self.check({"max_users": 10, "total_users": 9})
        self.assertEqual(announce.await_args.args[2].count, 1)
        self.assertEqual(announce.await_args.args[2].shop_id, 5)
        self.assertEqual(due, [])

    async def test_a_shop_under_the_threshold_clears_its_state_through_a_zero_signal(self):
        announce, _due = await self.check({"max_users": 10, "total_users": 1})
        self.assertEqual(announce.await_args.args[2].count, 0)

    async def test_an_unreadable_admin_announces_nothing_but_is_still_spaced_out(self):
        announce, due = await self.check(RuntimeError("down"))
        announce.assert_not_awaited()
        self.assertEqual(due, [])


class RecipientIsolationTests(DbCase):
    async def test_a_shop_alert_reaches_only_that_shops_staff(self):
        send = AsyncMock(return_value=1)
        shop_bot = (MagicMock(session=MagicMock(close=AsyncMock())), True)
        with (
            patch("app.services.notifications._send_to_chats", send),
            patch("app.services.notifications.get_settings", return_value=SimpleNamespace(admin_ids=[7001])),
            patch("app.services.notifications.notify_enabled", AsyncMock(return_value=True)),
            patch("app.services.notifications._shop_recipient_chat_ids", AsyncMock(return_value=[9001])),
            patch("app.services.resellers.get_reseller_profile", AsyncMock(return_value=MagicMock(bot_token="shop:t"))),
            patch("app.services.reseller_bots.open_notify_bot_for_reseller", AsyncMock(return_value=shop_bot)),
        ):
            from app.services.notifications import _dispatch_dual_notify

            async with self.Session() as session:
                await _dispatch_dual_notify(MagicMock(), session, svc.KEY_FAILURES, "t", ticket_reseller_id=5)
                self.assertEqual(send.await_args.args[1], [9001])
                await _dispatch_dual_notify(MagicMock(), session, svc.KEY_FAILURES, "t", ticket_reseller_id=None)
                self.assertEqual(send.await_args.args[1], [7001])


class MigrationTests(unittest.TestCase):
    def test_upgrade_is_idempotent_and_downgrade_drops_the_table(self):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0054_panel_alert_states.py"
        spec = importlib.util.spec_from_file_location("alert_states_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = scoped_sync_engine()
        with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
            self.assertIn("uq_panel_alert_states_scope_alert", {i["name"] for i in sa.inspect(conn).get_indexes("panel_alert_states")})
            conn.execute(sa.text("INSERT INTO panel_alert_states (scope_key, alert) VALUES ('platform', 'update')"))
            with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(sa.text("INSERT INTO panel_alert_states (scope_key, alert) VALUES ('platform', 'update')"))
            module.downgrade()
            self.assertFalse(sa.inspect(conn).has_table("panel_alert_states"))
