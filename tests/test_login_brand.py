"""The sign-in page shows the platform brand from the web panel settings, never a shop's."""

from __future__ import annotations

from unittest.mock import patch

import httpx
from fastapi import FastAPI, Request
from sqlalchemy.exc import OperationalError

from app.api import login_brand
from app.api.login_brand import register_login_brand
from app.services.panel_brand import DEFAULT_BRAND, save_brand
from tests.test_panel_brand import BrandStoreTestCase, _image_bytes


def _render(request: Request, name: str, ctx: dict):
    from app.api.app import render

    return render(request, name, ctx)


class LoginBrandTests(BrandStoreTestCase):
    def app(self) -> FastAPI:
        app = FastAPI()
        register_login_brand(app, session_factory=self.Session)

        @app.get("/login")
        async def login(request: Request):
            return _render(request, "login.html", {})

        return app

    async def login_page(self) -> str:
        transport = httpx.ASGITransport(app=self.app())
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/login")
        self.assertEqual(response.status_code, 200)
        return response.text

    async def test_the_default_brand_shows_until_one_is_set(self) -> None:
        html = await self.login_page()
        self.assertIn("MrClockBot", html)
        self.assertIn('src="/static/logo-64.png"', html)

    async def test_the_platform_name_logo_and_colour_show_on_the_login_page(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=None, name="ساعت <من>", color="blue", logo_bytes=_image_bytes("PNG"))
        html = await self.login_page()
        self.assertIn("ساعت &lt;من&gt;", html)
        self.assertNotIn("ساعت <من>", html)
        self.assertRegex(html, r'src="/media/uploads/brand_p_[0-9a-f]{12}\.png"')
        self.assertRegex(html, r'<html[^>]*data-brand="blue"')
        self.assertNotIn(">MrClockBot<", html)

    async def test_a_shops_brand_never_reaches_the_login_page(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="فروشگاه الف", color="green")
        html = await self.login_page()
        self.assertNotIn("فروشگاه الف", html)
        self.assertNotRegex(html, r'<html[^>]*data-brand=')

    async def test_an_unreadable_brand_falls_back_to_the_default(self) -> None:
        failing = patch.object(login_brand, "load_brand", side_effect=OperationalError("select", {}, Exception("down")))
        with failing:
            self.assertEqual(await login_brand.platform_brand(self.Session), DEFAULT_BRAND)
