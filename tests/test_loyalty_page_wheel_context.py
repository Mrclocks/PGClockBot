"""Loyalty page must render with wheel context; status pages match panel design."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.services.formatting import format_money

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app/web/templates"
LOYALTY_PAGES = ROOT / "app/api/loyalty_pages.py"
APP_PY = ROOT / "app/api/app.py"
CSS = ROOT / "app/web/static/panel.css"


def _jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["num"] = (
        lambda v: f"{int(v):,}".replace(",", "٬") if v is not None else "۰"
    )
    env.filters["gb"] = lambda v: str(v)
    env.filters["money"] = format_money
    return env


def _base_loyalty_ctx(**extra):
    req = MagicMock()
    req.url.path = "/loyalty"
    req.query_params = {}
    ctx = {
        "request": req,
        "staff": {"role": "admin", "permissions": ["loyalty"]},
        "tab": "overview",
        "metrics": {
            "total_referrals": 0,
            "qualified_referrals": 0,
            "points_issued": 0,
            "points_redeemed": 0,
            "wallet_credits_issued": 0,
            "active_rewards": 0,
            "top_referrers": [],
        },
        "rules": [],
        "rewards": [],
        "tiers": [],
        "txs": [],
        "event_labels": {},
        "reward_type_labels": {},
        "loyalty_enabled": "1",
        "wallet_rate": "100",
        "is_platform_admin": True,
        "flash_ok": None,
        "flash_err": None,
        "flash_msg": None,
        "open_loyalty_settings": False,
        "loyalty_settings_tab": "club",
        "values": {},
        "referral_tab_groups": [],
        "referral_groups": {},
        "referral_text_action": "",
        "app_version": "9.0.0",
        "pwa_name": "MrClockBot",
        "tickets_unread": 0,
        "inbox_alert": False,
        "lucky_wheel_enabled": "0",
        "lucky_wheel_spin_cost_points": 10,
        "lucky_wheel_daily_limit": 3,
        "lucky_wheel_cooldown_seconds": 0,
        "lucky_wheel_free_spins_daily": 0,
        "loyalty_submenu_order": "loy_referral,loy_points,loy_rewards,loy_wheel,loy_history",
        "wheel_prizes": [],
        "wheel_spins": [],
        "wheel_prize_type_labels": {"none": "خالی", "points": "امتیاز"},
    }
    ctx.update(extra)
    return ctx


class LoyaltyPageRenderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "loy.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    def test_handler_passes_wheel_context_keys(self):
        src = LOYALTY_PAGES.read_text(encoding="utf-8")
        self.assertIn("_wheel_panel_context", src)
        self.assertIn('"wheel_metrics"', src)
        self.assertIn('"wheel_prizes"', src)
        self.assertIn('"wheel_spins"', src)
        self.assertIn('"lucky_wheel_enabled"', src)
        self.assertIn('"loyalty_submenu_order"', src)
        self.assertIn('"wheel_prize_type_labels"', src)
        self.assertIn('"wheel"', src)

    async def test_wheel_panel_context_shape(self):
        from app.api.loyalty_pages import _wheel_panel_context

        async with self.Session() as session:
            ctx = await _wheel_panel_context(session, reseller_id=None)
        for key in (
            "wheel_metrics",
            "lucky_wheel_enabled",
            "lucky_wheel_spin_cost_points",
            "lucky_wheel_daily_limit",
            "lucky_wheel_cooldown_seconds",
            "lucky_wheel_free_spins_daily",
            "loyalty_submenu_order",
            "wheel_prizes",
            "wheel_spins",
            "wheel_prize_type_labels",
        ):
            self.assertIn(key, ctx)
        self.assertIn("wheel_enabled", ctx["wheel_metrics"])
        self.assertIn("active_prizes", ctx["wheel_metrics"])
        self.assertIn("total_spins", ctx["wheel_metrics"])

    def test_loyalty_template_renders_with_full_wheel_context(self):
        env = _jinja_env()
        tpl = env.get_template("loyalty.html")
        html = tpl.render(
            **_base_loyalty_ctx(
                wheel_metrics={
                    "wheel_enabled": True,
                    "active_prizes": 2,
                    "total_spins": 5,
                    "spin_cost": 10,
                },
                lucky_wheel_enabled="1",
            )
        )
        self.assertIn("باشگاه مشتریان", html)
        self.assertIn("چرخ شانس", html)
        self.assertIn("بخش", html)
        self.assertIn("چرخش", html)

    def test_loyalty_template_safe_without_wheel_metrics(self):
        env = _jinja_env()
        tpl = env.get_template("loyalty.html")
        html = tpl.render(**_base_loyalty_ctx())
        self.assertIn("خاموش", html)


class PanelStatusPageTests(unittest.TestCase):
    def test_status_template_exists_and_renders(self):
        env = _jinja_env()
        tpl = env.get_template("panel_status.html")
        html = tpl.render(
            code=500,
            title="خطای داخلی سرور",
            message="مشکلی پیش آمد.",
            ref="abc12345",
            primary_href="/login",
            primary_label="صفحه ورود",
            secondary_href="/logout",
            secondary_label="خروج",
            app_version="9.0.0",
            pwa_name="MrClockBot",
        )
        self.assertIn("panel-status", html)
        self.assertIn("panel-status-card", html)
        self.assertIn("خطای داخلی سرور", html)
        self.assertIn("abc12345", html)
        self.assertIn(">ref<", html)
        self.assertIn("fonts.css", html)
        self.assertIn("panel.css", html)

    def test_status_css_tokens(self):
        css = CSS.read_text(encoding="utf-8")
        self.assertIn(".panel-status {", css)
        self.assertIn(".panel-status-card", css)
        self.assertIn(".panel-status-watermark", css)
        self.assertIn(".panel-status-badge", css)
        self.assertIn("panel-status-card-in", css)

    def test_app_uses_branded_status_not_inline_tahoma(self):
        src = APP_PY.read_text(encoding="utf-8")
        self.assertIn("panel_status.html", src)
        self.assertIn("_status_page", src)
        self.assertIn("StarletteHTTPException", src)
        self.assertNotIn("font-family:Tahoma", src)
        self.assertNotIn("خطای داخلی</title></head><body style=", src)


if __name__ == "__main__":
    unittest.main()
