"""Shop Mini App: only the shop owner gets reseller ops/review; everyone else stays a customer."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_feeds import register_miniapp_feeds
from app.api.miniapp_pages import register_miniapp_pages
from app.api.miniapp_review import register_miniapp_review
from app.db.models import Base, BotUser, BotUserShop, Order, Payment, ResellerProfile
from app.services.miniapp_shop import PLATFORM_SWITCH, SHOP_OWNER_ONLY, SHOP_SWITCH, new_shop_key
from app.services.secret_box import seal_bot_token
from app.services.users import clear_settings_cache, set_setting
from tests.test_miniapp_shop_scope import MAIN_TOKEN, OTHER_SHOP_TOKEN, SHOP_TOKEN, signed

SHOP, OTHER_SHOP = 5, 6
KEY, OTHER_KEY = new_shop_key(), new_shop_key()
OWNER_TG, OTHER_OWNER_TG = 5005, 6006
CUSTOMER_TG, BOT_ADMIN_TG, ADMIN_TG = 1010, 2000, 7001
PERMS = '["orders","payments","miniapp","dashboard"]'


class ShopOwnerMiniAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=SHOP, telegram_id=OWNER_TG, referral_code="o5", role="reseller", full_name="Owner"),
                BotUser(id=OTHER_SHOP, telegram_id=OTHER_OWNER_TG, referral_code="o6", role="reseller"),
                BotUser(id=10, telegram_id=CUSTOMER_TG, referral_code="c10", role="user"),
                BotUser(id=11, telegram_id=BOT_ADMIN_TG, referral_code="ba11", role="user"),
                BotUser(id=20, telegram_id=ADMIN_TG, referral_code="a20", role="admin"),
                BotUserShop(bot_user_id=10, shop_id=SHOP),
                BotUserShop(bot_user_id=OTHER_SHOP, shop_id=SHOP),
                BotUserShop(bot_user_id=SHOP, shop_id=OTHER_SHOP),
                BotUserShop(bot_user_id=20, shop_id=SHOP),
                ResellerProfile(
                    id=1,
                    user_id=SHOP,
                    is_active=True,
                    setup_completed_at=now,
                    bot_token=seal_bot_token(SHOP_TOKEN),
                    miniapp_key=KEY,
                    web_permissions=PERMS,
                    bot_admin_ids=str(BOT_ADMIN_TG),
                    bot_username="shop5bot",
                ),
                ResellerProfile(
                    id=2,
                    user_id=OTHER_SHOP,
                    is_active=True,
                    setup_completed_at=now,
                    bot_token=seal_bot_token(OTHER_SHOP_TOKEN),
                    miniapp_key=OTHER_KEY,
                    web_permissions=PERMS,
                ),
                Order(id=20, user_id=10, amount=50_000, status="awaiting_approval", reseller_id=SHOP),
                Order(id=21, user_id=10, amount=50_000, status="awaiting_approval", reseller_id=OTHER_SHOP),
                Payment(
                    id=11,
                    user_id=10,
                    order_id=20,
                    amount=50_000,
                    status="pending",
                    receipt_file_id="SHOP_FILE",
                ),
                Payment(
                    id=12,
                    user_id=10,
                    amount=40_000,
                    status="pending",
                    receipt_file_id="SHOP_TOPUP",
                    is_wallet_topup=True,
                    wallet_shop_id=SHOP,
                ),
                Payment(
                    id=13,
                    user_id=10,
                    order_id=21,
                    amount=50_000,
                    status="pending",
                    receipt_file_id="OTHER_FILE",
                ),
                Payment(
                    id=10,
                    user_id=10,
                    amount=70_000,
                    status="pending",
                    receipt_file_id="PLATFORM_FILE",
                    is_wallet_topup=True,
                ),
            ])
            await session.commit()
            await set_setting(session, PLATFORM_SWITCH, "1")
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=SHOP)
            await set_setting(session, SHOP_SWITCH, "1", reseller_id=OTHER_SHOP)
        clear_settings_cache()
        settings = SimpleNamespace(
            miniapp_enabled=True,
            bot_token=MAIN_TOKEN,
            currency="تومان",
            miniapp_url="https://panel.example.com/miniapp/",
        )
        self.patches = [
            patch("app.services.miniapp_auth.get_settings", return_value=settings),
            patch("app.api.miniapp_pages.get_settings", return_value=settings),
            patch("app.services.miniapp_shop.get_settings", return_value=settings),
            patch("app.services.receipt_review.fetch_telegram_file", AsyncMock(return_value=(b"\xff\xd8", "image/jpeg"))),
            patch("app.services.receipt_files.get_settings", return_value=SimpleNamespace(bot_token=MAIN_TOKEN)),
            patch("app.services.receipt_review.open_notify_bot_for_user", AsyncMock(return_value=(SimpleNamespace(session=SimpleNamespace(close=AsyncMock())), True))),
            patch("app.services.receipt_review.notify_payer", AsyncMock(return_value=True)),
            patch("app.services.payment_review_actions.send_delivery_to_user", AsyncMock(return_value="ok")),
            patch("app.services.notifications.notify_wallet_topup_ok", AsyncMock()),
            patch("app.services.orders._sync_payment_review_messages", AsyncMock()),
            patch("app.api.home_pages._reseller_shop_stats", AsyncMock(return_value={"users": 3, "pending": 1})),
            patch(
                "app.services.resellers.get_reseller_panel_base_url",
                AsyncMock(return_value="https://panel.example.com"),
            ),
            patch(
                "app.api.miniapp_pages.get_reseller_panel_base_url",
                AsyncMock(return_value="https://panel.example.com"),
            ),
        ]
        for p in self.patches:
            p.start()

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_miniapp_pages(app, render=lambda *_a, **_k: HTMLResponse("page"), get_db=get_db)
        register_miniapp_feeds(app, get_db=get_db)
        register_miniapp_review(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        clear_settings_cache()
        await self.engine.dispose()

    async def get(self, path: str, *, tg_id: int, token: str = SHOP_TOKEN, key: str | None = KEY) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": signed(token, tg_id)}
        if key:
            headers["X-Mini-Shop"] = key
        return await self.client.get(path, headers=headers)

    async def post(self, path: str, *, tg_id: int, token: str = SHOP_TOKEN, key: str | None = KEY) -> httpx.Response:
        headers = {"X-Telegram-Init-Data": signed(token, tg_id)}
        if key:
            headers["X-Mini-Shop"] = key
        return await self.client.post(path, headers=headers)

    async def test_owner_gets_reseller_shell_with_ops(self) -> None:
        me = (await self.get("/api/mini/me", tg_id=OWNER_TG)).json()
        self.assertEqual(me["persona"], "reseller")
        self.assertEqual(me["user"]["role"], "reseller")
        self.assertEqual([n["id"] for n in me["nav"]], ["home", "services", "shop", "ops", "account"])
        self.assertTrue(me["commerce_allowed"])
        self.assertEqual(me["panel_base"], "https://panel.example.com")

    async def test_customer_bot_admin_other_reseller_and_platform_admin_stay_customers(self) -> None:
        for tg in (CUSTOMER_TG, BOT_ADMIN_TG, OTHER_OWNER_TG, ADMIN_TG):
            with self.subTest(tg=tg):
                me = (await self.get("/api/mini/me", tg_id=tg)).json()
                self.assertEqual(me["persona"], "user")
                self.assertEqual([n["id"] for n in me["nav"]], ["home", "services", "shop", "account"])
                self.assertEqual(me["panel_base"], "")

    async def test_owner_ops_are_this_shop_only(self) -> None:
        body = (await self.get("/api/mini/ops", tg_id=OWNER_TG)).json()
        self.assertEqual(body["shop"]["bot_username"], "shop5bot")
        self.assertEqual(body["stats"]["users"], 3)
        self.assertTrue(body["can_review"])

    async def test_non_owners_cannot_open_ops(self) -> None:
        for tg in (CUSTOMER_TG, BOT_ADMIN_TG, OTHER_OWNER_TG, ADMIN_TG):
            with self.subTest(tg=tg):
                response = await self.get("/api/mini/ops", tg_id=tg)
                self.assertEqual((response.status_code, response.json()["detail"]), (403, SHOP_OWNER_ONLY))

    async def test_owner_review_queue_is_this_shop_only(self) -> None:
        items = (await self.get("/api/mini/review", tg_id=OWNER_TG)).json()["items"]
        self.assertEqual(sorted(row["id"] for row in items), [11, 12])
        self.assertNotIn("PLATFORM_FILE", (await self.get("/api/mini/review", tg_id=OWNER_TG)).text)
        self.assertNotIn("OTHER_FILE", (await self.get("/api/mini/review", tg_id=OWNER_TG)).text)

    async def test_non_owners_cannot_review(self) -> None:
        for tg in (CUSTOMER_TG, BOT_ADMIN_TG, OTHER_OWNER_TG, ADMIN_TG):
            with self.subTest(tg=tg):
                response = await self.get("/api/mini/review", tg_id=tg)
                self.assertEqual(response.status_code, 403)

    async def test_owner_cannot_use_another_shops_miniapp_key_for_ops(self) -> None:
        response = await self.get(
            "/api/mini/ops",
            tg_id=OWNER_TG,
            token=OTHER_SHOP_TOKEN,
            key=OTHER_KEY,
        )
        self.assertEqual(response.status_code, 403)

    async def test_platform_miniapp_still_gives_owner_reseller_shell(self) -> None:
        me = (await self.get("/api/mini/me", tg_id=OWNER_TG, token=MAIN_TOKEN, key=None)).json()
        self.assertEqual(me["persona"], "reseller")
        self.assertIn("ops", [n["id"] for n in me["nav"]])

    async def test_ops_without_dashboard_hides_stats(self) -> None:
        # with_shop_settings always re-adds dashboard to non-empty ACLs, and an empty
        # ACL turns the shop Mini App off — so gate the filter at the payload helper.
        from app.services.miniapp_shop_ops import reseller_ops_payload

        profile = SimpleNamespace(
            user_id=SHOP,
            is_active=True,
            bot_username="shop5bot",
            billing_mode="payg",
            billing_balance=12_000,
            billing_suspended_at=None,
            web_permissions='["miniapp","payments"]',
        )
        with patch(
            "app.services.miniapp_shop_ops.shop_feature_allowed",
            side_effect=lambda *, key, profile=None: key in {"miniapp", "payments"},
        ), patch(
            "app.services.miniapp_shop_ops.resolve_shop_permissions_from_profile",
            return_value=["miniapp", "payments"],
        ), patch(
            "app.services.miniapp_shop_ops.permitted_ops_panel_links",
            return_value=[{"id": "finance", "label": "مالی", "path": "/finance"}],
        ):
            body = reseller_ops_payload(
                profile,
                stats={"users": 9, "pending": 2, "revenue": 100},
                panel_base="https://panel.example.com",
            )
        self.assertEqual(body["stats"]["users"], 0)
        self.assertEqual(body["stats"]["pending"], 0)
        self.assertIsNone(body["shop"]["billing"])
        self.assertEqual(body["permissions"], ["miniapp", "payments"])
        self.assertEqual([row["id"] for row in body["panel_links"]], ["finance"])

    async def test_ops_without_payments_omits_finance_and_review(self) -> None:
        async with self.Session() as session:
            profile = await session.get(ResellerProfile, 1)
            profile.web_permissions = '["dashboard","orders","tickets","miniapp","shop_settings"]'
            await session.commit()
        body = (await self.get("/api/mini/ops", tg_id=OWNER_TG)).json()
        self.assertIn("dashboard", body["permissions"])
        self.assertNotIn("payments", body["permissions"])
        self.assertFalse(body["can_review"])
        self.assertEqual(body["stats"]["users"], 3)
        link_ids = {row["id"] for row in body["panel_links"]}
        self.assertNotIn("finance", link_ids)
        self.assertIn("home", link_ids)
        self.assertIn("tickets", link_ids)
        self.assertIn("shop", link_ids)
        review = await self.get("/api/mini/review", tg_id=OWNER_TG)
        self.assertEqual(review.status_code, 403)

    async def test_foreign_and_platform_receipts_are_404_unchanged(self) -> None:
        for path, payment_id in (
            ("/api/mini/review/13/approve", 13),
            ("/api/mini/review/13/reject", 13),
            ("/api/mini/review/13/receipt", 13),
            ("/api/mini/review/10/approve", 10),
            ("/api/mini/review/10/reject", 10),
            ("/api/mini/review/10/receipt", 10),
        ):
            with self.subTest(path=path):
                if path.endswith("/receipt"):
                    response = await self.get(path, tg_id=OWNER_TG)
                else:
                    response = await self.post(path, tg_id=OWNER_TG)
                self.assertEqual(response.status_code, 404)
                async with self.Session() as session:
                    payment = await session.get(Payment, payment_id)
                    self.assertEqual(payment.status, "pending")

    async def test_approve_twice_credits_once(self) -> None:
        first = await self.post("/api/mini/review/12/approve", tg_id=OWNER_TG)
        self.assertEqual(first.status_code, 200, first.text)
        second = await self.post("/api/mini/review/12/approve", tg_id=OWNER_TG)
        self.assertIn(second.status_code, (200, 400))
        async with self.Session() as session:
            payment = await session.get(Payment, 12)
            self.assertNotEqual(payment.status, "pending")


class OwnerCredsMenuTests(unittest.TestCase):
    def test_submenu_creds_only_when_flagged(self) -> None:
        from app.bot.reply_keyboards import REPLY_ACTION_CREDS, _reseller_submenu_entries
        from tests.test_bot_ui_keyboard_snapshots import sample_reseller_profile

        profile = sample_reseller_profile(billing_mode="fixed")
        with patch("app.bot.reply_keyboards._reseller_capacity_entries", return_value=[]):
            plain = {key for key, _ in _reseller_submenu_entries(profile, ui={})}
            owner = {
                key
                for key, _
                in _reseller_submenu_entries(profile, show_owner_creds=True, ui={})
            }
        self.assertNotIn(REPLY_ACTION_CREDS, plain)
        self.assertIn(REPLY_ACTION_CREDS, owner)

    def test_is_shop_profile_owner(self) -> None:
        from app.services.reseller_access import is_shop_profile_owner

        user = SimpleNamespace(id=5)
        self.assertTrue(is_shop_profile_owner(user, 5))
        self.assertFalse(is_shop_profile_owner(user, 9))
        self.assertFalse(is_shop_profile_owner(user, None))


if __name__ == "__main__":
    unittest.main()
