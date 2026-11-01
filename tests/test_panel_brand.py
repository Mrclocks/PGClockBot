"""Panel brand: validation, isolation between shops, safe logo handling and who may edit what."""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import Depends, FastAPI, Request
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.panel_brand_pages import MAX_BODY_BYTES, register_panel_brand_pages
from app.db import Base
from app.db.models import BotUser, ResellerSetting, Role
from app.services import panel_brand
from app.services.panel_brand import (
    BRAND_PRESET_CSS,
    DEFAULT_BRAND,
    LOGO_EDGE,
    LOGO_KEY,
    LOGO_MAX_BYTES,
    NAME_MAX,
    PRESETS,
    BrandError,
    LogoError,
    brand_for_staff,
    clean_brand_name,
    load_brand,
    load_own_values,
    normalize_color,
    process_logo,
    save_brand,
)

OWNER = {
    "role": "admin",
    "permissions": [],
    "org_principal_id": 1,
    "org_depth": 0,
    "org_parent_id": None,
    "org_status": "active",
}
LEGACY_ADMIN = {"role": "admin", "permissions": []}


def _shop_staff(shop_id: int, permissions: list[str] | None = None) -> dict:
    return {"role": "reseller", "permissions": permissions if permissions is not None else ["shop_settings"], "bot_user_id": shop_id}


def _image_bytes(fmt: str, size: tuple[int, int] = (64, 64), mode: str = "RGBA", color=(200, 30, 30, 255)) -> bytes:
    out = BytesIO()
    Image.new(mode, size, color).save(out, format=fmt)
    return out.getvalue()


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


class PresetTests(unittest.TestCase):
    def test_every_preset_text_is_readable_on_its_themes_backgrounds(self) -> None:
        for preset in PRESETS:
            with self.subTest(preset=preset.key):
                for background in ("#09090b", "#0c0c0e"):
                    self.assertGreaterEqual(_contrast(preset.dark_text, background), 4.5)
                    self.assertGreaterEqual(_contrast(preset.dark_brand, background), 3.0)
                for background in ("#ffffff", "#fafafa"):
                    self.assertGreaterEqual(_contrast(preset.light_text, background), 4.5)
                    self.assertGreaterEqual(_contrast(preset.light_brand, background), 3.0)

    def test_the_generated_stylesheet_covers_both_themes_and_holds_only_fixed_tokens(self) -> None:
        css = str(BRAND_PRESET_CSS)
        for preset in PRESETS:
            self.assertIn(f'[data-brand="{preset.key}"]', css)
            self.assertIn(f'html[data-theme="light"][data-brand="{preset.key}"]', css)
        self.assertRegex(css, r'^[a-z0-9#:;,.()\[\]"=\-% {}\s]+$')
        self.assertNotIn("<", css)
        self.assertNotIn("url(", css)

    def test_keys_are_unique_and_the_default_is_not_a_preset(self) -> None:
        keys = [preset.key for preset in PRESETS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertNotIn("", keys)

    def test_a_colour_outside_the_presets_is_refused_on_save_and_reads_as_default_after(self) -> None:
        self.assertEqual(normalize_color(" Blue "), "blue")
        self.assertEqual(normalize_color(""), "")
        for raw in ("#ff0000", "red", "blue;}</style>", "expression(1)"):
            with self.subTest(raw=raw), self.assertRaises(BrandError):
                normalize_color(raw)
        self.assertEqual(panel_brand._stored_color("#ff0000"), "")


class NameTests(unittest.TestCase):
    def test_a_name_is_one_clean_line(self) -> None:
        self.assertEqual(clean_brand_name("  فروشگاه \n  من\t"), "فروشگاه من")
        self.assertEqual(clean_brand_name("a​b‮c⁦d"), "abcd")
        self.assertEqual(clean_brand_name(None), "")

    def test_too_long_names_are_refused_with_a_safe_message(self) -> None:
        with self.assertRaisesRegex(BrandError, str(NAME_MAX)):
            clean_brand_name("x" * (NAME_MAX + 1))
        self.assertEqual(len(clean_brand_name("x" * NAME_MAX)), NAME_MAX)


class LogoProcessingTests(unittest.TestCase):
    def test_png_jpeg_and_webp_become_one_128px_png(self) -> None:
        for fmt in ("PNG", "JPEG", "WEBP"):
            with self.subTest(fmt=fmt):
                mode = "RGB" if fmt == "JPEG" else "RGBA"
                out = process_logo(_image_bytes(fmt, (300, 200), mode, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 255)))
                image = Image.open(BytesIO(out))
                self.assertEqual((image.format, image.size, image.mode), ("PNG", (LOGO_EDGE, LOGO_EDGE), "RGBA"))

    def test_a_wide_logo_is_shown_whole_not_cropped(self) -> None:
        image = Image.open(BytesIO(process_logo(_image_bytes("PNG", (400, 100))))).convert("RGBA")
        alpha = image.getchannel("A")
        self.assertEqual(alpha.getbbox(), (0, 48, LOGO_EDGE, 80))  # 128x32 content, centred vertically
        self.assertEqual(alpha.getpixel((0, 0)), 0)

    def test_a_tiny_logo_is_scaled_up_to_the_frame(self) -> None:
        image = Image.open(BytesIO(process_logo(_image_bytes("PNG", (16, 16))))).convert("RGBA")
        self.assertEqual(image.getchannel("A").getbbox(), (0, 0, LOGO_EDGE, LOGO_EDGE))

    def test_vector_text_and_garbage_are_refused(self) -> None:
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'
        for label, data in {
            "svg": svg,
            "html": b"<html><script>alert(1)</script></html>",
            "text": b"just text",
            "empty": b"",
            "truncated png": _image_bytes("PNG")[:40],
            "gif": _image_bytes("GIF", mode="P", color=1),
        }.items():
            with self.subTest(label=label), self.assertRaises(LogoError):
                process_logo(data)

    def test_oversized_files_and_huge_pixel_counts_are_refused_before_decoding(self) -> None:
        with self.assertRaisesRegex(LogoError, "۲ مگابایت"):
            process_logo(b"\x89PNG" + b"0" * LOGO_MAX_BYTES)
        huge = _image_bytes("PNG", (6000, 5000), "L", 0)  # compresses to a few KB, 30 million pixels
        self.assertLess(len(huge), LOGO_MAX_BYTES)
        with self.assertRaisesRegex(LogoError, "ابعاد"):
            process_logo(huge)

    def test_metadata_of_the_upload_is_dropped(self) -> None:
        source = Image.new("RGBA", (64, 64), (1, 2, 3, 255))
        exif = Image.Exif()
        exif[0x010E] = "secret description"
        buf = BytesIO()
        source.save(buf, format="PNG", exif=exif.tobytes())
        image = Image.open(BytesIO(process_logo(buf.getvalue())))
        self.assertNotIn("exif", image.info)
        self.assertNotIn(b"secret", process_logo(buf.getvalue()))


class BrandStoreTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'brand.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        patcher = patch.object(panel_brand, "DATA_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        panel_brand.invalidate_brand_cache()
        async with self.Session() as session:
            self.shop_a = await self._shop(session, 9001)
            self.shop_b = await self._shop(session, 9002)

    async def asyncTearDown(self) -> None:
        panel_brand.invalidate_brand_cache()
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _shop(self, session, telegram_id: int) -> BotUser:
        user = BotUser(
            telegram_id=telegram_id, username=f"s{telegram_id}", full_name=f"shop {telegram_id}",
            role=Role.RESELLER.value, referral_code=f"R{telegram_id}",
        )
        session.add(user)
        await session.commit()
        return user

    def logo_files(self) -> list[str]:
        return sorted(p.name for p in (self.root / "uploads").glob("*")) if (self.root / "uploads").is_dir() else []


class IsolationTests(BrandStoreTestCase):
    async def test_a_shops_brand_never_reaches_another_shop_or_the_platform(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="Shop A", color="blue", logo_bytes=_image_bytes("PNG"))
            a = await load_brand(session, self.shop_a.id)
            b = await load_brand(session, self.shop_b.id)
            platform = await load_brand(session, None)
        self.assertEqual((a.name, a.color, a.has_custom_logo), ("Shop A", "blue", True))
        self.assertTrue(a.logo_url.startswith("/media/uploads/brand_r"))
        self.assertEqual(b, DEFAULT_BRAND)
        self.assertEqual(platform, DEFAULT_BRAND)

    async def test_a_shop_without_its_own_values_shows_the_platforms_field_by_field(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=None, name="Platform", color="purple", logo_bytes=_image_bytes("PNG"))
            await save_brand(session, shop_id=self.shop_a.id, name="", color="teal")
            a = await load_brand(session, self.shop_a.id)
            b = await load_brand(session, self.shop_b.id)
            platform = await load_brand(session, None)
        self.assertEqual((a.name, a.color), ("Platform", "teal"))
        self.assertEqual(a.logo_url, platform.logo_url)
        self.assertEqual((b.name, b.color, b.logo_url), ("Platform", "purple", platform.logo_url))

    async def test_the_platform_never_sees_a_shops_values(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="Shop A", color="pink", logo_bytes=_image_bytes("PNG"))
            own = await load_own_values(session, None)
        self.assertEqual(own, {"name": "", "logo": "", "color": ""})

    async def test_a_logo_path_that_is_not_the_scopes_own_is_ignored(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_b.id, name="B", color="", logo_bytes=_image_bytes("PNG"))
            b_logo = (await load_own_values(session, self.shop_b.id))["logo"]
            for tampered in (b_logo, "uploads/../bot.db", "/etc/passwd", "uploads/brand_p_aaaaaaaaaaaa.png", "uploads/x/../brand_r1_aaaaaaaaaaaa.png", "http://evil.example/x.png"):
                with self.subTest(tampered=tampered):
                    session.add(ResellerSetting(reseller_user_id=self.shop_a.id, key=LOGO_KEY, value=tampered))
                    await session.commit()
                    panel_brand.invalidate_brand_cache()
                    brand = await load_brand(session, self.shop_a.id)
                    self.assertFalse(brand.has_custom_logo)
                    row = await session.scalar(select(ResellerSetting).where(ResellerSetting.reseller_user_id == self.shop_a.id, ResellerSetting.key == LOGO_KEY))
                    await session.delete(row)
                    await session.commit()

    async def test_saving_again_is_picked_up_at_once_despite_the_cache(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="First", color="")
            self.assertEqual((await load_brand(session, self.shop_a.id)).name, "First")
            await save_brand(session, shop_id=self.shop_a.id, name="Second", color="")
            self.assertEqual((await load_brand(session, self.shop_a.id)).name, "Second")


