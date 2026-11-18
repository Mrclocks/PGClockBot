from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    Base,
    BotUser,
    Order,
    Plan,
    ServiceAddonPack,
    ServiceAutomation,
    UserService,
    WalletTransaction,
)
from app.services.service_automation import (
    automation_settings,
    claim_automation,
    configure_automation,
    exhausted_dimensions,
    process_service_automation,
)
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.postgres_support import scoped_async_engine, scoped_sync_engine

GB = 1024**3


class FakePG:
    def __init__(self):
        self.info = {
            "status": "active",
            "expire": int(datetime.now(timezone.utc).timestamp()) - 60,
            "data_limit": 10 * GB,
            "used_traffic": 10 * GB,
        }
        self.writes = []
        self.resets = 0
        self.fail_after_write = False

    async def get_user_by_id(self, user_id):
        return dict(self.info)

    async def modify_user_by_id(self, user_id, payload):
        await asyncio.sleep(0.03)
        self.writes.append(dict(payload))
        self.info.update(payload)
        if self.fail_after_write:
            raise TimeoutError("response lost after applying PG write")
        return dict(self.info)

    async def reset_user_by_id(self, user_id):
        self.resets += 1
        self.info["used_traffic"] = 0
        return dict(self.info)


class AutomationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.pg = FakePG()
        self.bot = AsyncMock()
        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.pg),
            patch("app.services.orders.get_pg", return_value=self.pg),
            patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(return_value=self.pg),
            ),
            patch("app.services.orders.assert_provision_renew", new=AsyncMock()),
            patch(
                "app.services.loyalty.consume_loyalty_discount_for_order",
                new=AsyncMock(),
            ),
            patch("app.services.loyalty.on_order_delivered", new=AsyncMock()),
            patch(
                "app.services.service_automation.get_all_settings",
                new=AsyncMock(return_value={"pay_wallet_enabled": "1"}),
            ),
        ]
        for p in self.patches:
            p.start()
        self.context_token = set_shop_reseller_id(None)
        async with self.Session() as session:
            user = BotUser(
                telegram_id=100, referral_code="AUTO100", wallet_balance=1000
            )
            plan = Plan(name="monthly", price=200, duration_days=30, data_limit_gb=10)
            volume = ServiceAddonPack(name="5 GB", kind="volume", amount=5, price=80)
            duration = ServiceAddonPack(
                name="7 days", kind="duration", amount=7, price=50
            )
            session.add_all([user, plan, volume, duration])
            await session.flush()
            svc = UserService(
                bot_user_id=user.id,
                plan_id=plan.id,
                pg_user_id=101,
                pg_username="auto-user",
            )
            session.add(svc)
            await session.commit()
            self.user_id, self.service_id = user.id, svc.id
            self.plan_id, self.volume_id, self.duration_id = (
                plan.id,
                volume.id,
                duration.id,
            )

    async def asyncTearDown(self):
        reset_shop_reseller_id(self.context_token)
        for p in reversed(self.patches):
            p.stop()
        await self.engine.dispose()

    async def enable(self, action="renew", choice_id=None):
        choices = {
            "renew": self.plan_id,
            "volume": self.volume_id,
            "duration": self.duration_id,
        }
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            await configure_automation(
                session,
                user,
                self.service_id,
                action,
                enabled=True,
                choice_id=choice_id or choices[action],
            )

    async def tick(self):
        async with self.Session() as session:
            await process_service_automation(session, self.service_id, self.bot)

    async def snapshot(self):
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            row = await session.get(ServiceAutomation, self.service_id)
            orders = list((await session.execute(select(Order))).scalars())
            ledger = list((await session.execute(select(WalletTransaction))).scalars())
            return user, row, orders, ledger

    async def test_renewal_restores_both_quotas_and_debits_once(self):
        await self.enable()
        await self.tick()
        await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 800)
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0].status, "delivered")
        self.assertTrue(orders[0].note.endswith(":auto"))
        self.assertEqual(len(ledger), 1)
        self.assertIsNone(row.pending_order_id)
        self.assertEqual(self.pg.resets, 0)
        self.assertEqual(self.pg.info["used_traffic"], 10 * GB)
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)

    async def test_concurrent_ticks_do_not_double_buy(self):
        await self.enable()
        await asyncio.gather(self.tick(), self.tick())
        self.assertEqual((await self.snapshot())[0].wallet_balance, 800)
        self.assertEqual(len(self.pg.writes), 1)

    async def test_low_wallet_notifies_once_then_retries_after_credit(self):
        await self.enable()
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            user.wallet_balance = 100
            await session.commit()
        await self.tick()
        await self.tick()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertEqual((await self.snapshot())[2], [])
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            user.wallet_balance = 300
            await session.commit()
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 100)
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_deleted_plan_requests_replacement_before_exhaustion(self):
        from app.services.plans_catalog import delete_sales_plan

        await self.enable()
        self.pg.info.update(
            expire=int(datetime.now(timezone.utc).timestamp()) + 86400, used_traffic=0
        )
        async with self.Session() as session:
            await delete_sales_plan(session, await session.get(Plan, self.plan_id))
            await session.commit()
        await self.tick()
        await self.tick()
        self.assertEqual(self.bot.send_message.await_count, 1)
        self.assertIn("جدید انتخاب", self.bot.send_message.call_args.args[1])
        user, row, orders, ledger = await self.snapshot()
        self.assertIsNone(row.renew_plan_id)
        self.assertTrue(row.renew_enabled)
        self.assertEqual(user.wallet_balance, 1000)
        self.assertEqual(orders, [])

    async def test_deleted_last_pack_remains_visible_for_replacement(self):
        await self.enable("volume")
        async with self.Session() as session:
            await session.delete(await session.get(ServiceAddonPack, self.volume_id))
            await session.commit()
        await self.tick()
        async with self.Session() as session:
            data = await automation_settings(
                session, await session.get(BotUser, self.user_id), self.service_id
            )
        volume = next(a for a in data["actions"] if a["action"] == "volume")
        self.assertTrue(volume["unavailable"])
        self.assertEqual(volume["choices"], [])
        self.assertIn("جدید انتخاب", self.bot.send_message.call_args.args[1])

    async def test_replacement_reenables_purchases(self):
        await self.enable("duration")
        async with self.Session() as session:
            pack = await session.get(ServiceAddonPack, self.duration_id)
            pack.is_active = False
            replacement = ServiceAddonPack(
                name="new", kind="duration", amount=3, price=40
            )
            session.add(replacement)
            await session.commit()
            replacement_id = replacement.id
        await self.tick()
        await self.enable("duration", replacement_id)
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 960)

    async def test_matching_addon_takes_priority_over_full_renewal(self):
        await self.enable()
        await self.enable("duration")
        await self.enable("volume")
        await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 870)
        self.assertEqual(len(orders), 2)
        self.assertTrue(all(o.note.startswith("svc_addon:") for o in orders))
        self.assertEqual(self.pg.resets, 0)
        await self.tick()
        self.assertEqual(len((await self.snapshot())[2]), 2)

    async def test_full_renewal_does_not_also_buy_one_pack(self):
        await self.enable()
        await self.enable("volume")
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 800)
        self.assertEqual(len((await self.snapshot())[2]), 1)

    async def test_missing_pack_does_not_fall_back_to_full_renewal(self):
        await self.enable()
        await self.enable("volume")
        self.pg.info["expire"] = int(datetime.now(timezone.utc).timestamp()) + 86400
        async with self.Session() as session:
            (await session.get(ServiceAddonPack, self.volume_id)).is_active = False
            await session.commit()
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 1000)
        self.assertEqual(self.pg.writes, [])

    async def test_lost_panel_response_stops_further_spending(self):
        await self.enable("volume")
        self.pg.fail_after_write = True
        await self.tick()
        await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 920)
        self.assertEqual(len(ledger), 1)
        self.assertTrue(row.needs_review)
        self.assertIsNotNone(row.pending_order_id)
        self.assertEqual(len(self.pg.writes), 1)

    async def test_restart_with_paid_order_never_buys_again(self):
        await self.enable()
        async with self.Session() as session:
            row = await session.get(ServiceAutomation, self.service_id)
            order = Order(
                user_id=self.user_id,
                service_id=self.service_id,
                plan_id=self.plan_id,
                amount=200,
                status="paid",
                note=f"renew:{self.service_id}",
            )
            session.add(order)
            await session.flush()
            row.pending_order_id, row.pending_action = order.id, "renew"
            await session.commit()
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 1000)
        self.assertTrue((await self.snapshot())[1].needs_review)
        self.assertEqual(self.pg.writes, [])

    async def test_failed_success_notification_retries_without_purchase(self):
        await self.enable()
        self.bot.send_message.side_effect = RuntimeError("telegram unavailable")
        await self.tick()
        self.bot.send_message.side_effect = None
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 800)
        self.assertIsNone((await self.snapshot())[1].pending_order_id)
        self.assertEqual(len(self.pg.writes), 1)

    async def test_disabled_and_unlimited_services_are_not_purchased(self):
        await self.enable()
        for status in ("disabled", "on_hold"):
            self.pg.info["status"] = status
            await self.tick()
        self.pg.info.update(status="active", data_limit=0, expire=None)
        await self.tick()
        self.assertEqual((await self.snapshot())[2], [])

    async def test_off_switch_stops_purchases(self):
        await self.enable()
        async with self.Session() as session:
            await configure_automation(
                session,
                await session.get(BotUser, self.user_id),
                self.service_id,
                "renew",
                enabled=False,
            )
        await self.tick()
        self.assertEqual((await self.snapshot())[2], [])

    async def test_choice_can_be_saved_while_switch_is_off(self):
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            await configure_automation(
                session,
                user,
                self.service_id,
                "renew",
                enabled=False,
                choice_id=self.plan_id,
            )
            await configure_automation(
                session, user, self.service_id, "renew", enabled=True
            )
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 800)

    async def test_other_user_and_foreign_catalog_rejected(self):
        async with self.Session() as session:
            other = BotUser(telegram_id=200, referral_code="OTHER200")
            session.add(other)
            await session.flush()
            foreign = Plan(
                name="foreign", price=20, duration_days=30, owner_reseller_id=other.id
            )
            session.add(foreign)
            await session.commit()
            with self.assertRaises(ValueError):
                await configure_automation(
                    session,
                    other,
                    self.service_id,
                    "renew",
                    enabled=True,
                    choice_id=self.plan_id,
                )
            with self.assertRaises(ValueError):
                await configure_automation(
                    session,
                    await session.get(BotUser, self.user_id),
                    self.service_id,
                    "renew",
                    enabled=True,
                    choice_id=foreign.id,
                )

    async def test_only_available_pack_kinds_are_shown(self):
        async with self.Session() as session:
            await session.delete(await session.get(ServiceAddonPack, self.volume_id))
            await session.commit()
            data = await automation_settings(
                session, await session.get(BotUser, self.user_id), self.service_id
            )
        self.assertEqual([a["action"] for a in data["actions"]], ["renew", "duration"])

    async def test_nonexistent_choice_is_rejected_without_invalid_fk(self):
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, "در دسترس"):
                await configure_automation(
                    session,
                    await session.get(BotUser, self.user_id),
                    self.service_id,
                    "renew",
                    enabled=True,
                    choice_id=99999,
                )
        self.assertFalse((await self.snapshot())[1].renew_enabled)

    async def test_wallet_failure_rolls_back_order_payment_and_claim(self):
        await self.enable()
        with patch(
            "app.services.wallet.debit_wallet",
            new=AsyncMock(side_effect=ValueError("موجودی کافی نیست")),
        ):
            with self.assertRaises(ValueError):
                await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 1000)
        self.assertIsNone(row.pending_order_id)
        self.assertIsNone(row.lock_token)
        self.assertEqual(orders, [])
        self.assertEqual(ledger, [])
        self.assertEqual(self.pg.writes, [])

    async def test_panel_read_failure_does_not_take_money_and_releases_lock(self):
        await self.enable()
        with patch.object(
            self.pg, "get_user_by_id", new=AsyncMock(side_effect=TimeoutError())
        ):
            with self.assertRaises(TimeoutError):
                await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 1000)
        self.assertIsNone(row.lock_token)
        self.assertEqual(orders, [])

    async def test_platform_service_keeps_original_shop_after_first_touch_changes(self):
        async with self.Session() as session:
            owner = BotUser(telegram_id=400, referral_code="OWNER400")
            session.add(owner)
            await session.flush()
            user = await session.get(BotUser, self.user_id)
            user.reseller_id = owner.id
            original = Order(
                user_id=user.id, amount=200, status="delivered", reseller_id=None
            )
            session.add(original)
            await session.flush()
            service = await session.get(UserService, self.service_id)
            service.remark = f"order:{original.id}"
            await session.commit()
        await self.enable("volume")
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 920)

    async def test_wallet_setting_off_stops_execution(self):
        await self.enable()
        with patch(
            "app.services.service_automation.get_all_settings",
            new=AsyncMock(return_value={"pay_wallet_enabled": "0"}),
        ):
            await self.tick()
            await self.tick()
        self.assertEqual((await self.snapshot())[2], [])
        self.assertEqual(self.bot.send_message.await_count, 1)

    async def test_api_enforces_ownership_and_boolean_toggle(self):
        import httpx
        from fastapi import FastAPI
        from app.api.miniapp_pages import register_miniapp_pages

        async def get_db():
            async with self.Session() as session:
                yield session

        async def load_user(session, request):
            return await session.get(BotUser, self.user_id)

        app = FastAPI()
        register_miniapp_pages(app, render=AsyncMock(), get_db=get_db)
        with (
            patch("app.api.miniapp_pages.load_mini_user", new=load_user),
            patch("app.api.miniapp_pages._require_commerce_ready", new=AsyncMock()),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                url = f"/api/mini/service/{self.service_id}/automation"
                response = await client.get(url)
                self.assertEqual(response.status_code, 200)
                response = await client.post(
                    url,
                    json={
                        "action": "renew",
                        "enabled": "false",
                        "choice_id": self.plan_id,
                    },
                )
                self.assertEqual(response.status_code, 400)
                response = await client.post(
                    "/api/mini/service/99999/automation",
                    json={
                        "action": "renew",
                        "enabled": True,
                        "choice_id": self.plan_id,
                    },
                )
                self.assertEqual(response.status_code, 404)
                response = await client.post(
                    url,
                    json={
                        "action": "renew",
                        "enabled": True,
                        "choice_id": self.plan_id,
                    },
                )
                self.assertEqual(response.status_code, 200)
        self.assertTrue((await self.snapshot())[1].renew_enabled)

    async def test_lease_can_only_be_claimed_once(self):
        await self.enable()
        async with self.Session() as one, self.Session() as two:
            self.assertIsNotNone(await claim_automation(one, self.service_id))
            self.assertIsNone(await claim_automation(two, self.service_id))

    async def test_paid_manual_mutation_prevents_automatic_purchase(self):
        await self.enable()
        async with self.Session() as session:
            session.add(
                Order(
                    user_id=self.user_id,
                    service_id=self.service_id,
                    plan_id=self.plan_id,
                    amount=200,
                    status="paid",
                    note=f"renew:{self.service_id}",
                )
            )
            await session.commit()
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 1000)
        self.assertEqual(self.pg.writes, [])

    async def test_sold_plan_can_be_removed_and_user_gets_replacement_notice(self):
        from app.services.plans_catalog import delete_sales_plan

        await self.enable()
        await self.tick()
        async with self.Session() as session:
            await delete_sales_plan(session, await session.get(Plan, self.plan_id))
            await session.commit()
        await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 800)
        self.assertIsNone(orders[0].plan_id)
        self.assertIsNone(row.renew_plan_id)
        self.assertIn("جدید انتخاب", self.bot.send_message.call_args.args[1])

    async def test_normal_admin_recovery_reuses_saved_renewal_target(self):
        from app.services.orders import fulfill_paid_order

        await self.enable()
        self.pg.fail_after_write = True
        await self.tick()
        self.pg.fail_after_write = False
        async with self.Session() as session:
            row = await session.get(ServiceAutomation, self.service_id)
            order = await session.get(Order, row.pending_order_id)
            await fulfill_paid_order(session, order)
        await self.tick()
        user, row, orders, ledger = await self.snapshot()
        self.assertEqual(user.wallet_balance, 800)
        self.assertFalse(row.needs_review)
        self.assertIsNone(row.pending_order_id)
        self.assertEqual(self.pg.resets, 0)
        self.assertEqual(self.pg.info["data_limit"], 20 * GB)
        self.assertEqual(self.pg.writes[0], self.pg.writes[1])
        self.assertEqual(len(ledger), 1)

    async def test_shop_wallet_cannot_spend_platform_balance(self):
        from app.services.wallet import credit_wallet, get_wallet_balance

        async with self.Session() as session:
            owner = BotUser(telegram_id=300, referral_code="SHOP300")
            session.add(owner)
            await session.flush()
            user = await session.get(BotUser, self.user_id)
            user.reseller_id = owner.id
            plan = await session.get(Plan, self.plan_id)
            plan.owner_reseller_id = owner.id
            (await session.get(UserService, self.service_id)).shop_id = owner.id
            await session.commit()
            owner_id = owner.id
        token = set_shop_reseller_id(owner_id)
        try:
            await self.enable()
        finally:
            reset_shop_reseller_id(token)
        await self.tick()
        self.assertEqual((await self.snapshot())[0].wallet_balance, 1000)
        async with self.Session() as session:
            user = await session.get(BotUser, self.user_id)
            await credit_wallet(session, user, 250, "shop seed", shop_id=owner_id)
        await self.tick()
        async with self.Session() as session:
            self.assertEqual(
                await get_wallet_balance(session, self.user_id, shop_id=owner_id), 50
            )
        self.assertEqual((await self.snapshot())[0].wallet_balance, 1000)


