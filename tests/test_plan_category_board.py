"""Plans grouped by category as the bot shows them, and moving a category up or down in the bot's order."""

from __future__ import annotations

import unittest

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import Base, Plan, PlanCategory
from app.services.plan_categories import move_category
from app.services.plan_category_board import (
    NOTE_CATEGORY_OFF,
    NOTE_NO_ACTIVE_PLANS,
    NOTE_NO_PLANS,
    build_plan_board,
)
from app.services.shop_scope import ShopScopeError
from tests.postgres_support import add_shop_users, scoped_async_engine

SHOP = 42
OTHER_SHOP = 77


def _category(cid: int, name: str, *, order: int = 0, active: bool = True, audience: str = "users",
              style: str | None = None, owner: int | None = None) -> PlanCategory:
    return PlanCategory(id=cid, name=name, sort_order=order, is_active=active, audience=audience,
                        button_style=style, owner_reseller_id=owner)


def _plan(pid: int, name: str, *, category: int | None = None, active: bool = True) -> Plan:
    return Plan(id=pid, name=name, category_id=category, is_active=active, is_trial=False, price=1000)


def _names(plans: list[Plan]) -> list[str]:
    return [p.name for p in plans]


class PlanBoardTests(unittest.TestCase):
    def test_groups_follow_the_category_order_of_the_bot(self):
        board = build_plan_board(
            [_plan(1, "a", category=2), _plan(2, "b", category=1), _plan(3, "c")],
            [_category(1, "اقتصادی", order=5), _category(2, "ویژه", order=1)],
        )
        self.assertEqual([g.category.name for g in board.groups], ["ویژه", "اقتصادی"])
        self.assertEqual([_names(g.plans) for g in board.groups], [["a"], ["b"]])
        self.assertEqual(_names(board.other_plans), ["c"])
        self.assertEqual(board.other_title, "سایر")
        self.assertIn("«سایر»", board.other_note)

    def test_equal_order_numbers_fall_back_to_the_id_like_the_bot(self):
        board = build_plan_board([], [_category(9, "دوم"), _category(3, "اول")])
        self.assertEqual([g.category.name for g in board.groups], ["اول", "دوم"])

    def test_a_category_says_why_it_has_no_bot_button(self):
        board = build_plan_board(
            [_plan(1, "on", category=1), _plan(2, "off", category=3, active=False), _plan(3, "x", category=2)],
            [_category(1, "روشن"), _category(2, "خاموش", active=False), _category(3, "بی‌پلن روشن"),
             _category(4, "خالی")],
        )
        notes = {g.category.name: g.bot_note for g in board.groups}
        self.assertEqual(notes, {
            "روشن": None,
            "خاموش": NOTE_CATEGORY_OFF,
            "بی‌پلن روشن": NOTE_NO_ACTIVE_PLANS,
            "خالی": NOTE_NO_PLANS,
        })
        self.assertTrue(board.groups[0].in_bot)

    def test_without_a_category_in_the_bot_the_rest_sit_under_the_fixed_button(self):
        board = build_plan_board(
            [_plan(1, "a")], [_category(1, "خاموش", active=False)], ui={"btn_shop_kind_fixed": "پلن‌های ما"}
        )
        self.assertEqual(board.other_title, "بدون دسته")
        self.assertIn("«پلن‌های ما»", board.other_note)

    def test_no_user_category_means_no_grouping(self):
        plans = [_plan(1, "a", category=5), _plan(2, "b")]
        board = build_plan_board(plans, [_category(5, "نمایندگان", audience="resellers")])
        self.assertFalse(board.has_categories)
        self.assertEqual(_names(board.other_plans), ["a", "b"])

    def test_a_plan_pointing_at_a_foreign_category_is_listed_without_one(self):
        board = build_plan_board([_plan(1, "a", category=99)], [_category(1, "ویژه")])
        self.assertEqual(_names(board.groups[0].plans), [])
        self.assertEqual(_names(board.other_plans), ["a"])

    def test_the_group_wears_the_colour_of_its_bot_button(self):
        board = build_plan_board([], [
            _category(1, "آبی", style="primary"), _category(2, "سبز", style="success"),
            _category(3, "قرمز", style="danger"), _category(4, "سفید", style=""),
            _category(5, "ارث", style=None),
        ], ui={"btn_style_shop_kind_fixed": "danger"})
        # «ارث» takes the colour the bot gives the «ثابت» button.
        self.assertEqual([g.tone for g in board.groups], ["primary", "success", "danger", "default", "danger"])


