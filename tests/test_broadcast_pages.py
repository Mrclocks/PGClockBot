"""Group-message pages: the owner's and a shop's page from one template, the live audience count, and sending
with a segment. A shop only ever counts, sends to and sees its own customers and plans."""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.api.broadcast_queue_pages import register_platform_broadcast_pages
from app.api.shop_broadcast_pages import register_shop_broadcast_pages
from app.db.models import BroadcastLog
from tests.test_broadcast_audience import OTHER_SHOP, OTHER_SHOP_PLAN, SHOP, SHOP_PLAN, AudienceCase

OWNER = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "org_status": "active"}
SHOP_STAFF = {"role": "reseller", "permissions": ["broadcast"], "bot_user_id": SHOP}
NO_PERMISSION = {"role": "reseller", "permissions": ["catalog_discounts"], "bot_user_id": SHOP}
CAMPAIGNS_ONLY = {"role": "reseller", "permissions": ["campaigns"], "bot_user_id": SHOP}


def _render(request, name, ctx):
    from app.api.app import render

    return render(request, name, ctx)


class PageCase(AudienceCase):
    def app(self, staff: dict, *, platform: bool = False) -> FastAPI:
        app = FastAPI()

        async def db():
            yield self.session

        async def guard():
            return staff

        if platform:
            register_platform_broadcast_pages(app, render=_render, require_admin=guard, get_db=db)
        else:
            register_shop_broadcast_pages(app, render=_render, require_staff=guard, get_db=db)
        return app

    async def call(self, staff: dict, method: str, url: str, *, platform: bool = False, **kw) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app(staff, platform=platform))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, follow_redirects=False, **kw)


class AudienceCountTests(PageCase):
    async def test_a_shop_counts_only_its_own_customers(self) -> None:
        response = await self.call(SHOP_STAFF, "GET", "/shop-broadcast/audience?segment=all")
        self.assertEqual(response.json()["count"], 2)
        response = await self.call(SHOP_STAFF, "GET", "/shop-broadcast/audience?segment=active")
        self.assertEqual((response.json()["count"], response.json()["label"]), (1, "سرویس فعال"))

    async def test_another_shops_plan_or_a_platform_segment_is_a_safe_400(self) -> None:
        for query in (f"segment=active&plan_id={OTHER_SHOP_PLAN}", "segment=admins", "segment=all&notice=1", "segment=expiring&days=-1"):
            with self.subTest(query=query):
                response = await self.call(SHOP_STAFF, "GET", f"/shop-broadcast/audience?{query}")
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("Traceback", response.text)
                self.assertNotIn("<b>", response.text)

    async def test_the_owner_counts_platform_segments(self) -> None:
        response = await self.call(OWNER, "GET", "/broadcast/audience?segment=expiring&days=3", platform=True)
        self.assertEqual(response.json()["count"], 1)

    async def test_staff_without_the_permission_cannot_count(self) -> None:
        response = await self.call(NO_PERMISSION, "GET", "/shop-broadcast/audience?segment=all")
        self.assertEqual(response.status_code, 403)

    async def test_the_campaigns_permission_alone_does_not_open_group_messages(self) -> None:
        for method, path in (("GET", "/shop-broadcast"), ("GET", "/shop-broadcast/audience?segment=all"),
                             ("POST", "/shop-broadcast/history/clear")):
            with self.subTest(path=path):
                response = await self.call(CAMPAIGNS_ONLY, method, path)
                self.assertEqual(response.status_code, 403)
        html = (await self.call(SHOP_STAFF, "GET", "/shop-broadcast")).text
        self.assertNotIn('href="/campaigns"', html)


class SendTests(PageCase):
    async def test_a_shop_message_is_queued_once_for_its_segment(self) -> None:
        form = {"segment": "never_bought", "text": "سلام", "request_token": "a" * 32,
                "btn_target": "shop", "btn_label": "", "btn_url": "", "btn_style": "", "btn_plan": ""}
        first = await self.call(SHOP_STAFF, "POST", "/shop-broadcast", data=form)
        again = await self.call(SHOP_STAFF, "POST", "/shop-broadcast", data=form)
        self.assertIn("ok=", first.headers["location"])
        self.assertIn("ok=", again.headers["location"])
        rows = list(await self.session.scalars(select(BroadcastLog)))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].shop_id, rows[0].audience, rows[0].total), (SHOP, "never_bought", 1))
        self.assertIn('"t": "shop"', rows[0].buttons)

    async def test_a_plan_button_of_another_shop_is_refused_and_nothing_is_queued(self) -> None:
        form = {"segment": "all", "text": "x", "request_token": "b" * 32, "btn_target": "plan",
                "btn_label": "", "btn_url": "", "btn_style": "", "btn_plan": str(OTHER_SHOP_PLAN)}
        response = await self.call(SHOP_STAFF, "POST", "/shop-broadcast", data=form)
        self.assertIn("err=", response.headers["location"])
        self.assertEqual(list(await self.session.scalars(select(BroadcastLog))), [])

    async def test_a_shop_may_send_a_button_for_its_own_plan(self) -> None:
        form = {"segment": "active", "text": "x", "request_token": "c" * 32, "btn_target": "plan",
                "btn_label": "", "btn_url": "", "btn_style": "", "btn_plan": str(SHOP_PLAN)}
        response = await self.call(SHOP_STAFF, "POST", "/shop-broadcast", data=form)
        self.assertIn("ok=", response.headers["location"])
        row = await self.session.scalar(select(BroadcastLog))
        self.assertIn(f'"t": "{SHOP_PLAN}"', row.buttons)


class RenderTests(PageCase):
    async def page(self, staff: dict, *, platform: bool) -> str:
        url = "/broadcast" if platform else "/shop-broadcast"
        response = await self.call(staff, "GET", url, platform=platform)
        self.assertEqual(response.status_code, 200)
        return response.text

    async def test_the_shop_page_offers_only_shop_segments_and_plans(self) -> None:
        html = await self.page(SHOP_STAFF, platform=False)
        self.assertIn('action="/shop-broadcast"', html)
        self.assertIn('data-audience-url="/shop-broadcast/audience"', html)
        self.assertIn('data-value="expiring"', html)
        for owner_only in ('data-value="admins"', 'data-value="resellers"', 'name="notice"', "/broadcast/history/bulk-action"):
            self.assertNotIn(owner_only, html)
        self.assertIn("پلن فروشگاه", html)
        self.assertNotIn("دیگری", html)

    async def test_the_owner_page_has_staff_segments_the_notice_switch_and_bulk_delete(self) -> None:
        html = await self.page(OWNER, platform=True)
        for part in ('data-value="admins"', 'name="notice"', "/broadcast/history/bulk-action", 'id="bc-preview"'):
            self.assertIn(part, html)

    async def test_history_shows_the_saved_audience_summary_escaped(self) -> None:
        self.session.add(BroadcastLog(shop_id=SHOP, audience="active", text="<script>x</script>", total=1, status="done",
                                      audience_spec='{"segment": "active", "as_of": "2026-01-01T00:00:00+00:00", "label": "<i>سرویس</i>"}'))
        self.session.add(BroadcastLog(shop_id=OTHER_SHOP, audience="all", text="دیگری-پیام", total=1, status="done"))
        await self.session.commit()
        html = await self.page(SHOP_STAFF, platform=False)
        self.assertIn("&lt;i&gt;سرویس&lt;/i&gt;", html)
        self.assertNotIn("<script>x</script>", html)
        self.assertNotIn("دیگری-پیام", html)


if __name__ == "__main__":
    unittest.main()