class ExhaustionTests(unittest.TestCase):
    def test_actual_finite_quota_required(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(
            exhausted_dimensions(
                {
                    "status": "active",
                    "expire": (now - timedelta(seconds=1)).isoformat(),
                    "data_limit": 100,
                    "used_traffic": 100,
                },
                now,
            ),
            (True, True),
        )
        self.assertEqual(
            exhausted_dimensions(
                {"status": "active", "data_limit": 0, "used_traffic": 999}, now
            ),
            (False, False),
        )
        self.assertEqual(
            exhausted_dimensions(
                {"status": "active", "data_limit": "nan", "used_traffic": "bad"}, now
            ),
            (False, False),
        )


class AutomationMigrationTests(unittest.TestCase):
    def test_upgrade_and_deleted_choices_clear_the_automation(self):
        import importlib.util
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = Path("alembic/versions/0034_service_automations.py")
        spec = importlib.util.spec_from_file_location("automation_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            for name in (
                "bot_users",
                "user_services",
                "plans",
                "orders",
                "service_addon_packs",
            ):
                conn.execute(sa.text(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)"))
                conn.execute(sa.text(f"INSERT INTO {name} (id) VALUES (1)"))
            with patch.object(
                migration, "op", Operations(MigrationContext.configure(conn))
            ):
                migration.upgrade()
                migration.upgrade()
                conn.execute(
                    sa.text(
                        "INSERT INTO service_automations (service_id, renew_plan_id, volume_pack_id, renew_enabled, volume_enabled) VALUES (1, 1, 1, true, true)"
                    )
                )
                conn.execute(sa.text("DELETE FROM plans WHERE id=1"))
                conn.execute(sa.text("DELETE FROM service_addon_packs WHERE id=1"))
                row = conn.execute(
                    sa.text(
                        "SELECT renew_plan_id, volume_pack_id, renew_enabled FROM service_automations"
                    )
                ).one()
                self.assertEqual(tuple(row), (None, None, 1))
                conn.execute(sa.text("DELETE FROM user_services WHERE id=1"))
                self.assertEqual(
                    conn.execute(
                        sa.text("SELECT COUNT(*) FROM service_automations")
                    ).scalar(),
                    0,
                )
                migration.downgrade()
                self.assertFalse(sa.inspect(conn).has_table("service_automations"))
        engine.dispose()

    def test_postgresql_schema_uses_boolean_defaults_and_safe_deletions(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        ddl = str(
            CreateTable(ServiceAutomation.__table__).compile(
                dialect=postgresql.dialect()
            )
        )
        self.assertIn("BOOLEAN DEFAULT false", ddl)
        self.assertEqual(ddl.count("ON DELETE SET NULL"), 3)
        self.assertIn("ON DELETE CASCADE", ddl)