def _admin() -> dict:
    return {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_parent_id": None,
            "org_status": "active", "pg_is_owner": True}


def _reseller(shop: int) -> dict:
    return {"role": "reseller", "bot_user_id": shop}


class _CategoryDbCase(unittest.IsolatedAsyncioTestCase):
    """Two shops, the platform and a reseller-audience category, each with its own order."""

    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        await add_shop_users(self.Session, SHOP, OTHER_SHOP)
        async with self.Session() as session:
            session.add_all([
                _category(1, "الف", order=0, owner=SHOP),
                _category(2, "ب", order=0, owner=SHOP),
                _category(3, "ج", order=7, owner=SHOP),
                _category(4, "نمایندگان", order=1, owner=SHOP, audience="resellers"),
                _category(5, "فروشگاه دیگر", order=0, owner=OTHER_SHOP),
                _category(6, "پلتفرم", order=0, owner=None),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def _order(self, owner: int | None, audience: str = "users") -> list[str]:
        async with self.Session() as session:
            cond = PlanCategory.owner_reseller_id.is_(None) if owner is None else PlanCategory.owner_reseller_id == owner
            rows = await session.scalars(
                select(PlanCategory).where(cond, PlanCategory.audience == audience)
                .order_by(PlanCategory.sort_order, PlanCategory.id)
            )
            return [row.name for row in rows]


class MoveCategoryTests(_CategoryDbCase):
    async def test_moving_up_swaps_with_the_neighbour_and_renumbers(self):
        async with self.Session() as session:
            self.assertTrue(await move_category(session, _reseller(SHOP), 2, direction="up"))
        self.assertEqual(await self._order(SHOP), ["ب", "الف", "ج"])
        async with self.Session() as session:
            orders = [c.sort_order for c in await session.scalars(
                select(PlanCategory).where(PlanCategory.owner_reseller_id == SHOP, PlanCategory.audience == "users")
                .order_by(PlanCategory.sort_order))]
        self.assertEqual(orders, [0, 1, 2])

    async def test_moving_down_from_the_middle(self):
        async with self.Session() as session:
            self.assertTrue(await move_category(session, _reseller(SHOP), 2, direction="down"))
        self.assertEqual(await self._order(SHOP), ["الف", "ج", "ب"])

    async def test_the_first_cannot_move_up_and_the_last_cannot_move_down(self):
        async with self.Session() as session:
            self.assertFalse(await move_category(session, _reseller(SHOP), 1, direction="up"))
            self.assertFalse(await move_category(session, _reseller(SHOP), 3, direction="down"))
        self.assertEqual(await self._order(SHOP), ["الف", "ب", "ج"])

    async def test_other_shops_and_audiences_keep_their_order(self):
        async with self.Session() as session:
            await move_category(session, _reseller(SHOP), 3, direction="up")
        self.assertEqual(await self._order(SHOP, "resellers"), ["نمایندگان"])
        self.assertEqual(await self._order(OTHER_SHOP), ["فروشگاه دیگر"])
        self.assertEqual(await self._order(None), ["پلتفرم"])

    async def test_another_shop_cannot_move_a_category(self):
        async with self.Session() as session:
            with self.assertRaises(ShopScopeError):
                await move_category(session, _reseller(OTHER_SHOP), 1, direction="down")
            with self.assertRaises(ShopScopeError):
                await move_category(session, _admin(), 1, direction="down")
        self.assertEqual(await self._order(SHOP), ["الف", "ب", "ج"])

    async def test_an_unknown_direction_is_rejected(self):
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await move_category(session, _reseller(SHOP), 1, direction="sideways")



class CategoryRouteTests(_CategoryDbCase):
    """The panel routes: the shop comes from the signed-in staff, never from the request."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import httpx
        from fastapi import FastAPI

        from app.api.plan_catalog_extras import register_plan_catalog_extras

        self.staff = _reseller(SHOP)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_plan_catalog_extras(app, require_perm=lambda _key: (lambda: self.staff), get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_move_returns_to_the_grouped_list(self):
        response = await self.client.post("/plans/categories/3/move", data={"direction": "up"})
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/plans?ok="))
        self.assertTrue(response.headers["location"].endswith("#plan-category-board"))
        self.assertEqual(await self._order(SHOP), ["الف", "ج", "ب"])

    async def test_move_of_another_shops_category_changes_nothing(self):
        self.staff = _reseller(OTHER_SHOP)
        response = await self.client.post("/plans/categories/2/move", data={"direction": "up"})
        self.assertIn("err=", response.headers["location"])
        self.assertEqual(await self._order(SHOP), ["الف", "ب", "ج"])

    async def test_toggle_from_the_list_goes_back_to_the_list(self):
        response = await self.client.post("/plans/categories/1/toggle", data={"back": "list"})
        self.assertTrue(response.headers["location"].startswith("/plans?ok="))
        async with self.Session() as session:
            self.assertFalse((await session.get(PlanCategory, 1)).is_active)

    async def test_an_unknown_back_value_is_never_followed(self):
        response = await self.client.post("/plans/categories/1/toggle", data={"back": "https://evil.example"})
        self.assertTrue(response.headers["location"].startswith("/plans?categories=1"))

    async def test_a_category_made_from_the_list_goes_last_and_the_page_lands_on_it(self):
        response = await self.client.post("/plans/categories", data={"name": "پرسرعت", "back": "list"})
        location = response.headers["location"]
        async with self.Session() as session:
            made = await session.scalar(select(PlanCategory).where(PlanCategory.name == "پرسرعت"))
        self.assertTrue(location.startswith("/plans?ok="))
        self.assertTrue(location.endswith(f"#plan-cat-{made.id}"))
        self.assertEqual((made.owner_reseller_id, made.audience), (SHOP, "users"))
        self.assertEqual((await self._order(SHOP))[-1], "پرسرعت")

    async def test_a_category_made_in_the_modal_keeps_the_modal_open(self):
        response = await self.client.post("/plans/categories", data={"name": "مودال"})
        self.assertTrue(response.headers["location"].startswith("/plans?categories=1&ok="))

    async def test_editing_a_category_keeps_its_place_in_the_bot(self):
        await self.client.post("/plans/categories/2/move", data={"direction": "up"})
        response = await self.client.post(
            "/plans/categories/2/edit", data={"name": "ب تازه", "button_style": "danger", "back": "list"}
        )
        self.assertTrue(response.headers["location"].endswith("#plan-cat-2"))
        self.assertEqual(await self._order(SHOP), ["ب تازه", "الف", "ج"])
        async with self.Session() as session:
            self.assertEqual((await session.get(PlanCategory, 2)).button_style, "danger")

    async def test_a_failed_save_from_the_list_returns_to_the_list_without_a_target(self):
        response = await self.client.post("/plans/categories", data={"name": "  ", "back": "list"})
        location = response.headers["location"]
        self.assertTrue(location.startswith("/plans?err="))
        self.assertTrue(location.endswith("#plan-category-board"))

    async def test_another_shop_cannot_edit_a_category(self):
        self.staff = _reseller(OTHER_SHOP)
        response = await self.client.post("/plans/categories/1/edit", data={"name": "ربوده"})
        self.assertIn("err=", response.headers["location"])
        async with self.Session() as session:
            self.assertEqual((await session.get(PlanCategory, 1)).name, "الف")

    async def test_quick_create_returns_the_category_and_a_repeat_returns_the_same_one(self):
        first = (await self.client.post("/plans/categories/quick", data={"name": " طلایی "})).json()
        again = (await self.client.post("/plans/categories/quick", data={"name": "طلایی"})).json()
        self.assertEqual((first["ok"], first["name"], first["created"]), (True, "طلایی", True))
        self.assertEqual((again["id"], again["created"]), (first["id"], False))
        async with self.Session() as session:
            category = await session.get(PlanCategory, first["id"])
            self.assertEqual((category.owner_reseller_id, category.audience), (SHOP, "users"))
        # A new category goes last in the bot.
        self.assertEqual((await self._order(SHOP))[-1], "طلایی")

    async def test_quick_create_saves_the_chosen_colour_and_answers_with_its_tone(self):
        made = (await self.client.post(
            "/plans/categories/quick", data={"name": "سبز", "button_style": "success"}
        )).json()
        self.assertEqual(made["tone"], "success")
        async with self.Session() as session:
            self.assertEqual((await session.get(PlanCategory, made["id"])).button_style, "success")
        # A repeat with another colour returns the first category as it is.
        again = (await self.client.post("/plans/categories/quick", data={"name": "سبز", "button_style": "danger"})).json()
        self.assertEqual((again["id"], again["tone"], again["created"]), (made["id"], "success", False))

    async def test_quick_create_keeps_shops_apart(self):
        self.staff = _reseller(OTHER_SHOP)
        made = (await self.client.post("/plans/categories/quick", data={"name": "الف"})).json()
        self.assertTrue(made["created"])
        async with self.Session() as session:
            self.assertEqual((await session.get(PlanCategory, made["id"])).owner_reseller_id, OTHER_SHOP)

    async def test_quick_create_rejects_an_empty_name_and_a_switched_off_twin(self):
        empty = await self.client.post("/plans/categories/quick", data={"name": "  "})
        self.assertEqual((empty.status_code, empty.json()["ok"]), (400, False))
        async with self.Session() as session:
            (await session.get(PlanCategory, 1)).is_active = False
            await session.commit()
        twin = await self.client.post("/plans/categories/quick", data={"name": "الف"})
        self.assertEqual(twin.status_code, 400)
        self.assertIn("خاموش", twin.json()["error"])


class PlanCategoryFieldTests(unittest.TestCase):
    """The category chips of the plan forms, rendered from the shared macro."""

    @staticmethod
    def render(categories: list[PlanCategory], **kwargs: object) -> str:
        from jinja2 import Environment, FileSystemLoader

        env = Environment(loader=FileSystemLoader("app/web/templates"), autoescape=True)
        macro = env.get_template("_plan_category_field.html").module.plan_category_field
        return str(macro(categories, {1: "success"}, **kwargs))

    def test_only_switched_on_user_categories_are_offered(self):
        html = self.render([
            _category(1, "ویژه"), _category(2, "خاموش", active=False), _category(3, "نمایندگان", audience="resellers"),
        ])
        self.assertIn('data-value="1" data-tone="success"', html)
        self.assertNotIn('data-value="2"', html)
        self.assertNotIn('data-value="3"', html)
        self.assertIn('<option value="" selected>', html)

    def test_the_plans_own_switched_off_category_stays_selected(self):
        html = self.render([_category(1, "ویژه"), _category(2, "قدیمی", active=False)], selected=2)
        self.assertIn('<option value="2" selected>', html)
        self.assertIn("plan-cat-chip-off", html)

    def test_a_hostile_category_name_is_escaped(self):
        html = self.render([_category(1, '<script>alert(1)</script>')])
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

if __name__ == "__main__":
    unittest.main()
