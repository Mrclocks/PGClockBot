"""Volume ceiling of fixed-plan shops: holds, delivery and add-on paths, and the oversell switch."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from app.db import Base
from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    PgAdminSubscription,
    Plan,
    ResellerProfile,
    Role,
    UserService,
    VolumeHold,
    WalletTransaction,
)
from app.services.billing import GB
from app.services.pasarguard import PasarGuardError
from app.services.provision_gate import ProvisionError, hold_create_volume, hold_limit_change
from app.services.volume_allocation import (
    CUSTOMER_UNAVAILABLE_MSG,
    VolumeAllocationError,
    apply_create_form_oversell,
    apply_oversell_switch,
    assert_volume_delta_allowed,
    build_allocation_snapshot,
    hold_volume,
    list_oversold_shop_ids,
    release_volume_hold,
    set_shop_oversell,
    sum_allocated_bytes,
    threshold_level,
)

ROOT = Path(__file__).resolve().parents[1]


class VolumeHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'vol.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _shop(
        self,
        *,
        shop_id: int = 10,
        ceiling_gb: int = 10,
        allow_oversell: bool = False,
        payg: bool = False,
        extra_gb: int = 0,
    ) -> int:
        async with self.Session() as session:
            session.add(
                BotUser(
                    id=shop_id,
                    telegram_id=1000 + shop_id,
                    role=Role.RESELLER.value,
                    referral_code=f"s{shop_id}",
                )
            )
            session.add(
                ResellerProfile(
                    user_id=shop_id,
                    pg_admin_username=f"admin{shop_id}",
                    billing_mode="payg" if payg else "fixed",
                    allow_volume_oversell=allow_oversell,
                    is_active=True,
                )
            )
            if ceiling_gb > 0 or extra_gb > 0:
                session.add(
                    PgAdminSubscription(
                        pg_username=f"admin{shop_id}",
                        base_gb=ceiling_gb,
                        extra_gb_purchased=extra_gb,
                    )
                )
            await session.commit()
        return shop_id

    async def _service(
        self,
        shop_id: int,
        *,
        bytes_: int | None,
        cancelled: bool = False,
        expired: bool = False,
        synced: bool = True,
        bot_user_id: int | None = None,
    ) -> int:
        async with self.Session() as session:
            uid = bot_user_id or (9000 + shop_id)
            if await session.get(BotUser, uid) is None:
                session.add(
                    BotUser(
                        id=uid,
                        telegram_id=uid,
                        role=Role.USER.value,
                        referral_code=f"u{uid}",
                    )
                )
            now = datetime.now(timezone.utc)
            svc = UserService(
                bot_user_id=uid,
                pg_user_id=uid,
                pg_username=f"u-{uid}-{bytes_}",
                shop_id=shop_id,
                quota_data_limit_bytes=bytes_,
                quota_synced_at=now if synced else None,
                quota_expire_at=(now - timedelta(days=1)) if expired else (now + timedelta(days=7)),
                is_cancelled=cancelled,
                quota_status="active" if synced else None,
            )
            session.add(svc)
            await session.commit()
            return int(svc.id)


class GateBehaviourTests(VolumeHarness):
    async def test_switch_off_blocks_positive_delta(self) -> None:
        shop = await self._shop(ceiling_gb=5, allow_oversell=False)
        await self._service(shop, bytes_=4 * GB)
        async with self.Session() as session:
            with self.assertRaises(VolumeAllocationError) as ctx:
                await assert_volume_delta_allowed(
                    session, shop_id=shop, delta_bytes=2 * GB, audience="admin"
                )
            self.assertIn("سقف حجم پر است", ctx.exception.message)
            self.assertIn("از", ctx.exception.message)

    async def test_switch_on_allows_oversell(self) -> None:
        shop = await self._shop(ceiling_gb=5, allow_oversell=True)
        await self._service(shop, bytes_=4 * GB)
        async with self.Session() as session:
            await assert_volume_delta_allowed(
                session, shop_id=shop, delta_bytes=10 * GB, audience="admin"
            )

    async def test_customer_message_hides_budget(self) -> None:
        shop = await self._shop(ceiling_gb=1, allow_oversell=False)
        await self._service(shop, bytes_=1 * GB)
        async with self.Session() as session:
            with self.assertRaises(VolumeAllocationError) as ctx:
                await assert_volume_delta_allowed(
                    session, shop_id=shop, delta_bytes=1 * GB, audience="customer"
                )
            self.assertEqual(ctx.exception.user_message(), CUSTOMER_UNAVAILABLE_MSG)
            self.assertNotIn("گیگ", ctx.exception.user_message())

    async def test_negative_and_zero_delta_free(self) -> None:
        shop = await self._shop(ceiling_gb=1, allow_oversell=False)
        await self._service(shop, bytes_=1 * GB)
        async with self.Session() as session:
            await assert_volume_delta_allowed(
                session, shop_id=shop, delta_bytes=-GB, audience="admin"
            )
            # zero with allow_unlimited True is a no-op shrink path
            await assert_volume_delta_allowed(
                session,
                shop_id=shop,
                delta_bytes=0,
                audience="admin",
                allow_unlimited=True,
            )

    async def test_unlimited_create_rejected_when_gated(self) -> None:
        shop = await self._shop(ceiling_gb=10, allow_oversell=False)
        async with self.Session() as session:
            with self.assertRaises(VolumeAllocationError):
                await assert_volume_delta_allowed(
                    session, shop_id=shop, delta_bytes=0, audience="admin"
                )

    async def test_expired_counts_until_cancelled(self) -> None:
        shop = await self._shop(ceiling_gb=5, allow_oversell=False)
        await self._service(shop, bytes_=3 * GB, expired=True)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 3 * GB)
            with self.assertRaises(VolumeAllocationError):
                await assert_volume_delta_allowed(
                    session, shop_id=shop, delta_bytes=3 * GB, audience="admin"
                )

    async def test_cancel_frees_allocation(self) -> None:
        shop = await self._shop(ceiling_gb=5, allow_oversell=False)
        await self._service(shop, bytes_=5 * GB, cancelled=True)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 0)
            await assert_volume_delta_allowed(
                session, shop_id=shop, delta_bytes=5 * GB, audience="admin"
            )

    async def test_unlimited_and_unsynced_count_zero_and_listed(self) -> None:
        shop = await self._shop(ceiling_gb=5, allow_oversell=False)
        await self._service(shop, bytes_=0, synced=True)
        await self._service(shop, bytes_=None, synced=False, bot_user_id=9001)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 0)
            snap = await build_allocation_snapshot(session, shop)
            self.assertIsNotNone(snap)
            kinds = {row["kind"] for row in snap.unlimited_services}
            self.assertEqual(kinds, {"unlimited", "unsynced"})

    async def test_oversold_blocks_increase_allows_decrease(self) -> None:
        shop = await self._shop(ceiling_gb=5, allow_oversell=False)
        await self._service(shop, bytes_=8 * GB)  # already oversold
        async with self.Session() as session:
            with self.assertRaises(VolumeAllocationError):
                await assert_volume_delta_allowed(
                    session, shop_id=shop, delta_bytes=GB, audience="admin"
                )
            await assert_volume_delta_allowed(
                session, shop_id=shop, delta_bytes=-GB, audience="admin"
            )
            snap = await build_allocation_snapshot(session, shop)
            self.assertTrue(snap.is_oversold)

    async def test_payg_exempt(self) -> None:
        shop = await self._shop(ceiling_gb=1, allow_oversell=False, payg=True)
        await self._service(shop, bytes_=10 * GB)
        async with self.Session() as session:
            await assert_volume_delta_allowed(
                session, shop_id=shop, delta_bytes=10 * GB, audience="admin"
            )

    async def test_shop_without_ceiling_is_not_gated(self) -> None:
        shop = await self._shop(ceiling_gb=0, allow_oversell=False)
        async with self.Session() as session:
            await assert_volume_delta_allowed(session, shop_id=shop, delta_bytes=50 * GB, audience="admin")

    async def test_tenancy_other_shop_not_counted(self) -> None:
        a = await self._shop(shop_id=10, ceiling_gb=5, allow_oversell=False)
        b = await self._shop(shop_id=20, ceiling_gb=5, allow_oversell=False)
        await self._service(a, bytes_=5 * GB)
        await self._service(b, bytes_=1 * GB, bot_user_id=9200)
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, a), 5 * GB)
            self.assertEqual(await sum_allocated_bytes(session, b), 1 * GB)
            await assert_volume_delta_allowed(
                session, shop_id=b, delta_bytes=3 * GB, audience="admin"
            )

    async def test_only_owner_changes_oversell_switch(self) -> None:
        profile = SimpleNamespace(allow_volume_oversell=False)
        apply_oversell_switch(profile, shown=True, allow=True, actor_is_owner=False)
        self.assertFalse(profile.allow_volume_oversell)
        apply_oversell_switch(profile, shown=True, allow=True, actor_is_owner=True)
        self.assertTrue(profile.allow_volume_oversell)
        # The switch can be turned off: an unchecked box posts only the marker.
        apply_oversell_switch(profile, shown=True, allow=False, actor_is_owner=True)
        self.assertFalse(profile.allow_volume_oversell)
        # A form without the switch leaves it alone.
        apply_oversell_switch(profile, shown=False, allow=True, actor_is_owner=True)
        self.assertFalse(profile.allow_volume_oversell)

    def test_threshold_levels(self) -> None:
        self.assertEqual(threshold_level(79 * GB, 100 * GB), 0)
        self.assertEqual(threshold_level(80 * GB, 100 * GB), 1)
        self.assertEqual(threshold_level(100 * GB, 100 * GB), 2)


class OversellSwitchTests(VolumeHarness):
    async def _allow(self, shop_id: int) -> bool:
        async with self.Session() as session:
            return bool(
                await session.scalar(
                    select(ResellerProfile.allow_volume_oversell).where(ResellerProfile.user_id == shop_id)
                )
            )

    async def test_checked_create_switch_turns_oversell_on_for_the_owner_only(self) -> None:
        await self._shop(shop_id=10)
        await self._shop(shop_id=20)
        async with self.Session() as session:
            await apply_create_form_oversell(session, 10, form_value="1", actor_is_owner=True)
            await apply_create_form_oversell(session, 20, form_value="1", actor_is_owner=False)
        self.assertTrue(await self._allow(10))
        self.assertFalse(await self._allow(20))

    async def test_unchecked_create_switch_and_payg_change_nothing(self) -> None:
        await self._shop(shop_id=10)
        await self._shop(shop_id=20, payg=True)
        async with self.Session() as session:
            await apply_create_form_oversell(session, 10, form_value=None, actor_is_owner=True)
            await apply_create_form_oversell(session, 20, form_value="1", actor_is_owner=True)
            await apply_create_form_oversell(session, None, form_value="1", actor_is_owner=True)
        self.assertFalse(await self._allow(10))
        self.assertFalse(await self._allow(20))

    async def test_bot_toggle_sets_both_ways_and_refuses_payg_and_non_owner(self) -> None:
        await self._shop(shop_id=10)
        await self._shop(shop_id=20, payg=True)
        async with self.Session() as session:
            self.assertTrue(await set_shop_oversell(session, 10, allow=True, actor_is_owner=True))
            self.assertTrue(await self._allow(10))
            self.assertFalse(await set_shop_oversell(session, 10, allow=False, actor_is_owner=True))
            self.assertFalse(await self._allow(10))
            with self.assertRaises(ValueError):
                await set_shop_oversell(session, 10, allow=True, actor_is_owner=False)
            with self.assertRaises(ValueError):
                await set_shop_oversell(session, 20, allow=True, actor_is_owner=True)
            with self.assertRaises(ValueError):
                await set_shop_oversell(session, 99, allow=True, actor_is_owner=True)

    def test_reseller_card_button_shows_state_and_hides_for_payg(self) -> None:
        from app.bot import keyboards as kb

        def labels(markup) -> dict[str, str]:
            return {b.callback_data: b.text for row in markup.inline_keyboard for b in row}

        self.assertIn("خاموش", labels(kb.admin_reseller_actions(7, oversell=False))["adm:resellers:osell:7"])
        self.assertIn("روشن", labels(kb.admin_reseller_actions(7, oversell=True))["adm:resellers:osell:7"])
        self.assertNotIn("adm:resellers:osell:7", labels(kb.admin_reseller_actions(7, oversell=None)))


class PathGateTests(VolumeHarness):
    async def test_loyalty_traffic_rejected_when_full(self) -> None:
        from app.services.loyalty import apply_service_reward

        shop = await self._shop(ceiling_gb=2, allow_oversell=False)
        svc_id = await self._service(shop, bytes_=2 * GB)
        async with self.Session() as session:
            service = await session.get(UserService, svc_id)
            user = await session.get(BotUser, service.bot_user_id)
            with (
                patch(
                    "app.services.service_cancellations.lock_service_mutation",
                    new=AsyncMock(),
                ),
                patch(
                    "app.services.loyalty.service_shop_id",
                    new=AsyncMock(return_value=shop),
                ),
                patch(
                    "app.services.users.current_shop_reseller_id",
                    return_value=shop,
                ),
                patch(
                    "app.services.pasarguard.get_pg_for_reseller",
                    new=AsyncMock(
                        return_value=SimpleNamespace(
                            get_user_by_id=AsyncMock(
                                return_value={"data_limit": 2 * GB, "status": "active"}
                            ),
                            modify_user_by_id=AsyncMock(),
                        )
                    ),
                ),
            ):
                with self.assertRaises(ValueError) as ctx:
                    await apply_service_reward(
                        session, user, service, "traffic_gb", 1
                    )
            self.assertEqual(str(ctx.exception), CUSTOMER_UNAVAILABLE_MSG)

    async def test_addon_volume_rejected_when_full(self) -> None:
        from app.services.service_addons import KIND_VOLUME, _apply_pack_to_service

        shop = await self._shop(ceiling_gb=2, allow_oversell=False)
        svc_id = await self._service(shop, bytes_=2 * GB)
        async with self.Session() as session:
            service = await session.get(UserService, svc_id)
            with patch(
                "app.services.pasarguard.get_pg_for_reseller",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        get_user_by_id=AsyncMock(
                            return_value={"data_limit": 2 * GB, "status": "active"}
                        ),
                        modify_user_by_id=AsyncMock(),
                    )
                ),
            ):
                with self.assertRaises(ValueError) as ctx:
                    await _apply_pack_to_service(
                        session,
                        service,
                        kind=KIND_VOLUME,
                        amount=1.0,
                        order_reseller_id=shop,
                    )
            self.assertEqual(str(ctx.exception), CUSTOMER_UNAVAILABLE_MSG)

    async def test_buy_extra_gb_raises_ceiling_not_allocation(self) -> None:
        """buy_extra_gb increases the subscription ceiling; allocation gate does not block it."""
        from app.services.reseller_capacity import buy_extra_gb

        shop = await self._shop(ceiling_gb=2, allow_oversell=False, extra_gb=0)
        await self._service(shop, bytes_=2 * GB)
        async with self.Session() as session:
            user = await session.get(BotUser, shop)
            user.wallet_balance = 1_000_000
            profile = await session.scalar(
                select(ResellerProfile).where(ResellerProfile.user_id == shop)
            )
            plan = SimpleNamespace(
                allow_buy_extra=True,
                billing_mode="fixed",
                plan_kind="subscription",
                extra_gb_price=1000,
            )
            pg = AsyncMock()
            pg.get_admin = AsyncMock(return_value={"data_limit": 2 * GB})
            pg.modify_admin = AsyncMock()
            with (
                patch("app.services.reseller_capacity.plan_allows_buy_extra", return_value=True),
                patch("app.services.reseller_capacity.is_payg", return_value=False),
                patch("app.services.pasarguard.get_pg", return_value=pg),
                patch(
                    "app.services.pg_admin_subscription.record_unit_extra",
                    new=AsyncMock(),
                ),
            ):
                await buy_extra_gb(
                    session, user=user, profile=profile, plan=plan, gb=1
                )
            pg.modify_admin.assert_awaited()


class HoldTests(VolumeHarness):
    async def _hold_rows(self) -> list[tuple[str, int]]:
        async with self.Session() as session:
            rows = await session.execute(select(VolumeHold.hold_key, VolumeHold.bytes).order_by(VolumeHold.id))
            return [(k, int(b)) for k, b in rows.all()]

    async def test_second_hold_is_refused_while_the_first_is_live(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        async with self.Session() as session:
            await hold_volume(session, shop_id=shop, delta_bytes=4 * GB, key="a")
        async with self.Session() as session:
            with self.assertRaises(VolumeAllocationError):
                await hold_volume(session, shop_id=shop, delta_bytes=2 * GB, key="b")
        self.assertEqual(await self._hold_rows(), [("a", 4 * GB)])

    async def test_concurrent_holds_cannot_both_fit(self) -> None:
        shop = await self._shop(ceiling_gb=5)

        async def take(key: str) -> bool:
            async with self.Session() as session:
                try:
                    await hold_volume(session, shop_id=shop, delta_bytes=3 * GB, key=key)
                except VolumeAllocationError:
                    return False
                return True

        results = await asyncio.gather(take("a"), take("b"))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(await self._hold_rows()), 1)

    async def test_retry_with_the_same_key_does_not_count_twice(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        for _ in range(2):
            async with self.Session() as session:
                await hold_volume(session, shop_id=shop, delta_bytes=3 * GB, key="order:1")
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 3 * GB)

    async def test_expired_hold_stops_counting(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        async with self.Session() as session:
            session.add(
                VolumeHold(
                    shop_id=shop, hold_key="old", bytes=5 * GB,
                    expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
                )
            )
            await session.commit()
            self.assertEqual(await sum_allocated_bytes(session, shop), 0)
            await hold_volume(session, shop_id=shop, delta_bytes=5 * GB, key="new")
        self.assertEqual(await self._hold_rows(), [("new", 5 * GB)])

    async def test_release_frees_the_volume(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        async with self.Session() as session:
            hold = await hold_volume(session, shop_id=shop, delta_bytes=5 * GB, key="a")
            await release_volume_hold(session, hold)
            await session.commit()
            self.assertEqual(await sum_allocated_bytes(session, shop), 0)

    async def test_other_shops_holds_are_not_counted(self) -> None:
        a = await self._shop(shop_id=10, ceiling_gb=5)
        b = await self._shop(shop_id=20, ceiling_gb=5)
        async with self.Session() as session:
            await hold_volume(session, shop_id=a, delta_bytes=5 * GB, key="a")
            await hold_volume(session, shop_id=b, delta_bytes=5 * GB, key="b")
            self.assertEqual(await sum_allocated_bytes(session, b), 5 * GB)

    async def test_limit_change_holds_only_the_increase(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        async with self.Session() as session:
            self.assertIsNone(
                await hold_limit_change(session, shop_id=shop, current_limit=3 * GB, new_limit=2 * GB, key="down")
            )
            await hold_limit_change(session, shop_id=shop, current_limit=2 * GB, new_limit=5 * GB, key="up")
            self.assertEqual(await sum_allocated_bytes(session, shop), 3 * GB)
            with self.assertRaises(ProvisionError):
                await hold_limit_change(session, shop_id=shop, current_limit=2 * GB, new_limit=0, key="unlimited")
            with self.assertRaises(ProvisionError):
                await hold_limit_change(session, shop_id=shop, current_limit=2 * GB, new_limit=None, key="unknown")

    async def test_quantity_is_held_in_full(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        async with self.Session() as session:
            with self.assertRaises(ProvisionError) as ctx:
                await hold_create_volume(
                    session, shop_id=shop, data_limit=2 * GB, quantity=3, key="bulk", audience="customer"
                )
            self.assertEqual(ctx.exception.message, CUSTOMER_UNAVAILABLE_MSG)


class DeliveryHoldTests(VolumeHarness):
    async def _order(self, shop: int, *, gb: int = 3) -> int:
        async with self.Session() as session:
            session.add(BotUser(id=500, telegram_id=500, role=Role.USER.value, referral_code="c500", reseller_id=shop))
            plan = Plan(name="p", price=1000, data_limit_gb=gb, duration_days=30, pg_group_ids="1")
            session.add(plan)
            await session.flush()
            order = Order(user_id=500, plan_id=plan.id, reseller_id=shop, amount=1000, status=OrderStatus.PAID.value)
            session.add(order)
            await session.commit()
            return int(order.id)

    async def _deliver(self, order_id: int, create_user: AsyncMock) -> SimpleNamespace:
        from app.services.orders import deliver_order

        pg = SimpleNamespace(
            _login_username="admin10", create_user=create_user, delete_user_by_id=AsyncMock(),
        )
        async with self.Session() as session:
            order = await session.get(Order, order_id)
            with (
                patch("app.services.pasarguard.get_pg_for_reseller", new=AsyncMock(return_value=pg)),
                patch("app.services.orders._reseller_pg_link", new=AsyncMock(return_value=("admin10", None))),
                patch("app.services.orders.assert_provision_create", new=AsyncMock()),
                patch("app.services.orders.generate_pg_username", new=AsyncMock(return_value="u77")),
            ):
                try:
                    await deliver_order(session, order)
                except Exception as exc:  # the outcome is read back from the database below
                    pg.error = exc
        return pg

    async def _state(self, order_id: int) -> tuple[str, int, int]:
        async with self.Session() as session:
            status = (await session.get(Order, order_id)).status
            held = await session.scalar(select(func.coalesce(func.sum(VolumeHold.bytes), 0)))
            refunds = await session.scalar(select(func.count()).select_from(WalletTransaction))
            return status, int(held), int(refunds)

    async def test_success_moves_the_volume_from_hold_to_service(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        order_id = await self._order(shop)
        await self._deliver(order_id, AsyncMock(return_value={"id": 77, "username": "u77", "data_limit": 3 * GB}))
        self.assertEqual(await self._state(order_id), (OrderStatus.DELIVERED.value, 0, 0))
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 3 * GB)

    async def test_unknown_panel_outcome_keeps_the_hold_and_refunds_nothing(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        order_id = await self._order(shop)
        await self._deliver(order_id, AsyncMock(side_effect=PasarGuardError("timeout", status_code=None)))
        self.assertEqual(await self._state(order_id), (OrderStatus.PAID.value, 3 * GB, 0))

    async def test_panel_refusal_releases_the_hold(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        order_id = await self._order(shop)
        await self._deliver(order_id, AsyncMock(side_effect=PasarGuardError("bad", status_code=422)))
        self.assertEqual(await self._state(order_id), (OrderStatus.PAID.value, 0, 0))

    async def test_full_shop_never_calls_the_panel(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        await self._service(shop, bytes_=4 * GB)
        order_id = await self._order(shop)
        create_user = AsyncMock()
        pg = await self._deliver(order_id, create_user)
        create_user.assert_not_awaited()
        self.assertEqual(str(pg.error), CUSTOMER_UNAVAILABLE_MSG)
        self.assertEqual(await self._state(order_id), (OrderStatus.PAID.value, 0, 0))


class RenewalHoldTests(VolumeHarness):
    async def test_renewal_holds_only_what_it_adds(self) -> None:
        from app.services.orders import _hold_renewal_volume, _RenewalRun

        shop = await self._shop(ceiling_gb=5)
        svc_id = await self._service(shop, bytes_=2 * GB)
        order = SimpleNamespace(id=8, reseller_id=shop)
        plan = SimpleNamespace(pg_template_id=None)
        run = _RenewalRun(snapshot=None, had_saved_target=False, write_attempted=False)
        async with self.Session() as session:
            service = await session.get(UserService, svc_id)
            await _hold_renewal_volume(session, order, service, plan, None, run, data_limit=4 * GB)
            self.assertEqual(await sum_allocated_bytes(session, shop), 4 * GB)
            with self.assertRaises(ValueError) as ctx:
                await _hold_renewal_volume(session, order, service, plan, None, run, data_limit=8 * GB)
            self.assertEqual(str(ctx.exception), CUSTOMER_UNAVAILABLE_MSG)


class AddonHoldTests(VolumeHarness):
    async def _apply(self, shop: int, svc_id: int, modify: AsyncMock) -> None:
        from app.services.service_addons import KIND_VOLUME, _apply_pack_to_service

        pg = SimpleNamespace(
            get_user_by_id=AsyncMock(return_value={"data_limit": 2 * GB, "status": "active"}),
            modify_user_by_id=modify,
        )
        async with self.Session() as session:
            service = await session.get(UserService, svc_id)
            with patch("app.services.pasarguard.get_pg_for_reseller", new=AsyncMock(return_value=pg)):
                try:
                    await _apply_pack_to_service(
                        session, service, kind=KIND_VOLUME, amount=1.0, order_reseller_id=shop, order_id=5,
                    )
                    await session.commit()
                except PasarGuardError:
                    await session.rollback()

    async def test_success_counts_once(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        svc_id = await self._service(shop, bytes_=2 * GB)
        await self._apply(shop, svc_id, AsyncMock(return_value={}))
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 3 * GB)

    async def test_unknown_outcome_keeps_the_hold(self) -> None:
        shop = await self._shop(ceiling_gb=5)
        svc_id = await self._service(shop, bytes_=2 * GB)
        await self._apply(shop, svc_id, AsyncMock(side_effect=PasarGuardError("timeout", status_code=None)))
        async with self.Session() as session:
            self.assertEqual(await sum_allocated_bytes(session, shop), 3 * GB)
            self.assertEqual(await session.scalar(select(VolumeHold.hold_key)), "addon:5")


class OversoldListAndMigrationTests(VolumeHarness):
    def test_postgres_default_is_a_boolean_literal(self) -> None:
        ddl = str(CreateTable(ResellerProfile.__table__).compile(dialect=postgresql.dialect()))
        self.assertIn("allow_volume_oversell BOOLEAN DEFAULT true", ddl)


    async def test_owner_oversold_list(self) -> None:
        a = await self._shop(shop_id=10, ceiling_gb=5, allow_oversell=False)
        b = await self._shop(shop_id=20, ceiling_gb=5, allow_oversell=False)
        await self._service(a, bytes_=8 * GB)
        await self._service(b, bytes_=1 * GB, bot_user_id=9200)
        async with self.Session() as session:
            oversold = await list_oversold_shop_ids(session)
            self.assertEqual(oversold, [a])

    def test_migration_upgrade_twice_and_downgrade(self) -> None:
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location(
            "vol_mig", ROOT / "alembic/versions/0065_allow_volume_oversell.py"
        )
        migration = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE reseller_profiles ("
                    "id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)"
                )
            )
            conn.execute(text("INSERT INTO reseller_profiles VALUES (1, 10)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                row = conn.execute(
                    text("SELECT allow_volume_oversell FROM reseller_profiles")
                ).one()
                self.assertEqual(int(row[0]), 1)
                self.assertIn("volume_holds", sa.inspect(conn).get_table_names())
                migration.downgrade()
                cols = {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")}
                self.assertNotIn("allow_volume_oversell", cols)
                self.assertNotIn("volume_holds", sa.inspect(conn).get_table_names())
                self.assertEqual(
                    conn.execute(text("SELECT user_id FROM reseller_profiles")).scalar(),
                    10,
                )
                migration.upgrade()
                self.assertIn(
                    "allow_volume_oversell",
                    {c["name"] for c in sa.inspect(conn).get_columns("reseller_profiles")},
                )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
