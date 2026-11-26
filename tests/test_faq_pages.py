"""«سوالات متداول» web page: who may open it, which shop it edits, refused input, row actions and escaping."""

from __future__ import annotations

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.api.faq_pages import draft_from_form, faq_scope, register_faq_pages
from app.db.models import FaqItem, ResellerSetting
from app.services import faq
from app.services.authz import authz_from_staff, can_shop
from app.services.faq import FaqDraft
from app.services.settings_tab_groups import FAQ_PAGE, group_settings_tabs, tabs_with_page_tabs
from app.services.users import SETTINGS_TABS
from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.users import clear_settings_cache
from tests.postgres_support import PostgresTestCase, add_shop_users

SHOP = 41
OTHER = 42
OWNER = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None, "org_status": "active"}
SHOP_STAFF = {"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": SHOP}
NO_PERMISSION = {"role": "reseller", "permissions": ["broadcast"], "bot_user_id": SHOP}
BARE_ADMIN = {"role": "admin", "permissions": []}
FORM = {"question": "لینک وصل نمی‌شود؟", "answer": "QR را دوباره بگیرید", "button_style": "inherit", "is_active": "1"}


def _render(request, name, ctx, status_code=200):
    from app.api.app import render

    return render(request, name, ctx, status_code=status_code)


class FaqPageCase(PostgresTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await add_shop_users(self.Session, SHOP, OTHER)
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    def app(self, staff: dict) -> FastAPI:
        app = FastAPI()

        async def db():
            async with self.Session() as session:
                yield session

        def require_perm(perm: str):
            async def guard():
                if not can_shop(authz_from_staff(staff), perm):
                    raise HTTPException(status_code=403)
                return staff

            return guard

        register_faq_pages(app, render=_render, require_perm=require_perm, get_db=db)
        return app

    async def call(self, staff: dict, method: str, url: str, **kw) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app(staff))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, url, follow_redirects=False, **kw)

    async def items(self, shop: int | None) -> list[FaqItem]:
        async with self.Session() as session:
            return await faq.list_faq_items(session, reseller_id=shop)

    async def seed(self, shop: int | None, question: str = "سوال") -> FaqItem:
        async with self.Session() as session:
            return await faq.create_faq_item(session, FaqDraft(question=question, answer="جواب"), reseller_id=shop)


class ScopeTests(FaqPageCase):
    def test_scope_comes_from_the_session_only(self) -> None:
        self.assertEqual(faq_scope(OWNER).reseller_id, None)
        self.assertEqual(faq_scope(SHOP_STAFF).reseller_id, SHOP)
        self.assertIsNone(faq_scope(BARE_ADMIN))

    def test_form_values_become_a_draft(self) -> None:
        draft = draft_from_form({**FORM, "button_style": "danger", "icon_custom_emoji_id": "", "is_active": ""})
        self.assertEqual((draft.button_style, draft.icon_custom_emoji_id, draft.is_active), ("danger", None, False))
        self.assertIsNone(draft_from_form(FORM).button_style)

    def test_the_faq_row_is_in_the_customer_area_for_staff_who_may_open_it(self) -> None:
        for staff, tabs in ((OWNER, SETTINGS_TABS), (SHOP_STAFF, RESELLER_SETTINGS_TABS)):
            groups = group_settings_tabs(tabs_with_page_tabs(tabs, staff), "faq", area="customer")
            hrefs = [tab.href for group in groups for tab in group.tabs]
            self.assertIn(FAQ_PAGE, hrefs, staff)
        groups = group_settings_tabs(tabs_with_page_tabs(RESELLER_SETTINGS_TABS, NO_PERMISSION), "faq", area="customer")
        self.assertNotIn(FAQ_PAGE, [tab.href for group in groups for tab in group.tabs])


