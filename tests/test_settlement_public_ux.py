"""Settlement return pages + panel-hosted checkout go-link (Shaparak domain match)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from fastapi import FastAPI
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.settlement_pages import register_settlement_pages
from app.db import Base
from app.db.models import (
    BotUser,
    Payment,
    PaymentMethod,
    PaymentSettlement,
    PaymentStatus,
    Role,
    SettlementStatus,
)
from app.services.payment_settlement import CHANNEL_PSP, public_checkout_button_url
from app.services.users import clear_settings_cache

ROOT = Path(__file__).resolve().parents[1]


class SettlementStatusTemplateTests(unittest.TestCase):
    def render(self, **kwargs) -> str:
        env = Environment(
            loader=FileSystemLoader(str(ROOT / "app" / "web" / "templates")),
            autoescape=select_autoescape(["html", "xml"]),
        )
        base = {
            "code": "ok",
            "title": "پرداخت تأیید شد",
            "message": "می‌توانید به تلگرام برگردید.",
            "watermark": "OK",
            "kicker": "",
            "kicker_ltr": False,
            "ref": None,
            "primary_href": "tg://resolve?domain=shopbot",
            "primary_label": "بازگشت به ربات",
            "primary_fallback_href": "https://t.me/shopbot",
            "secondary_href": "",
            "secondary_label": "",
            "guide_heading": "",
            "guide_steps": [],
            "code_block": "",
            "guide_note": "",
            "body_html": "",
            "show_close_button": False,
            "close_button_label": "بستن",
            "footer": "می‌توانید به تلگرام برگردید.",
            "app_version": "0.0.0",
            "pwa_name": "MrClockBot",
        }
        base.update(kwargs)
        return env.get_template("panel_status.html").render(**base)

    def test_success_tone_uses_ok_status(self) -> None:
        html = self.render()
        self.assertIn('data-status="ok"', html)
        self.assertIn("پرداخت تأیید شد", html)
        self.assertIn("می‌توانید به تلگرام برگردید", html)

    def test_payment_page_uses_tg_resolve_not_plain_tme_alone(self) -> None:
        html = self.render()
        self.assertNotIn("panel-status-kicker", html)
        self.assertNotIn("panel-status-close-btn", html)
        self.assertNotIn("Telegram.WebApp.close", html)
        self.assertIn("بازگشت به ربات", html)
        self.assertIn('href="tg://resolve?domain=shopbot"', html)
        self.assertIn('data-https="https://t.me/shopbot"', html)
        self.assertIn("openTelegramLink", html)
        self.assertIn("panel-status-actions--single", html)

    def test_ascii_kicker_keeps_ltr(self) -> None:
        html = self.render(
            kicker="HTTP 404",
            kicker_ltr=True,
            primary_href="",
            primary_label="",
            primary_fallback_href="",
            show_close_button=False,
            code="404",
            title="یافت نشد",
        )
        self.assertIn('dir="ltr"', html)
        self.assertIn("HTTP 404", html)

    def test_warn_and_error_tones(self) -> None:
        warn = self.render(code="warn", title="در حال بررسی", watermark="WARN")
        err = self.render(code="error", title="ناموفق", watermark="ERR")
        self.assertIn('data-status="warn"', warn)
        self.assertIn('data-status="error"', err)


class ShopBotChatUrlTests(unittest.IsolatedAsyncioTestCase):
    async def test_shop_username_wins_over_platform(self) -> None:
        from app.services.ux20 import shop_bot_chat_url, shop_bot_https_url

        profile = MagicMock(bot_username="ShopBot")
        result = MagicMock()
        result.scalar_one_or_none.return_value = profile
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)

        with patch("app.config.get_settings") as gs:
            gs.return_value = MagicMock(bot_username="platformbot")
            url = await shop_bot_chat_url(session, shop_owner_id=42)
            https = await shop_bot_https_url(session, shop_owner_id=42)
        self.assertEqual(url, "tg://resolve?domain=ShopBot")
        self.assertEqual(https, "https://t.me/ShopBot")

    async def test_platform_fallback_when_shop_has_no_username(self) -> None:
        from app.services.ux20 import shop_bot_chat_url

        result = MagicMock()
        result.scalar_one_or_none.return_value = MagicMock(bot_username=None)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)

        with patch("app.config.get_settings") as gs:
            gs.return_value = MagicMock(bot_username="@MainBot")
            url = await shop_bot_chat_url(session, shop_owner_id=7)
        self.assertEqual(url, "tg://resolve?domain=MainBot")

    async def test_platform_shop_uses_settings_username(self) -> None:
        from app.services.ux20 import shop_bot_chat_url

        session = AsyncMock()
        with patch("app.config.get_settings") as gs:
            gs.return_value = MagicMock(bot_username="MainBot")
            url = await shop_bot_chat_url(session, shop_owner_id=None)
        self.assertEqual(url, "tg://resolve?domain=MainBot")
        session.execute.assert_not_called()

    async def test_missing_username_returns_none(self) -> None:
        from app.services.ux20 import shop_bot_chat_url

        session = AsyncMock()
        with patch("app.config.get_settings") as gs:
            gs.return_value = MagicMock(bot_username="")
            url = await shop_bot_chat_url(session, shop_owner_id=None)
        self.assertIsNone(url)


class BotTgResolveLinkTests(unittest.TestCase):
    def test_builds_native_deep_link(self) -> None:
        from app.services.ux20 import bot_tg_resolve_link, normalize_bot_username

        self.assertEqual(normalize_bot_username("@Shop_Bot"), "Shop_Bot")
        self.assertEqual(
            bot_tg_resolve_link("ShopBot"),
            "tg://resolve?domain=ShopBot",
        )
        self.assertIsNone(bot_tg_resolve_link(""))
        self.assertIsNone(bot_tg_resolve_link("ab"))  # too short
        self.assertIsNone(bot_tg_resolve_link("https://evil.example"))


class PublicCheckoutButtonUrlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'ux.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmp.cleanup()

    async def test_button_defaults_to_direct_psp_checkout_url(self) -> None:
        async with self.Session() as session:
            session.add(
                BotUser(id=1, telegram_id=101, referral_code="u1", role=Role.USER.value)
            )
            payment = Payment(
                user_id=1,
                amount=50_000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.flush()
            settlement = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                tenant_key=0,
                channel=CHANNEL_PSP,
                provider="sunpay",
                status=SettlementStatus.AWAITING.value,
                amount=50_000,
                currency="IRT",
                idempotency_key="psp:ux:1",
                checkout_token="tok-abc",
                checkout_url="https://payment.example.ir/pay/uuid-here",
            )
            session.add(settlement)
            await session.commit()
            await session.refresh(settlement)

        with patch(
            "app.services.public_https.https_public_base",
            return_value="https://bot.example.com:9000",
        ):
            link = public_checkout_button_url(settlement, ui={})
        self.assertEqual(link, "https://payment.example.ir/pay/uuid-here")
        self.assertNotIn("/payments/settlement/go/", link)

    async def test_button_panel_mode_uses_go_link(self) -> None:
        settlement = MagicMock(
            id=42,
            checkout_token="tok-abc",
            checkout_url="https://payment.example.ir/pay/uuid-here",
        )
        with patch(
            "app.services.public_https.https_public_base",
            return_value="https://bot.example.com:9000",
        ):
            link = public_checkout_button_url(
                settlement, ui={"psp_checkout_link_mode": "panel"}
            )
        self.assertTrue(
            link.startswith(
                "https://bot.example.com:9000/payments/settlement/go/42?"
            )
        )
        self.assertIn("t=tok-abc", link)

    def test_empty_checkout_url_returns_empty(self) -> None:
        settlement = MagicMock(checkout_url=None)
        self.assertEqual(public_checkout_button_url(settlement), "")


class SettlementGoEndpointTests(unittest.IsolatedAsyncioTestCase):
    """Public ``/payments/settlement/go/{id}`` — token gate + https-only redirect."""

    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'go.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        self._idem = 0

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_settlement_pages(app, get_db=get_db)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=False,
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _seed(
        self,
        *,
        token: str = "tok-go-1",
        checkout_url: str = "https://payment.example.ir/pay/abc",
        status: str = SettlementStatus.AWAITING.value,
    ) -> int:
        self._idem += 1
        async with self.Session() as session:
            if await session.get(BotUser, 1) is None:
                session.add(
                    BotUser(
                        id=1, telegram_id=101, referral_code="u1", role=Role.USER.value
                    )
                )
            payment = Payment(
                user_id=1,
                amount=50_000,
                method=PaymentMethod.PSP.value,
                status=PaymentStatus.PENDING.value,
                is_wallet_topup=True,
            )
            session.add(payment)
            await session.flush()
            settlement = PaymentSettlement(
                payment_id=payment.id,
                shop_owner_id=None,
                tenant_key=0,
                channel=CHANNEL_PSP,
                provider="sunpay",
                status=status,
                amount=50_000,
                currency="IRT",
                idempotency_key=f"psp:go:{self._idem}",
                checkout_token=token,
                checkout_url=checkout_url,
            )
            session.add(settlement)
            await session.commit()
            return int(settlement.id)

    async def test_wrong_or_empty_token_is_404(self) -> None:
        sid = await self._seed(token="correct-token")
        empty = await self.client.get(f"/payments/settlement/go/{sid}")
        wrong = await self.client.get(f"/payments/settlement/go/{sid}?t=nope")
        missing = await self.client.get(
            "/payments/settlement/go/999999?t=correct-token"
        )
        self.assertEqual(empty.status_code, 404)
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(missing.status_code, 404)

    async def test_valid_token_redirects_302_to_checkout(self) -> None:
        target = "https://payment.example.ir/pay/abc"
        sid = await self._seed(token="tok-ok", checkout_url=target)
        resp = await self.client.get(f"/payments/settlement/go/{sid}?t=tok-ok")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp.headers.get("location"), target)

    async def test_non_https_checkout_is_400(self) -> None:
        sid = await self._seed(
            token="tok-http",
            checkout_url="http://payment.example.ir/pay/abc",
        )
        resp = await self.client.get(f"/payments/settlement/go/{sid}?t=tok-http")
        self.assertEqual(resp.status_code, 400)

    async def test_settled_shows_already_confirmed_not_redirect(self) -> None:
        sid = await self._seed(
            token="tok-done",
            status=SettlementStatus.SETTLED.value,
        )
        resp = await self.client.get(f"/payments/settlement/go/{sid}?t=tok-done")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("قبلاً تأیید شده", resp.text)
        self.assertIn("می‌توانید به تلگرام برگردید", resp.text)
        self.assertIn("<title>موفق</title>", resp.text)
        self.assertNotIn("panel-status", resp.text)
        self.assertIsNone(resp.headers.get("location"))


class PspReturnMethodTests(unittest.IsolatedAsyncioTestCase):
    """Return endpoint must accept POST (Paystar browser handoff)."""

    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'ret.db'}"
        )
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_settlement_pages(app, get_db=get_db)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def test_post_return_is_not_method_not_allowed(self) -> None:
        resp = await self.client.post(
            "/payments/settlement/psp/sunpay/return/999999",
            data={"success": "1", "token": "x"},
        )
        self.assertNotEqual(resp.status_code, 405)
        self.assertEqual(resp.status_code, 404)
        self.assertIn("تسویه یافت نشد", resp.text)


if __name__ == "__main__":
    unittest.main()
