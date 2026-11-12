"""Authorization, validation and rendered controls for random discount management."""
from __future__ import annotations

import secrets
import unittest
from collections.abc import AsyncIterator
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.random_discounts import register_random_discounts
from app.db.models import Base, BotUser, RandomDiscount
from app.services.formatting import format_money, format_number
from app.services.marketing_nav import RANDOM_DISCOUNTS, marketing_groups, marketing_sections
from app.services.resellers import DEFAULT_FEATURE_PERMS, feature_perm_groups

TEMPLATES = Path(__file__).resolve().parents[1] / "app/web/templates"
OWNER = {"role": "admin", "org_principal_id": 1, "org_depth": 0, "org_status": "active"}
SHOP = {"role": "reseller", "bot_user_id": 2, "permissions": ["random_discounts"]}


class Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.fields: dict[str, dict[str, str | None]] = {}
        self.forms: list[dict[str, str | None]] = []
        self.modals: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        name = values.get("name")
        if tag in {"input", "select"} and name:
            self.fields[name] = values
        if tag == "form":
            self.forms.append(values)
        if tag == "div" and "ui-modal" in (values.get("class") or "").split():
            self.modals.append(values)


class RandomDiscountAdminTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=2, telegram_id=202, referral_code="shop2", role="reseller"),
                BotUser(id=3, telegram_id=203, referral_code="shop3", role="reseller"),
            ])
            await session.commit()
        self.staff = OWNER
        env = Environment(loader=ChoiceLoader([
            DictLoader({"base.html": "{% block content %}{% endblock %}"}),
            FileSystemLoader(str(TEMPLATES)),
        ]), autoescape=select_autoescape(["html"]))
        env.filters.update(num=format_number, money=format_money)
        env.globals["marketing_groups"] = marketing_groups

        def render(request: Request, template: str, context: dict) -> HTMLResponse:
            return HTMLResponse(env.get_template(template).render(
                request=request, csrf_token="test-csrf", marketing_sections=marketing_sections(self.staff), **context,
            ))

        async def staff() -> dict:
            return self.staff

        async def get_db() -> AsyncIterator[AsyncSession]:
            async with self.Session() as session:
                yield session

        self.app = FastAPI()
        register_random_discounts(self.app, render=render, require_staff=staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://example.test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()

    def form(self, **changes: str) -> dict[str, str]:
        return {
            "title": "تخفیف تازه", "mode": "automatic", "reduction_kind": "amount", "minimum": "10",
            "maximum": "100", "step": "1", "window_minutes": "60", "request_key": secrets.token_hex(16),
            **changes,
        }

    async def test_missing_permission_and_unscoped_staff_cannot_open_or_mutate(self) -> None:
        for profile in [{}, {"role": "admin"}, {"role": "reseller", "bot_user_id": 2, "permissions": ["catalog_discounts"]},
                        {"role": "reseller", "permissions": ["random_discounts"]},
                        {"role": "pg_staff", "permissions": ["random_discounts"]}]:
            self.staff = profile
            with self.subTest(profile=profile):
                self.assertEqual((await self.client.get("/random-discounts")).status_code, 403)
                self.assertEqual((await self.client.post("/random-discounts", data=self.form())).status_code, 403)
                self.assertEqual((await self.client.post("/random-discounts/1/stop")).status_code, 403)

    async def test_rendered_form_has_labels_numeric_limits_and_csrf(self) -> None:
        response = await self.client.get("/random-discounts")
        self.assertEqual(response.status_code, 200)
        controls = Controls()
        controls.feed(response.text)
        for name in ("title", "mode", "reduction_kind", "minimum", "maximum", "step", "window_minutes", "code"):
            self.assertIn(name, controls.fields)
        self.assertEqual(controls.fields["csrf_token"]["value"], "test-csrf")
        self.assertEqual(controls.fields["window_minutes"]["max"], "10080")
        self.assertEqual(controls.fields["minimum"]["dir"], "ltr")
        self.assertEqual(len(controls.fields["request_key"]["value"]), 32)
        self.assertTrue(all("ds" in row["class"].split() for row in controls.modals))
        self.assertIn("هنوز تخفیف تصادفی تعریف نشده", response.text)

    async def test_title_and_flash_are_escaped_and_stopping_requires_confirmation(self) -> None:
        title = '<img src=x onerror="alert(1)">'
        response = await self.client.post("/random-discounts", data=self.form(title=title))
        self.assertEqual(response.status_code, 303)
        response = await self.client.get("/random-discounts", params={"error": "<script>unsafe</script>"})
        self.assertNotIn("<img src=x", response.text)
        self.assertNotIn("<script>", response.text)
        self.assertIn("&lt;img", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        controls = Controls()
        controls.feed(response.text)
        stop_forms = [row for row in controls.forms if row.get("action", "").endswith("/stop")]
        self.assertEqual(len(stop_forms), 1)
        self.assertIn("سفارش‌های قبلی تغییر نمی‌کند", stop_forms[0]["data-confirm"])

    async def test_invalid_form_values_are_safe_errors_and_create_no_policy(self) -> None:
        for changes in [{"minimum": "NaN"}, {"maximum": "0"}, {"step": "-1"}, {"window_minutes": "10081"},
                        {"minimum": "20", "maximum": "10"}, {"mode": "code", "code": "<script>"},
                        {"reduction_kind": "percent", "maximum": "101"}]:
            with self.subTest(changes=changes):
                response = await self.client.post("/random-discounts", data=self.form(**changes))
                self.assertEqual(response.status_code, 303)
                self.assertIn("error", parse_qs(urlsplit(response.headers["location"]).query))
        async with self.Session() as session:
            self.assertEqual(await session.scalar(select(func.count()).select_from(RandomDiscount)), 0)
        self.assertEqual((await self.client.get("/random-discounts?before=NaN")).status_code, 400)

    async def test_duplicate_submission_creates_one_policy_and_other_shop_cannot_stop_it(self) -> None:
        self.staff = SHOP
        form = self.form()
        self.assertEqual((await self.client.post("/random-discounts", data=form)).status_code, 303)
        self.assertEqual((await self.client.post("/random-discounts", data=form)).status_code, 303)
        async with self.Session() as session:
            rows = list((await session.scalars(select(RandomDiscount))).all())
            self.assertEqual(len(rows), 1)
            policy_id = rows[0].id
        self.staff = {**SHOP, "bot_user_id": 3}
        self.assertNotIn("تخفیف تازه</span>", (await self.client.get("/random-discounts")).text)
        response = await self.client.post(f"/random-discounts/{policy_id}/stop")
        self.assertIn("error", parse_qs(urlsplit(response.headers["location"]).query))
        async with self.Session() as session:
            self.assertTrue((await session.get(RandomDiscount, policy_id)).is_active)
        self.staff = SHOP
        self.assertEqual((await self.client.post(f"/random-discounts/{policy_id}/stop")).status_code, 303)
        async with self.Session() as session:
            self.assertFalse((await session.get(RandomDiscount, policy_id)).is_active)

    def test_navigation_and_permission_defaults_require_explicit_grant(self) -> None:
        self.assertNotIn("random_discounts", DEFAULT_FEATURE_PERMS.split(","))
        permissions = [p for group in feature_perm_groups() for p in group.perms if p.key == "random_discounts"]
        self.assertEqual(len(permissions), 1)
        self.assertFalse(permissions[0].default)
        self.assertEqual([row.key for row in marketing_sections(SHOP)], [RANDOM_DISCOUNTS])
        self.assertNotIn(RANDOM_DISCOUNTS, [row.key for row in marketing_sections({**SHOP, "permissions": ["catalog_discounts"]})])
        self.assertEqual(marketing_sections({**SHOP, "bot_user_id": None}), ())