class PageTests(FaqPageCase):
    async def test_access_needs_the_permission_and_a_shop(self) -> None:
        self.assertEqual((await self.call(NO_PERMISSION, "GET", FAQ_PAGE)).status_code, 403)
        self.assertEqual((await self.call(NO_PERMISSION, "POST", FAQ_PAGE, data=FORM)).status_code, 403)
        denied = await self.call(BARE_ADMIN, "POST", FAQ_PAGE, data=FORM)
        self.assertEqual(denied.status_code, 303)
        self.assertTrue(denied.headers["location"].startswith("/dashboard"))
        self.assertEqual(await self.items(None), [])

    async def test_shop_staff_add_to_their_own_shop_only(self) -> None:
        response = await self.call(SHOP_STAFF, "POST", FAQ_PAGE, data=FORM)
        self.assertEqual((response.status_code, response.headers["location"]), (303, "/faq?ok=created"))
        self.assertEqual([i.question for i in await self.items(SHOP)], [FORM["question"]])
        self.assertEqual(await self.items(None), [])
        page = await self.call(SHOP_STAFF, "GET", "/faq?ok=created")
        self.assertEqual(page.status_code, 200)
        self.assertIn("سوال اضافه شد.", page.text)
        self.assertIn(FORM["question"], page.text)

    async def test_owner_edits_the_platform_list(self) -> None:
        await self.call(OWNER, "POST", FAQ_PAGE, data=FORM)
        self.assertEqual(len(await self.items(None)), 1)
        self.assertEqual(await self.items(SHOP), [])

    async def test_another_shops_question_cannot_be_changed_by_id(self) -> None:
        other = await self.seed(OTHER, "مال فروشگاه دیگر")
        platform = await self.seed(None, "مال پلتفرم")
        for item in (other, platform):
            for path in (f"/faq/{item.id}", f"/faq/{item.id}/delete", f"/faq/{item.id}/hide", f"/faq/{item.id}/up"):
                response = await self.call(SHOP_STAFF, "POST", path, data=FORM)
                self.assertEqual(response.status_code, 400, path)
                self.assertIn(faq.NOT_FOUND, response.text)
        self.assertEqual([i.question for i in await self.items(OTHER)], ["مال فروشگاه دیگر"])
        self.assertTrue((await self.items(None))[0].is_active)

    async def test_refused_input_reopens_the_form_with_what_was_typed(self) -> None:
        response = await self.call(SHOP_STAFF, "POST", FAQ_PAGE, data={**FORM, "answer": "<b>بسته‌نشده"})
        self.assertEqual(response.status_code, 400)
        self.assertIn('id="modal-faq-new"', response.text)
        self.assertIn("لینک وصل نمی‌شود؟", response.text)
        self.assertEqual(await self.items(SHOP), [])
        bad_color = await self.call(SHOP_STAFF, "POST", FAQ_PAGE, data={**FORM, "button_style": "evil"})
        self.assertIn(faq.STYLE_INVALID, bad_color.text)

    async def test_row_actions_toggle_move_and_delete(self) -> None:
        first, second = await self.seed(SHOP, "اول"), await self.seed(SHOP, "دوم")
        self.assertEqual((await self.call(SHOP_STAFF, "POST", f"/faq/{first.id}/hide")).headers["location"], "/faq?ok=hidden")
        await self.call(SHOP_STAFF, "POST", f"/faq/{second.id}/up")
        items = await self.items(SHOP)
        self.assertEqual([i.id for i in items], [second.id, first.id])
        self.assertFalse(next(i for i in items if i.id == first.id).is_active)
        self.assertEqual((await self.call(SHOP_STAFF, "POST", f"/faq/{first.id}/explode")).status_code, 400)
        await self.call(SHOP_STAFF, "POST", f"/faq/{first.id}/delete")
        self.assertEqual([i.id for i in await self.items(SHOP)], [second.id])

    async def test_update_keeps_the_values_sent(self) -> None:
        item = await self.seed(SHOP)
        data = {**FORM, "button_style": "", "icon_custom_emoji_id": "5368324170671202286", "is_active": ""}
        await self.call(SHOP_STAFF, "POST", f"/faq/{item.id}", data=data)
        async with self.Session() as session:
            stored = await session.get(FaqItem, item.id)
        self.assertEqual((stored.button_style, stored.icon_custom_emoji_id, stored.is_active), ("", "5368324170671202286", False))

    async def test_hostile_question_is_escaped_on_the_page(self) -> None:
        await self.seed(SHOP, '<script>alert(1)</script>"')
        page = await self.call(SHOP_STAFF, "GET", FAQ_PAGE)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)

    async def test_empty_list_offers_the_legacy_import_once(self) -> None:
        async with self.Session() as session:
            session.add(ResellerSetting(reseller_user_id=SHOP, key="faq_text", value="❓ الف؟\nب\n\n❓ ج؟\nد"))
            await session.commit()
        clear_settings_cache()
        page = await self.call(SHOP_STAFF, "GET", FAQ_PAGE)
        self.assertIn('action="/faq/import"', page.text)
        self.assertEqual((await self.call(SHOP_STAFF, "POST", "/faq/import")).headers["location"], "/faq?ok=imported")
        self.assertEqual([i.question for i in await self.items(SHOP)], ["الف؟", "ج؟"])
        again = await self.call(SHOP_STAFF, "POST", "/faq/import")
        self.assertEqual(again.status_code, 400)
        self.assertNotIn('action="/faq/import"', (await self.call(SHOP_STAFF, "GET", FAQ_PAGE)).text)
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(ResellerSetting.value).where(ResellerSetting.key == "faq_text")), "❓ الف؟\nب\n\n❓ ج؟\nد")
