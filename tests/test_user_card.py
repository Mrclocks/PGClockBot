"""The users-list user card shows one shop's view of a customer and nothing from another shop."""

from __future__ import annotations

import unittest
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.user_pages import register_user_pages
from app.db.models import Base, BotUser, BotUserShop, Order, Plan, ShopWallet, UserService
from app.services.formatting import format_money, format_number
from app.services.user_card import build_user_card
from app.services.users_ops import expire_tone
from tests.postgres_support import scoped_async_engine

ROOT = Path(__file__).resolve().parents[1]
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP_A, SHOP_B = 10, 20
HOSTILE_NAME = '<script>alert("x")</script>'


def shop_staff(shop_id: int) -> dict[str, Any]:
    return {"role": "reseller", "bot_user_id": shop_id, "permissions": ["dashboard"]}


class UserCardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        now = datetime.now(timezone.utc)
        self.session.add_all([
            BotUser(id=SHOP_A, telegram_id=5010, referral_code="shopa", role="reseller"),
            BotUser(id=SHOP_B, telegram_id=5020, referral_code="shopb", role="reseller"),
            Plan(id=1, name="Monthly", price=100, duration_days=30, data_limit_gb=50),
        ])
        await self.session.flush()
        self.session.add_all([
            # A customer of shop A who also bought from the platform.
            BotUser(id=11, telegram_id=9011, referral_code="c11", full_name=HOSTILE_NAME,
                    reseller_id=SHOP_A, wallet_balance=50, created_at=now),
            BotUser(id=1, telegram_id=9001, referral_code="c1", full_name="Platform customer",
                    wallet_balance=70, color_tag="red", created_at=now),
        ])
        await self.session.flush()
        self.session.add_all([
            ShopWallet(user_id=11, reseller_id=SHOP_A, balance=80),
            # Shop A's own risk tag lives on its membership row.
            BotUserShop(bot_user_id=11, shop_id=SHOP_A, color_tag="red"),
            UserService(id=1, bot_user_id=11, plan_id=1, pg_username="shop_a_service", shop_id=SHOP_A,
                        quota_synced_at=now, quota_expire_at=now + timedelta(days=2),
                        quota_data_limit_bytes=50 * 1024**3),
            UserService(id=2, bot_user_id=11, plan_id=1, pg_username="platform_service", shop_id=None),
            UserService(id=3, bot_user_id=1, plan_id=1, pg_username="owner_service", shop_id=None),
            Order(user_id=11, reseller_id=SHOP_A, plan_id=1, amount=100, status="delivered"),
            Order(user_id=11, reseller_id=SHOP_A, plan_id=1, amount=40, status="awaiting_approval"),
            Order(user_id=11, reseller_id=None, plan_id=1, amount=900, status="delivered"),
        ])
        await self.session.commit()

        self.staff: dict[str, Any] = shop_staff(SHOP_A)
        self.app = FastAPI()

        async def get_db() -> AsyncIterator[AsyncSession]:
            yield self.session

        async def require_staff() -> dict[str, Any]:
            return self.staff

        env = Environment(loader=FileSystemLoader(ROOT / "app/web/templates"), autoescape=select_autoescape())
        env.filters["num"] = format_number
        env.filters["money"] = format_money

        def render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
            return HTMLResponse(env.get_template(name).render(**context, csrf_token="fake-csrf"))

        register_user_pages(self.app, render=render, require_admin=require_staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.session.close()
        await self.engine.dispose()

    async def test_shop_sees_only_its_own_purse_orders_and_services(self) -> None:
        response = await self.client.get("/users/11/card")
        self.assertEqual(response.status_code, 200)
        self.assertIn("shop_a_service", response.text)
        self.assertNotIn("platform_service", response.text)
        self.assertIn(">80<", response.text, "the shop purse, not the platform balance")
        self.assertIn(">100<", response.text, "only this shop's paid orders count")
        self.assertNotIn("900", response.text)
        self.assertNotIn(">140<", response.text, "an order still awaiting approval is not paid")

    async def test_another_shop_cannot_open_the_card(self) -> None:
        self.staff = shop_staff(SHOP_B)
        response = await self.client.get("/users/11/card")
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("shop_a_service", response.text)
        self.assertNotIn("9011", response.text)

    async def test_an_unknown_user_is_not_found(self) -> None:
        response = await self.client.get("/users/999/card")
        self.assertEqual(response.status_code, 404)

    async def test_customer_text_is_escaped(self) -> None:
        response = await self.client.get("/users/11/card")
        self.assertNotIn(HOSTILE_NAME, response.text)
        self.assertIn("&lt;script&gt;", response.text)

    async def test_a_latin_name_keeps_the_header_right_to_left(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.get("/users/1/card")
        self.assertIn('<h3 class="user-identity-name"><bdi>Platform customer</bdi></h3>', response.text)
        self.assertNotIn('dir="auto"', response.text)

    async def test_owner_sees_the_platform_purse_and_services(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.get("/users/1/card")
        self.assertEqual(response.status_code, 200)
        self.assertIn("owner_service", response.text)
        self.assertIn(">70<", response.text)

    async def test_the_risk_tag_needs_the_right_to_read_private_marks(self) -> None:
        user = await self.session.get(BotUser, 11)
        shown = await build_user_card(self.session, user, shop_id=SHOP_A, show_private_marks=True)
        hidden = await build_user_card(self.session, user, shop_id=SHOP_A, show_private_marks=False)
        self.assertEqual(shown.risk_tag.key, "red")
        self.assertIsNone(hidden.risk_tag)

    async def test_a_soon_expiring_service_is_flagged(self) -> None:
        user = await self.session.get(BotUser, 11)
        card = await build_user_card(self.session, user, shop_id=SHOP_A, show_private_marks=True)
        self.assertEqual([s.pg_username for s in card.services], ["shop_a_service"])
        self.assertEqual(card.services[0].plan_name, "Monthly")
        self.assertEqual(card.services[0].expire_tone, "warn")


    async def test_the_card_is_section_boxes_with_the_status_once_in_the_header(self) -> None:
        response = await self.client.get("/users/11/card")
        self.assertIn('<section class="edit-section edit-section--identity">', response.text)
        self.assertEqual(response.text.count('<section class="edit-section'), 3)
        self.assertEqual(response.text.count(">فعال<"), 1)
        self.assertNotIn("<dt>وضعیت</dt>", response.text)

    async def test_owner_opening_a_reseller_from_the_resellers_list_edits_the_reseller(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.get(f"/users/{SHOP_A}/card?from=resellers")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f'data-edit-url="/resellers/{SHOP_A}/edit?fragment=1"', response.text)
        self.assertNotIn('data-modal-open="modal-user-edit"', response.text)

    async def test_the_same_reseller_from_the_users_list_edits_the_customer(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.get(f"/users/{SHOP_A}/card")
        self.assertIn('data-modal-open="modal-user-edit"', response.text)
        self.assertNotIn('data-modal-open="modal-reseller-edit"', response.text)

    async def test_a_shop_cannot_reach_the_reseller_edit_through_the_resellers_hint(self) -> None:
        response = await self.client.get("/users/11/card?from=resellers")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("modal-reseller-edit", response.text)
        self.assertNotIn("modal-user-edit", response.text, "the resellers page has no user edit modal")

    async def test_shop_staff_without_panel_service_rights_still_get_edit(self) -> None:
        response = await self.client.get("/users/11/card")
        self.assertIn('data-modal-load="/users/11/edit?fragment=1"', response.text)

    async def test_username_and_telegram_id_copy_on_tap(self) -> None:
        user = await self.session.get(BotUser, 11)
        user.username = "shop_customer"
        await self.session.commit()
        response = await self.client.get("/users/11/card")
        self.assertIn('data-copy="@shop_customer"', response.text)
        self.assertIn('data-copy="9011"', response.text)

    async def test_wallet_points_purchases_and_membership_sit_in_one_box(self) -> None:
        response = await self.client.get("/users/11/card")
        stats = response.text.split('class="edit-section user-card-stats"')[1].split("</section>")[0]
        for label in ("کیف پول", "امتیاز", "خرید موفق", "عضویت"):
            self.assertIn(f"<dt>{label}</dt>", stats)

    async def test_a_blocked_customer_gets_no_message_button(self) -> None:
        user = await self.session.get(BotUser, 11)
        user.is_blocked = True
        await self.session.commit()
        response = await self.client.get("/users/11/card")
        self.assertNotIn("modal-user-message", response.text)
        self.assertIn(">مسدود<", response.text)

    async def test_a_message_sent_from_the_resellers_list_returns_there(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.post(f"/users/{SHOP_A}/message", data={"body": "", "return_page": "resellers"})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/resellers?err="))

    async def test_a_card_opened_from_another_table_links_to_the_users_edit(self) -> None:
        for page in ("finance", "tickets", "trials", "cleanup", "applications", "loyalty"):
            response = await self.client.get(f"/users/11/card?from={page}")
            self.assertRegex(response.text, r'<a class="btn" href="/users\?uid=11&amp;edit=11">(?:\{\{[^}]*\}\}|<svg[^>]*>.*?</svg>)?ویرایش</a>', page)
            self.assertNotIn('data-modal-open="modal-user-edit"', response.text, page)

    async def test_an_unknown_card_page_behaves_like_the_users_list(self) -> None:
        response = await self.client.get("/users/11/card?from=https://evil.example")
        self.assertIn('data-modal-open="modal-user-edit"', response.text)
        self.assertNotIn("evil.example", response.text)

    async def test_a_message_sent_from_another_table_returns_to_that_page(self) -> None:
        for page, path in (("finance", "/finance"), ("tickets", "/tickets"), ("trials", "/trial-users"),
                           ("cleanup", "/service-cleanup"), ("loyalty", "/loyalty")):
            response = await self.client.post("/users/11/message", data={"body": "", "return_page": page})
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith(f"{path}?err="), page)

    async def test_staff_without_the_users_right_cannot_open_the_card(self) -> None:
        self.staff = {"role": "reseller", "bot_user_id": SHOP_A, "permissions": ["finance"]}
        app = FastAPI()
        # The real permission dependency, so the right is checked as in production.
        from app.services.authz import authz_from_staff, can_shop

        def require_perm(perm: str):
            async def _dep() -> dict[str, Any]:
                if not can_shop(authz_from_staff(self.staff), perm):
                    from fastapi import HTTPException

                    raise HTTPException(status_code=403)
                return self.staff
            return _dep

        async def get_db() -> AsyncIterator[AsyncSession]:
            yield self.session

        register_user_pages(app, render=lambda *a, **k: HTMLResponse(""), require_admin=require_perm("x"),
                            get_db=get_db, require_perm=require_perm)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/users/11/card?from=finance")
        self.assertEqual(response.status_code, 403)

    async def test_an_unknown_return_page_falls_back_to_the_users_list(self) -> None:
        self.staff = OWNER.copy()
        response = await self.client.post(
            f"/users/{SHOP_A}/message", data={"body": "", "return_page": "https://evil.example"}
        )
        self.assertTrue(response.headers["location"].startswith("/users"))


class ExpireToneTests(unittest.TestCase):
    def test_expired_is_danger_inside_the_window_warn_otherwise_plain(self) -> None:
        self.assertEqual(expire_tone(0, expiring=False), "danger")
        self.assertEqual(expire_tone(-3, expiring=False), "danger")
        self.assertEqual(expire_tone(2, expiring=True), "warn")
        self.assertEqual(expire_tone(30, expiring=False), "")
        self.assertEqual(expire_tone(None, expiring=False), "", "unlimited time")


if __name__ == "__main__":
    unittest.main()