class LogoFileTests(BrandStoreTestCase):
    async def test_the_stored_file_is_a_png_with_an_unguessable_name_inside_the_scope_prefix(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="", color="", logo_bytes=_image_bytes("JPEG", mode="RGB", color=(9, 9, 9)))
        (name,) = self.logo_files()
        self.assertRegex(name, rf"^brand_r{self.shop_a.id}_[0-9a-f]{{12}}\.png$")
        self.assertEqual(Image.open(self.root / "uploads" / name).format, "PNG")

    async def test_replacing_or_clearing_removes_only_the_scopes_own_old_file(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_b.id, name="", color="", logo_bytes=_image_bytes("PNG"))
            (b_file,) = self.logo_files()
            await save_brand(session, shop_id=self.shop_a.id, name="", color="", logo_bytes=_image_bytes("PNG"))
            await save_brand(session, shop_id=self.shop_a.id, name="", color="", logo_bytes=_image_bytes("PNG", color=(1, 1, 1, 255)))
            self.assertEqual(len(self.logo_files()), 2)
            self.assertIn(b_file, self.logo_files())
            await save_brand(session, shop_id=self.shop_a.id, name="", color="", clear_logo=True)
            self.assertEqual(self.logo_files(), [b_file])

    async def test_a_refused_upload_changes_nothing_and_leaves_no_file(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="Keep", color="blue")
            with self.assertRaises(LogoError):
                await save_brand(session, shop_id=self.shop_a.id, name="Changed", color="green", logo_bytes=b"<svg onload=alert(1)>")
            brand = await load_brand(session, self.shop_a.id)
        self.assertEqual((brand.name, brand.color), ("Keep", "blue"))
        self.assertEqual(self.logo_files(), [])

    async def test_a_failed_settings_write_does_not_leave_the_new_file_behind(self) -> None:
        async with self.Session() as session:
            with patch.object(panel_brand, "set_settings_bulk", side_effect=RuntimeError("db down")), self.assertRaises(RuntimeError):
                await save_brand(session, shop_id=self.shop_a.id, name="x", color="", logo_bytes=_image_bytes("PNG"))
        self.assertEqual(self.logo_files(), [])

    async def test_a_lookup_failure_shows_the_default_instead_of_breaking_the_page(self) -> None:
        async with self.Session() as session:
            with patch.object(panel_brand, "load_brand", side_effect=RuntimeError("db down")):
                self.assertEqual(await brand_for_staff(session, OWNER), DEFAULT_BRAND)


