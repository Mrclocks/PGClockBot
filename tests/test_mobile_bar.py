"""Phone bottom bar settings: the owner's page order, who may change it, and how each viewer's bar follows it."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.mobile_bar_settings import register_mobile_bar_settings
from app.db import Base
from app.services import mobile_bar
from app.services.mobile_bar import (
    DUPLICATE_PAGE,
    SETTING_KEY,
    UNKNOWN_PAGE,
    clean_bar_slots,
    load_bar_form,
    load_bar_order,
    parse_bar_order,
    save_bar_order,
)
from app.services.panel_nav import DOCK_SLOTS, NavViewer, build_dock, build_sidebar, dock_choices
from app.services.platform_identity import is_explicit_owner_staff
from app.services.users import get_setting

ALL_PG = frozenset({"pg_overview", "pg_users", "pg_admins", "pg_nodes", "pg_groups", "pg_hosts", "pg_templates", "pg_inbounds"})
OWNER_VIEW = NavViewer(role="admin", perms=frozenset(), pg_perms=ALL_PG, is_owner=True,
                       can_add_representative=False, can_manage_representatives=True)
SHOP_VIEW = NavViewer(role="reseller", perms=frozenset({"dashboard", "orders", "plans", "tickets"}), pg_perms=frozenset(),
                      is_owner=False, can_add_representative=True, can_manage_representatives=False)
PG_STAFF_VIEW = NavViewer(role="pg_staff", perms=frozenset(), pg_perms=frozenset({"pg_users"}), is_owner=False,
                          can_add_representative=False, can_manage_representatives=False)

OWNER = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0, "org_parent_id": None,
         "org_status": "active"}
SHOP_STAFF = {"role": "reseller", "permissions": ["shop_settings", "dashboard"], "bot_user_id": 10}


def bar(viewer: NavViewer, order: tuple[str, ...], path: str = "/home") -> list[str]:
    return [item.key for item in build_dock(build_sidebar(viewer, path=path), order).items]


class BarOrderTests(unittest.TestCase):
    def test_without_a_setting_the_bar_keeps_its_default_order(self) -> None:
        self.assertEqual(bar(OWNER_VIEW, ()), ["home", "inbox", "finance", "users"])

    def test_chosen_pages_come_first_in_the_chosen_order(self) -> None:
        self.assertEqual(bar(OWNER_VIEW, ("plans", "pg_nodes", "home", "tickets")), ["plans", "pg_nodes", "home", "tickets"])

    def test_empty_places_are_filled_with_the_default_order(self) -> None:
        self.assertEqual(bar(OWNER_VIEW, ("plans",)), ["plans", "home", "inbox", "finance"])

    def test_a_page_the_viewer_may_not_open_is_skipped_and_filled(self) -> None:
        self.assertEqual(bar(SHOP_VIEW, ("pg_nodes", "plans")), ["plans", "home", "inbox", "finance"])
        for viewer in (OWNER_VIEW, SHOP_VIEW, PG_STAFF_VIEW):
            sidebar_keys = {i.key for s in build_sidebar(viewer, path="/home") for i in s.items}
            keys = bar(viewer, ("system_settings", "pg_admins", "service_cleanup", "plans"))
            self.assertLessEqual(len(keys), DOCK_SLOTS)
            self.assertTrue(set(keys) <= sidebar_keys, keys)

    def test_one_choice_covers_the_same_page_for_every_role(self) -> None:
        self.assertEqual(bar(SHOP_VIEW, ("users",))[0], "customers")
        self.assertEqual(bar(PG_STAFF_VIEW, ("tickets",))[0], "pg_tickets")

    def test_the_active_page_and_menu_state_follow_the_custom_order(self) -> None:
        dock = build_dock(build_sidebar(OWNER_VIEW, path="/plans"), ("plans",))
        self.assertEqual([i.key for i in dock.items if i.active], ["plans"])
        self.assertFalse(dock.more_active)


class SlotValidationTests(unittest.TestCase):
    def test_choices_are_every_sidebar_page_once_grouped_by_section(self) -> None:
        keys = [c.key for c in dock_choices()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("plans", keys)
        self.assertIn("pg_nodes", keys)
        self.assertNotIn("customers", keys, "folded into the users choice")
        self.assertEqual([c.section for c in dock_choices()][0], "سیستم")
        labels = [c.label for c in dock_choices()]
        self.assertEqual(len(labels), len(set(labels)), "the panel dropdown is one flat list: every label must be unique")

    def test_empty_slots_are_left_to_the_default_order(self) -> None:
        self.assertEqual(clean_bar_slots(["plans", "", None, "home"]), ("plans", "home"))
        self.assertEqual(clean_bar_slots(["", "", "", ""]), ())

    def test_unknown_or_repeated_pages_are_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, UNKNOWN_PAGE):
            clean_bar_slots(["plans", "/admin<script>"])
        with self.assertRaisesRegex(ValueError, DUPLICATE_PAGE):
            clean_bar_slots(["plans", "plans"])

    def test_extra_slots_are_ignored(self) -> None:
        self.assertEqual(clean_bar_slots(["home", "inbox", "plans", "finance", "tickets"]), ("home", "inbox", "plans", "finance"))

    def test_a_stored_value_from_an_older_release_or_a_hand_edit_is_read_safely(self) -> None:
        self.assertEqual(parse_bar_order(" plans ,gone_page,plans,home,inbox,finance,tickets"), ("plans", "home", "inbox", "finance"))
        self.assertEqual(parse_bar_order(None), ())


class StoreTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'bar.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        mobile_bar.invalidate_bar_cache()

    async def asyncTearDown(self) -> None:
        mobile_bar.invalidate_bar_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def stored(self) -> str:
        async with self.Session() as session:
            return await get_setting(session, SETTING_KEY, "")


class StoreTests(StoreTestCase):
    async def test_a_saved_order_is_used_at_once_despite_the_cache(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await load_bar_order(session), ())
            await save_bar_order(session, ["plans", "", "tickets", ""])
            self.assertEqual(await load_bar_order(session), ("plans", "tickets"))
        self.assertEqual(await self.stored(), "plans,tickets")

    async def test_the_form_shows_saved_slots_then_automatic_ones(self) -> None:
        async with self.Session() as session:
            await save_bar_order(session, ["pg_users"])
            form = await load_bar_form(session)
        self.assertEqual(form.slots, ("pg_users", "", "", ""))
        self.assertEqual([section for section, _ in form.groups], ["سیستم", "فروشگاه", "پاسارگارد"])

    async def test_a_read_failure_shows_the_default_order_instead_of_breaking_the_page(self) -> None:
        async with self.Session() as session:
            with patch.object(mobile_bar, "get_setting", AsyncMock(side_effect=RuntimeError("db down"))), \
                    self.assertLogs("app.services.mobile_bar", level="WARNING"):
                self.assertEqual(await load_bar_order(session), ())


class RouteTests(StoreTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import render

        self.staff: dict = {}

        def require_admin() -> dict:
            # The same rule as the panel's require_admin: the explicit Owner only.
            if self.staff.get("role") != "admin" or not is_explicit_owner_staff(self.staff):
                raise HTTPException(403)
            return self.staff

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_mobile_bar_settings(app, require_admin=require_admin, get_db=get_db)

        @app.get("/probe")
        async def probe(request: Request, session=Depends(get_db)):
            request.state.mobile_bar_order = await load_bar_order(session)
            summary = SimpleNamespace(trials=0, buyers=0, conversion_percent=0)
            return render(
                request, "trial_users.html",
                {"staff": OWNER, "is_owner": True, "rows": [], "next_before": None, "summary": summary,
                 "filters": {"all": "همه"}, "active_filter": "all", "q": "", "settings_href": None,
                 "can_message": False, "can_block": False, "return_to": "/trial-users"},
            )

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def post(self, staff: dict, **form: str) -> httpx.Response:
        self.staff.clear()
        self.staff.update(staff)
        return await self.client.post("/settings/mobile-bar", data=form, follow_redirects=False)

    async def test_the_owner_saves_the_order_and_lands_back_on_the_box(self) -> None:
        response = await self.post(OWNER, bar_slot_1="plans", bar_slot_2="", bar_slot_3="pg_users", bar_slot_4="home")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/settings?tab=pwa&saved=1"))
        self.assertTrue(response.headers["location"].endswith("#mobile-bar"))
        self.assertEqual(await self.stored(), "plans,pg_users,home")

    async def test_the_bar_on_every_page_follows_the_saved_order(self) -> None:
        # This probe's owner has no PasarGuard login, so pg_nodes is skipped and the default order fills in.
        await self.post(OWNER, bar_slot_1="plans", bar_slot_2="pg_nodes", bar_slot_3="tickets")
        page = (await self.client.get("/probe")).text
        bar_html = page.split("data-bottom-dock>")[1].split("</nav>")[0]
        self.assertEqual(re.findall(r'<a href="([^"]+)" class="bottom-dock-item', bar_html),
                         ["/plans", "/tickets", "/home", "/inbox"])

    async def test_reset_returns_to_the_default_order(self) -> None:
        await self.post(OWNER, bar_slot_1="plans")
        response = await self.post(OWNER, bar_slot_1="plans", bar_reset="1")
        self.assertEqual(response.status_code, 303)
        self.assertEqual(await self.stored(), "")

    async def test_shop_staff_and_a_bare_admin_are_refused_and_nothing_is_stored(self) -> None:
        for staff in (SHOP_STAFF, {"role": "admin", "permissions": []}, {"role": "pg_staff", "permissions": []}):
            response = await self.post(staff, bar_slot_1="plans")
            self.assertEqual(response.status_code, 403, staff)
        self.assertEqual(await self.stored(), "")

    async def test_bad_input_is_reported_back_and_stores_nothing(self) -> None:
        for form in ({"bar_slot_1": "plans", "bar_slot_2": "plans"}, {"bar_slot_1": "<b>x</b>"}):
            response = await self.post(OWNER, **form)
            self.assertEqual(response.status_code, 303)
            self.assertIn("err=", response.headers["location"])
        self.assertEqual(await self.stored(), "")


class SettingsTabTests(unittest.TestCase):
    def test_the_mobile_bar_box_is_first_in_the_appearance_tab_with_the_saved_choices(self) -> None:
        from app.api.app import templates
        from app.services.mobile_bar import MobileBarForm, _grouped
        from app.services.panel_brand import DEFAULT_BRAND

        form = MobileBarForm(slots=("plans", "pg_users", "", ""), groups=_grouped(dock_choices()))
        html = templates.get_template("_settings_pwa.html").render(
            pwa={"name_raw": "", "short_name_raw": "", "description_raw": "", "icon_preview": "/pwa/icon/192",
                 "has_custom_icon": False},
            brand_values={}, brand_form_action="/panel-brand", panel_brand=DEFAULT_BRAND, app_version="t",
            csrf_token="tok", mobile_bar=form,
        )
        self.assertLess(html.index("نوار موبایل"), html.index("برند وب‌پنل"))
        box = html[html.index('id="mobile-bar"'):html.index("</form>")]
        self.assertIn('action="/settings/mobile-bar"', html)
        self.assertIn('name="csrf_token" value="tok"', box)
        self.assertEqual(len(re.findall(r'<select name="bar_slot_\d"', box)), DOCK_SLOTS)
        first_slot = box[box.index('name="bar_slot_1"'):box.index('name="bar_slot_2"')]
        self.assertIn('<option value="plans" selected>', first_slot)
        self.assertEqual(len(re.findall(r'<option value="" selected>خودکار</option>', box)), 2)
        self.assertIn('<optgroup label="پاسارگارد">', box)


if __name__ == "__main__":
    unittest.main()
