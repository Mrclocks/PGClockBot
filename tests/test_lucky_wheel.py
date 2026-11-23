"""Lucky Wheel engine — security, tenant isolation, idempotency, RNG."""

from __future__ import annotations

import unittest
from tests.postgres_support import scoped_async_engine



class LuckyWheelEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.db import Base
        import app.db.models  # noqa: F401

        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def _user(self, session, tid: int, code: str, *, reseller_id=None, points=100):
        from app.db.models import BotUser, Role

        u = BotUser(
            telegram_id=tid,
            role=Role.USER.value,
            referral_code=code,
            reseller_id=reseller_id,
            wallet_balance=0,
            points_balance=points,
        )
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def _enable_wheel(self, session, *, reseller_id=None, cost=10, daily=10):
        from app.services.users import set_setting

        await set_setting(session, "loyalty_enabled", "1", reseller_id=reseller_id)
        await set_setting(session, "lucky_wheel_enabled", "1", reseller_id=reseller_id)
        await set_setting(
            session, "lucky_wheel_spin_cost_points", str(cost), reseller_id=reseller_id
        )
        await set_setting(
            session, "lucky_wheel_daily_limit", str(daily), reseller_id=reseller_id
        )
        await set_setting(
            session, "lucky_wheel_cooldown_seconds", "0", reseller_id=reseller_id
        )
        await set_setting(
            session, "lucky_wheel_free_spins_daily", "0", reseller_id=reseller_id
        )

    def test_weighted_draw_deterministic(self):
        from types import SimpleNamespace

        from app.services.lucky_wheel import weighted_draw

        prizes = [
            SimpleNamespace(weight=1, id=1, label="a"),
            SimpleNamespace(weight=3, id=2, label="b"),
        ]
        self.assertEqual(weighted_draw(prizes, random_fn=lambda n: 0).id, 1)
        self.assertEqual(weighted_draw(prizes, random_fn=lambda n: 1).id, 2)
        self.assertEqual(weighted_draw(prizes, random_fn=lambda n: 3).id, 2)

    def test_weighted_draw_distribution_sanity(self):
        from types import SimpleNamespace

        from app.services.lucky_wheel import weighted_draw

        prizes = [
            SimpleNamespace(weight=1, id=1),
            SimpleNamespace(weight=9, id=2),
        ]
        counts = {1: 0, 2: 0}
        # Inject sequential picks across full weight range
        for i in range(1000):
            pick = weighted_draw(prizes, random_fn=lambda n, i=i: i % n)
            counts[pick.id] += 1
        self.assertEqual(counts[1], 100)
        self.assertEqual(counts[2], 900)

    def test_settings_and_btn_presence(self):
        from app.services.button_styles import BUTTON_STYLE_CATALOG
        from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS

        for k in (
            "btn_loy_wheel",
            "lucky_wheel_enabled",
            "lucky_wheel_spin_cost_points",
            "lucky_wheel_daily_limit",
            "lucky_wheel_cooldown_seconds",
            "lucky_wheel_free_spins_daily",
            "loyalty_submenu_order",
        ):
            self.assertIn(k, DEFAULT_SETTINGS)
        keys = {f[0] for f in SETTING_GROUPS["متن دکمه‌های منو"]}
        self.assertIn("btn_loy_wheel", keys)
        ids = {b["id"] for b in BUTTON_STYLE_CATALOG}
        self.assertIn("loy_wheel", ids)

    def test_submenu_order_parse(self):
        from app.services.lucky_wheel import parse_submenu_order

        order = parse_submenu_order("loy_wheel,loy_points,bogus")
        self.assertEqual(order[0], "loy_wheel")
        self.assertEqual(order[1], "loy_points")
        self.assertIn("loy_history", order)
        self.assertNotIn("bogus", order)

    async def test_disabled_wheel_and_empty_pool(self):
        from app.services.lucky_wheel import spin, upsert_prize
        from app.services.users import set_setting

        async with self.Session() as session:
            u = await self._user(session, 501, "WHEEL001")
            await set_setting(session, "loyalty_enabled", "1", reseller_id=None)
            await set_setting(session, "lucky_wheel_enabled", "0", reseller_id=None)
            with self.assertRaises(ValueError):
                await spin(session, u, idempotency_key="k-disabled")

            await self._enable_wheel(session, reseller_id=None, cost=0)
            with self.assertRaises(ValueError):
                await spin(session, u, idempotency_key="k-empty")

            await upsert_prize(
                session,
                reseller_id=None,
                label="خالی",
                prize_type="none",
                prize_value=0,
                weight=1,
            )
            result = await spin(
                session, u, idempotency_key="k-ok", random_fn=lambda n: 0
            )
            self.assertEqual(result.prize_type, "none")
            self.assertFalse(result.replayed)

    async def test_idempotent_double_spin(self):
        from app.services.lucky_wheel import spin, upsert_prize

        async with self.Session() as session:
            u = await self._user(session, 502, "WHEEL002", points=50)
            await self._enable_wheel(session, reseller_id=None, cost=5)
            await upsert_prize(
                session,
                reseller_id=None,
                label="۵ امتیاز",
                prize_type="points",
                prize_value=5,
                weight=1,
            )
            r1 = await spin(session, u, idempotency_key="same-key", random_fn=lambda n: 0)
            await session.refresh(u)
            bal_after = int(u.points_balance)
            r2 = await spin(session, u, idempotency_key="same-key", random_fn=lambda n: 0)
            await session.refresh(u)
            self.assertTrue(r2.replayed)
            self.assertEqual(r1.spin.id, r2.spin.id)
            self.assertEqual(int(u.points_balance), bal_after)

    async def test_tenant_isolation(self):
        from app.db.models import BotUser, Role
        from app.services.lucky_wheel import list_active_pool, spin, upsert_prize

        async with self.Session() as session:
            owner = BotUser(
                telegram_id=9001,
                role=Role.RESELLER.value,
                referral_code="OWNER001",
                wallet_balance=0,
                points_balance=0,
            )
            session.add(owner)
            await session.commit()
            await session.refresh(owner)

            platform_user = await self._user(session, 503, "WHEEL003", points=100)
            shop_user = await self._user(
                session, 504, "WHEEL004", reseller_id=owner.id, points=100
            )

            await self._enable_wheel(session, reseller_id=None, cost=0)
            await self._enable_wheel(session, reseller_id=owner.id, cost=0)

            await upsert_prize(
                session,
                reseller_id=None,
                label="پلتفرم",
                prize_type="points",
                prize_value=3,
                weight=1,
            )
            await upsert_prize(
                session,
                reseller_id=owner.id,
                label="فروشگاه",
                prize_type="points",
                prize_value=7,
                weight=1,
            )

            plat_pool = await list_active_pool(session, reseller_id=None)
            shop_pool = await list_active_pool(session, reseller_id=owner.id)
            self.assertTrue(all(p.reseller_id is None for p in plat_pool))
            self.assertTrue(all(int(p.reseller_id) == int(owner.id) for p in shop_pool))
            self.assertEqual(plat_pool[0].label, "پلتفرم")
            self.assertEqual(shop_pool[0].label, "فروشگاه")

            r_plat = await spin(
                session, platform_user, idempotency_key="plat-1", random_fn=lambda n: 0
            )
            # The wheel belongs to the bot in use, not to the customer's attribution.
            from app.services.users import reset_shop_reseller_id, set_shop_reseller_id

            token = set_shop_reseller_id(owner.id)
            try:
                r_shop = await spin(
                    session, shop_user, idempotency_key="shop-1", random_fn=lambda n: 0
                )
            finally:
                reset_shop_reseller_id(token)
            self.assertEqual(r_plat.prize_value, 3)
            self.assertEqual(r_shop.prize_value, 7)
            self.assertIsNone(r_plat.spin.reseller_id)
            self.assertEqual(int(r_shop.spin.reseller_id), int(owner.id))

    async def test_acl_deny_cross_scope_archive(self):
        from app.db.models import BotUser, Role
        from app.services.lucky_wheel import archive_prize, upsert_prize

        async with self.Session() as session:
            owner = BotUser(
                telegram_id=9002,
                role=Role.RESELLER.value,
                referral_code="OWNER002",
                wallet_balance=0,
                points_balance=0,
            )
            session.add(owner)
            await session.commit()
            await session.refresh(owner)

            prize = await upsert_prize(
                session,
                reseller_id=None,
                label="پلتفرم",
                prize_type="none",
                prize_value=0,
                weight=1,
            )
            # Shop staff must not archive platform prize
            ok = await archive_prize(session, prize.id, reseller_id=owner.id)
            self.assertFalse(ok)
            # Platform scope can
            ok2 = await archive_prize(session, prize.id, reseller_id=None)
            self.assertTrue(ok2)


