"""Mini App addon checkout and the admin's payment-purpose notifications."""

from datetime import datetime, timezone
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.miniapp_feeds import register_miniapp_feeds
from app.api.miniapp_pages import register_miniapp_pages
from app.db.models import (
    Base, BotUser, Order, Payment, ServiceAddonPack, UserService,
)
from app.services.notifications import (
    _pending_order_detail_lines, notify_new_order, notify_new_subscription,
    notify_pending_approval,
)
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id


class MiniAppAddonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.user = BotUser(id=1, telegram_id=101, referral_code="buyer", role="user", wallet_balance=10000)
        self.other = BotUser(id=2, telegram_id=102, referral_code="other", role="user", wallet_balance=10000)
        self.svc = UserService(id=10, bot_user_id=1, pg_user_id=110, pg_username="my-service")
        self.foreign_svc = UserService(id=20, bot_user_id=2, pg_user_id=120, pg_username="private-service")
        self.volume = ServiceAddonPack(id=1, name="بسته حجم", kind="volume", amount=2.5, price=1000)
        self.duration = ServiceAddonPack(id=2, name="بسته زمان", kind="duration", amount=7, price=2000)
        self.foreign_pack = ServiceAddonPack(id=3, name="private-pack", kind="volume", amount=10, price=1, owner_reseller_id=2)
        self.inactive = ServiceAddonPack(id=4, name="inactive", kind="volume", amount=10, price=1, is_active=False)
        self.session.add_all([
            self.user, self.other, self.svc, self.foreign_svc,
            self.volume, self.duration, self.foreign_pack, self.inactive,
        ])
        await self.session.commit()
        self.shop_token = set_shop_reseller_id(None)
        self.bot = AsyncMock()

        async def load_user(*args):
            return await self.session.scalar(select(BotUser).where(BotUser.id == 1))

        self.patches = [
            patch("app.api.miniapp_pages.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages.assert_mini_force_join", AsyncMock()),
            patch("app.api.miniapp_pages.get_all_settings", AsyncMock(return_value={"pay_wallet_enabled": "1"})),
            patch("app.services.service_addons._apply_pack_to_service", AsyncMock()),
            patch("app.services.notifications._dispatch_dual_notify", AsyncMock()),
            patch("app.services.reseller_bots.open_notify_bot_for_user", AsyncMock(return_value=(self.bot, True))),
            patch("app.api.miniapp_feeds.load_mini_user", AsyncMock(side_effect=load_user)),
            patch("app.api.miniapp_pages._fetch_pg_info", AsyncMock(return_value={"error": "upstream_unavailable"})),
        ]
        self.mocks = [p.start() for p in self.patches]
        app = FastAPI()

        async def get_db():
            yield self.session

        register_miniapp_pages(app, render=lambda *args: "", get_db=get_db)
        register_miniapp_feeds(app, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches):
            p.stop()
        reset_shop_reseller_id(self.shop_token)
        await self.session.close()
        await self.engine.dispose()

    async def buy(self, pack_id=1, service_id=10, kind=None):
        body = {"service_id": service_id, "pack_id": pack_id}
        if kind is not None:
            body["kind"] = kind
        return await self.client.post("/api/mini/addon", json=body)

    async def assert_no_purchase(self):
        self.assertEqual(await self.session.scalar(select(func.count(Order.id))), 0)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 10000)
        self.mocks[3].assert_not_awaited()

    async def test_catalog_contains_only_active_platform_packs(self):
        response = await self.client.get("/api/mini/service/10/addons")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["cache-control"])
        packs = response.json()["packs"]
        self.assertEqual({p["id"] for p in packs}, {1, 2})
        self.assertEqual({p["amount_label"] for p in packs}, {"2.5 گیگ", "7 روز"})

    async def test_catalog_filters_the_selected_kind_on_the_server(self):
        for kind, pack_id, amount in [("volume", 1, "2.5 گیگ"), ("duration", 2, "7 روز")]:
            with self.subTest(kind=kind):
                response = await self.client.get(f"/api/mini/service/10/addons?kind={kind}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["kind"], kind)
                packs = response.json()["packs"]
                self.assertEqual([p["id"] for p in packs], [pack_id])
                self.assertEqual(packs[0]["kind"], kind)
                self.assertEqual(packs[0]["amount_label"], amount)

    async def test_catalog_rejects_an_unknown_kind(self):
        response = await self.client.get("/api/mini/service/10/addons?kind=unknown")
        self.assertEqual(response.status_code, 422)

    async def test_checkout_rejects_the_opposite_kind_before_debit(self):
        for kind, pack_id in [("volume", 2), ("duration", 1), ("unknown", 1)]:
            with self.subTest(kind=kind):
                response = await self.client.post("/api/mini/addon", json={
                    "service_id": 10, "pack_id": pack_id, "kind": kind,
                })
                self.assertEqual(response.status_code, 400)
                await self.assert_no_purchase()

    async def test_service_payload_marks_only_eligible_owned_services(self):
        linked = UserService(id=11, bot_user_id=1, pg_user_id=111, pg_username="linked", remark="linked")
        unconnected = UserService(id=12, bot_user_id=1, pg_username="unconnected")
        self.session.add_all([linked, unconnected])
        await self.session.commit()
        response = await self.client.get("/api/mini/services")
        self.assertEqual(response.status_code, 200)
        allowed = {s["id"]: s["addons_allowed"] for s in response.json()["items"]}
        self.assertEqual(allowed, {10: True, 11: False, 12: False})

    async def test_volume_and_duration_use_real_wallet_checkout_and_notify(self):
        for pack, balance in [(self.volume, 9000), (self.duration, 7000)]:
            with self.subTest(kind=pack.kind):
                response = await self.buy(pack.id, kind=pack.kind)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["wallet"], balance)
                order = await self.session.get(Order, response.json()["order_id"])
                self.assertEqual(order.status, "delivered")
                self.assertEqual(order.service_id, 10)
                self.assertIsNone(order.plan_id)
                self.assertIn(f":{pack.kind}:", order.note)
                payment = await self.session.scalar(select(Payment).where(Payment.order_id == order.id))
                self.assertEqual(payment.status, "approved")
                self.assertEqual(payment.amount, pack.price)
                call = self.mocks[3].await_args
                self.assertIs(call.args[1], self.svc)
                self.assertEqual(call.kwargs["kind"], pack.kind)
                self.assertEqual(call.kwargs["amount"], pack.amount)
                text = self.mocks[4].await_args.args[3]
                self.assertIn("my-service", text)
                self.assertIn(pack.name, text)
                self.assertIn("افزایش حجم" if pack.kind == "volume" else "افزایش زمان", text)
        self.assertEqual(self.bot.session.close.await_count, 2)

    async def test_foreign_service_is_rejected_before_checkout(self):
        self.assertEqual((await self.buy(service_id=20)).status_code, 404)
        self.assertEqual((await self.client.get("/api/mini/service/20/addons")).status_code, 404)
        await self.assert_no_purchase()

    async def test_inactive_foreign_and_missing_packs_are_rejected(self):
        for pack_id in (3, 4, 999):
            self.assertEqual((await self.buy(pack_id)).status_code, 400)
        await self.assert_no_purchase()

    async def test_insufficient_wallet_does_not_create_order(self):
        self.volume.price = 10001
        await self.session.commit()
        response = await self.buy()
        self.assertEqual(response.status_code, 400)
        self.assertIn("موجودی", response.text)
        await self.assert_no_purchase()

    async def test_disabled_wallet_and_admin_cannot_buy(self):
        self.mocks[2].return_value = {"pay_wallet_enabled": "0"}
        self.assertEqual((await self.buy()).status_code, 403)
        self.user.role = "admin"
        self.assertEqual((await self.buy()).status_code, 403)
        self.assertEqual((await self.client.get("/api/mini/service/10/addons")).status_code, 403)
        await self.assert_no_purchase()

    async def test_linked_and_shop_owned_services_cannot_buy(self):
        self.svc.remark = "linked"
        self.assertEqual((await self.buy()).status_code, 400)
        self.assertEqual((await self.client.get("/api/mini/service/10/addons")).status_code, 400)
        self.svc.remark = None
        self.user.reseller_id = 2
        self.svc.shop_id = 2
        # A shop-sold service is not the Mini App's to show at all, so it is not found.
        self.assertEqual((await self.buy()).status_code, 404)
        await self.assert_no_purchase()

    async def test_known_unlimited_quota_is_rejected_without_debit(self):
        self.svc.quota_synced_at = datetime.now(timezone.utc)
        self.svc.quota_data_limit_bytes = 0
        self.svc.quota_expire_at = None
        await self.session.commit()
        for pack_id in (1, 2):
            response = await self.buy(pack_id)
            self.assertEqual(response.status_code, 400)
            self.assertIn("نامحدود", response.text)
        await self.assert_no_purchase()

    async def test_malformed_body_is_rejected(self):
        for body in (None, [], {}, {"pack_id": "bad", "service_id": 10}):
            response = await self.client.post("/api/mini/addon", json=body)
            self.assertEqual(response.status_code, 400)
        response = await self.client.post("/api/mini/addon", content="{broken", headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        await self.assert_no_purchase()

    async def test_delivery_failure_refunds_wallet(self):
        self.mocks[3].side_effect = ValueError("اعمال بسته ناموفق")
        response = await self.buy()
        self.assertEqual(response.status_code, 400)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 10000)
        self.mocks[4].assert_not_awaited()

    async def test_unexpected_delivery_error_is_safe_and_refunded(self):
        self.mocks[3].side_effect = RuntimeError("internal upstream detail")
        response = await self.buy()
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("internal", response.text)
        await self.session.refresh(self.user)
        self.assertEqual(self.user.wallet_balance, 10000)

    async def test_notification_failure_does_not_fail_completed_purchase(self):
        async def failed_notify_bot(*args):
            await self.session.rollback()
            raise RuntimeError("notification unavailable")

        self.mocks[5].side_effect = failed_notify_bot
        response = await self.buy()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["wallet"], 9000)

    async def test_pending_receipt_reports_snapshot_and_service(self):
        for kind, amount, expected in [("volume", "2.5", "2.5 گیگ"), ("duration", "7", "7 روز")]:
            with self.subTest(kind=kind):
                order = Order(id=77, user_id=1, service_id=10, amount=1000, payment_method="card", note=f"svc_addon:1:10:{kind}:{amount}")
                payment = Payment(id=88, user_id=1, order_id=77, amount=1000, method="card", is_wallet_topup=False)
                self.session.add_all([order, payment])
                await self.session.commit()
                # Pack edits must not change the purchased entitlement in the message.
                self.volume.amount = 999
                self.volume.name = "بسته <حجم>"
                await self.session.commit()
                await notify_pending_approval(self.bot, self.session, payment, 101)
                text = self.mocks[4].await_args.args[3]
                self.assertIn("افزایش حجم" if kind == "volume" else "افزایش زمان", text)
                self.assertIn(expected, text)
                self.assertIn("my-service", text)
                self.assertIn("بسته &lt;حجم&gt;", text)
                self.assertNotIn("خرید اشتراک", text)
                self.assertNotIn("999 گیگ", text)
                await self.session.delete(payment)
                await self.session.delete(order)
                await self.session.commit()

    async def test_legacy_and_deleted_pack_notifications(self):
        order = Order(user_id=1, service_id=10, amount=1000, payment_method="card", note="svc_addon:1:10")
        kind, lines = await _pending_order_detail_lines(self.session, order, None)
        self.assertEqual(kind, "افزایش حجم")
        self.assertIn("2.5 گیگ", "\n".join(lines))
        order.note = "svc_addon:1:10:duration:7"
        await self.session.delete(self.volume)
        await self.session.commit()
        kind, lines = await _pending_order_detail_lines(self.session, order, None)
        self.assertEqual(kind, "افزایش زمان")
        self.assertIn("7 روز", "\n".join(lines))
        self.assertIn("my-service", "\n".join(lines))

    async def test_all_order_notifications_include_addon_purpose(self):
        order = Order(id=77, user_id=1, service_id=10, amount=1000, payment_method="wallet", note="svc_addon:1:10:volume:2.5")
        for notify in (notify_new_order, notify_new_subscription):
            await notify(self.bot, self.session, order=order, user_tg_id=101)
            text = self.mocks[4].await_args.args[3]
            self.assertIn("افزایش حجم", text)
            self.assertIn("2.5 گیگ", text)
            self.assertIn("my-service", text)
            self.assertIn("کیف پول", text)

    async def test_notification_does_not_disclose_foreign_service_or_pack(self):
        order = Order(id=77, user_id=1, service_id=20, amount=1, payment_method="card", note="svc_addon:3:20:volume:10")
        kind, lines = await _pending_order_detail_lines(self.session, order, None)
        text = "\n".join(lines)
        self.assertEqual(kind, "افزایش حجم")
        self.assertNotIn("private-service", text)
        self.assertNotIn("private-pack", text)
