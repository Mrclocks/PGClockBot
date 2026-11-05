"""Manual wallet/points adjusts require a reason and notify the customer once."""

from __future__ import annotations

import html
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
SHOP_A = 81
SHOP_B = 82


class NoticeTextTests(unittest.TestCase):
    def test_wallet_and_points_notices_escape_reason_and_show_balance(self):
        from app.services.formatting import format_toman
        from app.services.manual_adjust_notices import points_change_notice, wallet_change_notice

        wallet = wallet_change_notice(
            mode="credit", amount=12_000, balance=50_000, note="<script>x</script>"
        )
        self.assertIn("شارژ شد", wallet)
        self.assertIn(format_toman(12_000), wallet)
        self.assertIn(format_toman(50_000), wallet)
        self.assertIn(html.escape("<script>x</script>"), wallet)
        self.assertNotIn("<script>", wallet)

        debit = wallet_change_notice(mode="debit", amount=1_000, balance=0, note=None)
        self.assertIn("کسر شد", debit)
        self.assertNotIn("توضیح:", debit)

        points = points_change_notice(mode="debit", points=7, balance=3, note="a&b")
        self.assertIn("کسر شد", points)
        self.assertIn("7", points)
        self.assertIn("3", points)
        self.assertIn("a&amp;b", points)

    def test_schedule_skips_replayed_adjust(self):
        from app.services import manual_adjust_notices as notices

        with patch.object(notices.asyncio, "get_running_loop") as loop:
            notices.schedule_customer_notice(1, "hi", shop_id=None, replayed=True)
            loop.assert_not_called()


class PlatformWalletReasonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "plat-wallet.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

        from app.db.models import BotUser, Role

        async with self.Session() as session:
            session.add(BotUser(id=1, telegram_id=101, referral_code="u1", role=Role.USER.value, wallet_balance=10_000))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_web_platform_wallet_rejects_missing_or_short_reason(self):
        from fastapi import FastAPI
        import httpx
        from app.api.user_pages import register_user_pages
        from app.db.models import BotUser

        staff = {
            "role": "admin", "username": "root", "org_principal_id": 1,
            "org_depth": 0, "org_parent_id": None, "org_status": "active",
        }

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_user_pages(app, render=lambda *a, **k: None, require_admin=lambda: staff, get_db=get_db)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            for note in ("", "  ", "ab"):
                with self.subTest(note=note):
                    resp = await client.post(
                        "/users/1/wallet-credit",
                        data={"amount": "5000", "mode": "credit", "note": note, "nonce": secrets.token_hex(8)},
                    )
                    self.assertEqual(resp.status_code, 303)
                    self.assertIn("err=", resp.headers["location"])
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            self.assertEqual(int(user.wallet_balance or 0), 10_000)

    async def test_web_platform_wallet_schedules_notice_with_reason_and_balance(self):
        from fastapi import FastAPI
        import httpx
        from app.api.user_pages import register_user_pages
        from app.db.models import BotUser
        from app.services.formatting import format_toman

        staff = {
            "role": "admin", "username": "root", "org_principal_id": 1,
            "org_depth": 0, "org_parent_id": None, "org_status": "active",
        }

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_user_pages(app, render=lambda *a, **k: None, require_admin=lambda: staff, get_db=get_db)
        with patch("app.services.manual_adjust_notices.schedule_customer_notice") as schedule:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/users/1/wallet-credit",
                    data={
                        "amount": "5000",
                        "mode": "credit",
                        "note": "جبران قطعی شبکه",
                        "nonce": secrets.token_hex(8),
                    },
                )
            self.assertEqual(resp.status_code, 303)
            self.assertIn("ok=", resp.headers["location"])
            schedule.assert_called_once()
            args, kwargs = schedule.call_args
            self.assertEqual(args[0], 1)
            self.assertIn("جبران قطعی شبکه", args[1])
            self.assertIn(format_toman(15_000), args[1])
            self.assertEqual(kwargs.get("shop_id"), None)
            self.assertFalse(kwargs.get("replayed", False))
        async with self.Session() as session:
            self.assertEqual(int((await session.get(BotUser, 1)).wallet_balance or 0), 15_000)

    async def test_bot_reason_step_rejects_short_reason(self):
        from app.bot.handlers.admin_wallet_adjust import adm_users_wallet_reason_save

        message = MagicMock()
        message.text = "ab"
        message.answer = AsyncMock()
        state = AsyncMock()
        state.get_data = AsyncMock(
            return_value={"admin_credit_user_id": 1, "admin_wallet_mode": "credit", "admin_wallet_amount": 5000}
        )
        db_user = MagicMock(telegram_id=99, role="admin")
        session = AsyncMock()
        with (
            patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=True)),
            patch("app.bot.handlers.admin_wallet_adjust._is_admin", return_value=True),
            patch("app.services.bot_user_admin.admin_adjust_user_wallet", AsyncMock()) as adjust,
        ):
            await adm_users_wallet_reason_save(message, state, session=session, db_user=db_user)
        adjust.assert_not_awaited()
        message.answer.assert_awaited()
        self.assertIn("علت", message.answer.await_args.args[0])

    async def test_template_requires_reason_labeled_علت(self):
        edit = (ROOT / "app/web/templates/_user_edit_body.html").read_text(encoding="utf-8")
        platform = edit.split('id="user-wallet"', 1)[1].split("{% endif %}", 1)[0]
        self.assertIn("note_required=true", platform)
        self.assertIn("note_label='علت'", platform)


class ShopAdjustNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401
        from app.db.models import BotUser, ResellerProfile

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "shop-notice.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            await session.flush()
            session.add(ResellerProfile(user_id=SHOP_A, is_active=True))
            session.add(ResellerProfile(user_id=SHOP_B, is_active=True))
            session.add(BotUser(id=101, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A))
            session.add(BotUser(id=102, telegram_id=1020, referral_code="c102", reseller_id=SHOP_B))
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    def _staff(self, shop_id: int, perms: list[str] | None = None) -> dict:
        return {
            "role": "reseller",
            "bot_user_id": shop_id,
            "id": shop_id,
            "username": f"shop{shop_id}",
            "permissions": perms or ["wallet_adjust", "loyalty_adjust"],
        }

    async def test_shop_wallet_notice_uses_shop_id_and_skips_replay(self):
        from app.services.shop_manual_adjust import new_request_key
        from app.services.shop_wallet_adjust import adjust_shop_wallet, parse_request

        key = new_request_key()
        request = parse_request(
            customer_id=101, raw_amount="5000", direction="credit", reason="refund outage", request_key=key
        )
        with patch("app.services.manual_adjust_notices.schedule_customer_notice") as schedule:
            async with self.Session() as session:
                first = await adjust_shop_wallet(session, staff=self._staff(SHOP_A), request=request)
            async with self.Session() as session:
                again = await adjust_shop_wallet(session, staff=self._staff(SHOP_A), request=request)
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        from app.services.formatting import format_toman

        schedule.assert_called_once()
        _args, kwargs = schedule.call_args
        self.assertEqual(kwargs.get("shop_id"), SHOP_A)
        self.assertIn("refund outage", _args[1])
        self.assertIn(format_toman(5_000), _args[1])
        self.assertIn(format_toman(first.balance_after), _args[1])

    async def test_shop_points_notice_uses_shop_bot_path(self):
        from app.services.shop_manual_adjust import new_request_key
        from app.services.shop_points_adjust import adjust_shop_points, parse_points_request

        request = parse_points_request(
            raw_customer_id="101",
            raw_points="40",
            direction="credit",
            reason="goodwill gift",
            request_key=new_request_key(),
        )
        with patch("app.services.manual_adjust_notices.schedule_customer_notice") as schedule:
            async with self.Session() as session:
                change = await adjust_shop_points(session, staff=self._staff(SHOP_A), request=request)
        self.assertFalse(change.replayed)
        schedule.assert_called_once()
        args, kwargs = schedule.call_args
        self.assertEqual(args[0], 101)
        self.assertEqual(kwargs.get("shop_id"), SHOP_A)
        self.assertIn("goodwill gift", args[1])
        self.assertIn("40", args[1])


class PlatformPointsNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from app.db import Base
        import app.db.models  # noqa: F401
        from app.db.models import BotUser, Role

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "plat-points.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add(
                BotUser(
                    id=1, telegram_id=101, referral_code="u1", role=Role.USER.value, points_balance=10
                )
            )
            await session.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def test_admin_points_schedules_once_and_marks_replay(self):
        from app.db.models import BotUser
        from app.services.loyalty import admin_adjust_points
        from app.services.manual_adjust_notices import schedule_points_notice

        key = secrets.token_hex(16)
        with patch("app.services.manual_adjust_notices.schedule_customer_notice") as schedule:
            async with self.Session() as session:
                user = await session.get(BotUser, 1)
                first = await admin_adjust_points(
                    session, user, 5, reason="جبران", admin_identity="root", shop_id=None, request_key=key
                )
                schedule_points_notice(
                    user_id=1,
                    mode="credit",
                    points=5,
                    balance=int(first.tx.balance_after),
                    note="جبران",
                    shop_id=None,
                    replayed=first.replayed,
                )
            async with self.Session() as session:
                user = await session.get(BotUser, 1)
                again = await admin_adjust_points(
                    session, user, 5, reason="جبران", admin_identity="root", shop_id=None, request_key=key
                )
                schedule_points_notice(
                    user_id=1,
                    mode="credit",
                    points=5,
                    balance=int(again.tx.balance_after),
                    note="جبران",
                    shop_id=None,
                    replayed=again.replayed,
                )
        self.assertFalse(first.replayed)
        self.assertTrue(again.replayed)
        # The mock replaces the early-return; only non-replayed kwargs count as a real notify.
        sent = [c for c in schedule.call_args_list if not c.kwargs.get("replayed")]
        self.assertEqual(len(sent), 1)
        self.assertIn("جبران", sent[0].args[1])
        self.assertEqual(await self._balance(), 15)

    async def _balance(self) -> int:
        from app.db.models import BotUser

        async with self.Session() as session:
            return int((await session.get(BotUser, 1)).points_balance or 0)

    async def test_send_uses_open_notify_bot_for_user_with_shop_id(self):
        from app.services.manual_adjust_notices import _send_customer_notice

        bot = MagicMock()
        bot.send_message = AsyncMock()
        bot.session = MagicMock()
        bot.session.close = AsyncMock()
        with (
            patch("app.db.session.SessionLocal") as session_local,
            patch(
                "app.services.reseller_bots.open_notify_bot_for_user",
                AsyncMock(return_value=(bot, True)),
            ) as opener,
        ):
            session = AsyncMock()
            user = MagicMock(id=101, telegram_id=1010)
            session.get = AsyncMock(return_value=user)
            session_local.return_value.__aenter__ = AsyncMock(return_value=session)
            session_local.return_value.__aexit__ = AsyncMock(return_value=None)
            await _send_customer_notice(101, "<b>hi</b>", shop_id=SHOP_A)
        opener.assert_awaited_once()
        self.assertEqual(opener.await_args.kwargs.get("shop_id"), SHOP_A)
        bot.send_message.assert_awaited_once()
        self.assertEqual(bot.send_message.await_args.args[0], 1010)


class FormatTomanInNoticeTests(unittest.TestCase):
    def test_wallet_notice_includes_formatted_balance(self):
        from app.services.formatting import format_toman
        from app.services.manual_adjust_notices import wallet_change_notice

        text = wallet_change_notice(mode="credit", amount=5000, balance=15000, note="ok reason")
        self.assertIn(format_toman(5000), text)
        self.assertIn(format_toman(15000), text)


if __name__ == "__main__":
    unittest.main()