class LuckyWheelMenuTests(unittest.TestCase):
    def test_customer_submenu_includes_wheel(self):
        from app.bot.keyboards import (
            REPLY_ACTION_LOY_WHEEL,
            loyalty_reply_keyboard,
            reply_action_map,
        )

        ui = {
            "menu_order": "shop,loyalty",
            "loyalty_enabled": "1",
            "lucky_wheel_enabled": "1",
            "btn_loyalty": "باشگاه",
            "btn_loy_wheel": "🎡 چرخ شانس",
            "btn_menu_home": "🏠 منوی اصلی",
            "btn_back": "⬅️ بازگشت",
            "loyalty_submenu_order": "loy_referral,loy_points,loy_rewards,loy_wheel,loy_history",
        }
        mapping = reply_action_map("user", ui=ui, include_submenus=True)
        self.assertEqual(mapping["🎡 چرخ شانس"], REPLY_ACTION_LOY_WHEEL)
        flat = [b.text for row in loyalty_reply_keyboard(ui).keyboard for b in row]
        self.assertIn("🎡 چرخ شانس", flat)
        # Between rewards and history by default order
        self.assertLess(flat.index("🎁 جوایز"), flat.index("🎡 چرخ شانس"))
        self.assertLess(flat.index("🎡 چرخ شانس"), flat.index("📜 تاریخچه"))


if __name__ == "__main__":
    unittest.main()
