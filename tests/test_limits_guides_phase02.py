"""Phase 0+2: gift per-user, trial contact, receipt dup, guides, limits IA, doctor."""

from __future__ import annotations

import hashlib
import hmac
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("WEB_SECRET", "test-web-secret-for-limits-guides-32ch")


class LimitsGuidesPhase02Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        from app.db.models import Base
        import app.db.models  # noqa: F401

        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "limits.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)

    async def asyncTearDown(self):
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _user(self, session, tg: int):
        from app.db.models import BotUser, Role

        u = BotUser(telegram_id=tg, referral_code=f"U{tg}", role=Role.USER.value)
        session.add(u)
        await session.commit()
        await session.refresh(u)
        return u

    async def test_redeem_charge_code_per_user_once(self):
        from app.db.models import ChargeCode, ChargeCodeUse
        from app.services.ux20 import create_charge_code, redeem_charge_code
        from sqlalchemy import select

        async with self.Session() as session:
            user = await self._user(session, 501)
            other = await self._user(session, 502)
            code = await create_charge_code(session, amount=1000, max_uses=5)
            code_id = int(code.id)
            code_str = code.code
            with patch("app.services.users.current_shop_reseller_id", return_value=None):
                _row, _bal = await redeem_charge_code(session, user=user, code=code_str)
                await session.commit()
                refreshed = await session.get(ChargeCode, code_id)
                self.assertEqual(int(refreshed.used_count), 1)
                with self.assertRaises(ValueError) as ctx:
                    await redeem_charge_code(session, user=user, code=code_str)
                self.assertIn("قبلاً", str(ctx.exception))
                await redeem_charge_code(session, user=other, code=code_str)
                await session.commit()
            n = (
                await session.execute(
                    select(ChargeCodeUse).where(
                        ChargeCodeUse.charge_code_id == code_id,
                        ChargeCodeUse.status == "consumed",
                    )
                )
            ).scalars().all()
            self.assertEqual(len(n), 2)

    def test_iran_phone_and_hmac(self):
        from app.services.trial_contact import (
            canonical_iran_phone,
            hash_trial_phone,
            is_iran_mobile,
            phone_hmac_sha256,
        )

        self.assertTrue(is_iran_mobile("09121234567"))
        self.assertTrue(is_iran_mobile("989121234567"))
        self.assertFalse(is_iran_mobile("12345"))
        self.assertEqual(canonical_iran_phone("09121234567"), "989121234567")
        secret = "test-secret-xxxxxxxx"
        h1 = hash_trial_phone("09121234567", secret=secret, require_iran=True)
        h2 = phone_hmac_sha256("989121234567", secret=secret)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)
        self.assertNotIn("0912", h1)

    async def test_trial_claim_phone_unique(self):
        from app.db.models import Plan
        from app.services.orders import create_order
        from app.services.users import set_setting

        async with self.Session() as session:
            user_a = await self._user(session, 601)
            user_b = await self._user(session, 602)
            plan = Plan(
                name="trial",
                price=0,
                duration_days=1,
                data_limit_gb=1,
                is_active=True,
                is_trial=True,
            )
            session.add(plan)
            await session.commit()
            await set_setting(session, "trial_require_contact", "1")
            phone_hash = hmac.new(b"x", b"989121234567", hashlib.sha256).hexdigest()
            with patch("app.services.users.current_shop_reseller_id", return_value=None):
                await create_order(
                    session,
                    user_id=user_a.id,
                    plan_id=plan.id,
                    trial_phone_hash=phone_hash,
                    trial_telegram_id=int(user_a.telegram_id),
                )
                with self.assertRaises(ValueError) as ctx:
                    await create_order(
                        session,
                        user_id=user_b.id,
                        plan_id=plan.id,
                        trial_phone_hash=phone_hash,
                        trial_telegram_id=int(user_b.telegram_id),
                    )
                self.assertIn("قبلاً", str(ctx.exception))

    def test_connection_guides_parse(self):
        from app.services.connection_guides import (
            default_connection_guides,
            guides_for_audience,
            parse_connection_guides,
            serialize_connection_guides,
        )

        raw = serialize_connection_guides(default_connection_guides())
        items = parse_connection_guides(raw)
        users = guides_for_audience(items, "user")
        resellers = guides_for_audience(items, "reseller")
        self.assertTrue(users)
        self.assertTrue(resellers)
        self.assertTrue(all(g["audience"] == "user" for g in users))
        self.assertTrue(all(g["audience"] == "reseller" for g in resellers))

    def test_receipt_dup_policy(self):
        from app.db.models import PaymentReceiptFingerprint
        from app.services.receipt_fingerprints import format_dup_warning, normalize_dup_policy

        self.assertEqual(normalize_dup_policy("BLOCK"), "block")
        self.assertEqual(normalize_dup_policy("anything"), "warn")
        fp = PaymentReceiptFingerprint(payment_id=7, file_unique_id="x", sha256="a" * 64)
        self.assertIn("#7", format_dup_warning([fp]))

    def test_limits_guides_tabs(self):
        from app.services.resellers import RESELLER_SETTINGS_TABS
        from app.services.users import SETTINGS_TABS, TAB_SETTING_GROUPS, keys_for_tab

        owner_tabs = {t[0] for t in SETTINGS_TABS}
        shop_tabs = {t[0] for t in RESELLER_SETTINGS_TABS}
        self.assertIn("limits", owner_tabs)
        self.assertIn("guides", owner_tabs)
        self.assertIn("limits", shop_tabs)
        self.assertIn("guides", shop_tabs)
        self.assertNotIn("forcejoin", owner_tabs)
        self.assertIn("referral_required", keys_for_tab("limits"))
        self.assertIn("trial_require_contact", keys_for_tab("limits"))
        self.assertIn("connection_guides", keys_for_tab("guides"))
        self.assertNotIn("معرف اجباری", TAB_SETTING_GROUPS.get("payment") or [])

    def test_doctor_cli_accepts_json(self):
        from app.cli.main import build_parser

        p = build_parser()
        args = p.parse_args(["doctor", "--json"])
        self.assertTrue(getattr(args, "doctor_json", False) or getattr(args, "json", False))


if __name__ == "__main__":
    unittest.main()