class BrandRouteTests(BrandStoreTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import render

        self.staff: dict = {}

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_panel_brand_pages(app, require_staff=lambda: self.staff, get_db=get_db)

        @app.get("/probe")
        async def probe(request: Request, session=Depends(get_db)):
            request.state.panel_brand = await brand_for_staff(session, self.staff)
            summary = SimpleNamespace(trials=0, buyers=0, conversion_percent=0)
            return render(
                request, "trial_users.html",
                {"staff": self.staff, "rows": [], "next_before": None, "summary": summary, "filters": {"all": "همه"},
                 "active_filter": "all", "q": "", "settings_href": None, "can_message": False, "can_block": False, "return_to": "/trial-users"},
            )

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def _post(self, staff: dict, files=None, **form):
        self.staff.clear()
        self.staff.update(staff)
        return await self.client.post("/panel-brand", data=form, files=files, follow_redirects=False)

    async def _page(self, staff: dict) -> str:
        self.staff.clear()
        self.staff.update(staff)
        panel_brand.invalidate_brand_cache()
        return (await self.client.get("/probe")).text

    async def test_the_owner_saves_the_platform_brand_and_lands_back_on_the_tab(self) -> None:
        response = await self._post(OWNER, panel_brand_name="Owner Co", panel_brand_color="green")
        self.assertEqual(response.status_code, 303)
        self.assertTrue(response.headers["location"].startswith("/settings?tab=pwa&saved=1"))
        async with self.Session() as session:
            self.assertEqual((await load_brand(session, None)).name, "Owner Co")

    async def test_a_shop_with_shop_settings_saves_only_its_own_brand_whatever_the_form_says(self) -> None:
        response = await self._post(
            _shop_staff(self.shop_a.id), panel_brand_name="A Co", panel_brand_color="teal",
            shop_id=str(self.shop_b.id), reseller_id=str(self.shop_b.id), scope="platform",
        )
        self.assertTrue(response.headers["location"].startswith("/shop-settings?tab=brand&saved=1"))
        async with self.Session() as session:
            self.assertEqual((await load_brand(session, self.shop_a.id)).name, "A Co")
            self.assertEqual(await load_brand(session, self.shop_b.id), DEFAULT_BRAND)
            self.assertEqual(await load_brand(session, None), DEFAULT_BRAND)

    async def test_everyone_else_is_refused_and_nothing_is_stored(self) -> None:
        for label, staff in {
            "shop staff without shop_settings": _shop_staff(self.shop_a.id, ["orders", "campaigns"]),
            "shop staff with no permissions": _shop_staff(self.shop_a.id, []),
            "reseller without a shop": {"role": "reseller", "permissions": ["shop_settings"], "bot_user_id": None},
            "legacy admin without an owner principal": LEGACY_ADMIN,
            "panel staff": {"role": "pg_staff", "permissions": ["shop_settings"]},
        }.items():
            with self.subTest(label):
                response = await self._post(staff, panel_brand_name="Nope")
                self.assertEqual(response.status_code, 403)
        async with self.Session() as session:
            for scope in (None, self.shop_a.id):
                self.assertEqual(await load_own_values(session, scope), {"name": "", "logo": "", "color": ""})

    async def test_bad_input_is_reported_back_and_stores_nothing(self) -> None:
        svg = ("logo.png", b"<svg onload=alert(1)></svg>", "image/png")
        cases = {
            "colour": {"panel_brand_name": "ok", "panel_brand_color": "#ff0000"},
            "name": {"panel_brand_name": "x" * (NAME_MAX + 1), "panel_brand_color": ""},
        }
        for label, form in cases.items():
            with self.subTest(label):
                response = await self._post(_shop_staff(self.shop_a.id), **form)
                self.assertIn("err=", response.headers["location"])
        response = await self._post(_shop_staff(self.shop_a.id), files={"panel_brand_logo": svg}, panel_brand_name="ok")
        self.assertIn("err=", response.headers["location"])
        async with self.Session() as session:
            self.assertEqual(await load_brand(session, self.shop_a.id), DEFAULT_BRAND)
        self.assertEqual(self.logo_files(), [])

    async def test_an_oversized_body_is_refused_before_it_is_read(self) -> None:
        self.staff.clear()
        self.staff.update(_shop_staff(self.shop_a.id))
        response = await self.client.post(
            "/panel-brand", content=b"x", headers={"content-length": str(MAX_BODY_BYTES + 1), "content-type": "multipart/form-data; boundary=x"},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])

    async def test_a_logo_upload_is_stored_and_shown_in_the_header_of_that_shop_only(self) -> None:
        png = ("logo.png", _image_bytes("PNG"), "image/png")
        await self._post(_shop_staff(self.shop_a.id), files={"panel_brand_logo": png}, panel_brand_name="Brand A", panel_brand_color="pink")
        await self._post(_shop_staff(self.shop_b.id), panel_brand_name="Brand B", panel_brand_color="green")
        page_a = await self._page(_shop_staff(self.shop_a.id))
        page_b = await self._page(_shop_staff(self.shop_b.id))
        self.assertIn("Brand A", page_a)
        self.assertRegex(page_a, r'<html[^>]*data-brand="pink"')
        self.assertIn("/media/uploads/brand_r", page_a)
        self.assertNotIn("Brand B", page_a)
        self.assertIn("Brand B", page_b)
        self.assertRegex(page_b, r'<html[^>]*data-brand="green"')
        self.assertNotIn("Brand A", page_b)
        self.assertNotIn(f"brand_r{self.shop_a.id}_", page_b)

    async def test_a_hostile_name_is_escaped_in_the_header(self) -> None:
        await self._post(OWNER, panel_brand_name='<img src=x onerror="alert(1)">', panel_brand_color="")
        page = await self._page(OWNER)
        self.assertNotIn('<img src=x onerror="alert(1)">', page)
        self.assertIn("&lt;img src=x", page)

    async def test_the_software_version_is_only_shown_to_the_owner(self) -> None:
        owner_page = await self._page(OWNER)
        shop_page = await self._page(_shop_staff(self.shop_a.id))
        version = re.compile(r"brand-ver-(line|dot)|topbar-version")
        self.assertRegex(owner_page, version)
        self.assertNotRegex(shop_page, version)

    async def test_without_any_brand_the_header_keeps_the_default(self) -> None:
        page = await self._page(_shop_staff(self.shop_a.id))
        self.assertIn("MrClockBot", page)
        self.assertIn("/static/logo-64.png", page)
        self.assertNotRegex(page, r"<html[^>]*data-brand=")


class SettingsTabTests(BrandStoreTestCase):
    """The tab as the settings pages serve it: shop settings (real route) and the Owner's web-app tab."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import render
        from app.api.shop_settings import register_shop_settings

        self.staff = _shop_staff(self.shop_a.id)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_shop_settings(app, render=render, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def test_the_shop_settings_page_serves_the_brand_tab_with_its_saved_values(self) -> None:
        async with self.Session() as session:
            await save_brand(session, shop_id=self.shop_a.id, name="Saved Name", color="purple")
            await save_brand(session, shop_id=self.shop_b.id, name="Other Shop", color="green")
        panel_brand.invalidate_brand_cache()
        response = await self.client.get("/shop-settings?tab=brand")
        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn('action="/panel-brand"', text)
        self.assertIn('value="Saved Name"', text)
        self.assertRegex(text, r'value="purple"\s+checked')
        self.assertIn("ظاهر و برند", text)
        self.assertNotIn("Other Shop", text)
        self.assertNotIn("آیکن وب‌اپ", text)  # the web-app (PWA) icon belongs to the platform only

    def test_the_owners_tab_holds_the_brand_card_and_the_web_app_form(self) -> None:
        from app.api.app import templates

        html = templates.get_template("_settings_pwa.html").render(
            pwa={"name": "Panel", "short_name": "P", "description": "", "name_raw": "", "short_name_raw": "", "description_raw": "",
                 "icon_preview": "/pwa/icon/192", "has_custom_icon": False},
            brand_values={"name": "Owner Brand", "logo": "", "color": "blue"},
            brand_form_action="/panel-brand", panel_brand=DEFAULT_BRAND, app_version="t", csrf_token="tok",
        )
        self.assertIn('action="/panel-brand"', html)
        self.assertIn('value="Owner Brand"', html)
        self.assertIn('action="/settings?tab=pwa"', html)
        self.assertLess(html.index("برند وب‌پنل"), html.index("وب‌اپ (PWA)"))


if __name__ == "__main__":
    unittest.main()
