"""Several API payment gateways per shop: choice, per-gateway credentials, verify on the same merchant."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.bot.handlers.psp_check import psp_gateway_picker
from app.bot.handlers.shop import _psp_callback_target
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
from app.services.payment_providers.base import CheckoutResult, VerifyResult
from app.services.payment_settlement import complete_psp_return, create_psp_checkout
from app.services.psp_gateways import (
    MAX_PSP_GATEWAYS,
    all_psp_gateways,
    choose_psp_gateway,
    gateway_choices,
    merge_submitted_psp_gateways,
    parse_psp_gateways,
    psp_gateways_form_json,
)
from app.services.users import (
    clear_settings_cache,
    get_all_settings,
    get_setting,
    set_setting,
    set_settings_bulk,
)
from app.services.wallet_topup import TopupError, start_topup, topup_options

ROOT = Path(__file__).resolve().parents[1]
AMOUNT = 70_000
SHOP_A = 9101
SHOP_B = 9102


def gateway(gid: str, merchant: str = "", **extra) -> dict:
    return {"id": gid, "name": f"درگاه {gid}", "provider": "zarinpal", "merchant_id": merchant, **extra}


def gateways_json(*rows: dict) -> str:
    return json.dumps(list(rows), ensure_ascii=False)


class FakeAdapter:
    """Records the credential each call used; verify succeeds."""

    def __init__(self, calls: list[tuple[str, str]], merchant: str) -> None:
        self.calls = calls
        self.merchant = merchant

    async def create_checkout(self, req) -> CheckoutResult:
        self.calls.append(("checkout", self.merchant))
        return CheckoutResult(external_ref=f"A{req.settlement_id}", checkout_url="https://pay.test/start")

    async def verify(self, *, external_ref, amount, callback_params, expected_invoice_id=None) -> VerifyResult:
        self.calls.append(("verify", self.merchant))
        return VerifyResult(ok=True, external_ref=external_ref, amount=amount)


class ParseAndFormTests(unittest.TestCase):
    def test_no_saved_list_uses_the_older_single_gateway_settings(self) -> None:
        ui = {"psp_provider": "sunpay", "psp_merchant_id": "key-1", "psp_pay_url": "https://pay.test"}
        [only] = all_psp_gateways(ui)
        self.assertEqual((only.id, only.provider, only.merchant_id), ("default", "sunpay", "key-1"))

    def test_hostile_rows_are_normalised(self) -> None:
        rows = [gateway("a", provider="evil"), gateway("a"), gateway("bad id!"), "x", *[gateway(f"g{i}") for i in range(20)]]
        parsed = parse_psp_gateways(json.dumps(rows))
        self.assertEqual(parsed[0].provider, "zarinpal")
        self.assertEqual(len({g.id for g in parsed}), len(parsed))
        self.assertNotIn("bad id!", {g.id for g in parsed})
        self.assertEqual(len(parsed), MAX_PSP_GATEWAYS)
        self.assertEqual(parse_psp_gateways("not json"), [])

    def test_form_json_never_contains_a_credential(self) -> None:
        ui = {"psp_gateways": gateways_json(gateway("a", "secret-merchant-a"), gateway("b"))}
        form = json.loads(psp_gateways_form_json(ui))
        self.assertNotIn("secret-merchant-a", psp_gateways_form_json(ui))
        self.assertEqual([(r["id"], r["has_secret"]) for r in form], [("a", True), ("b", False)])

    def test_blank_credential_keeps_the_saved_one_and_new_one_replaces_it(self) -> None:
        ui = {"psp_gateways": gateways_json(gateway("a", "old-a"), gateway("b", "old-b"))}
        merged = json.loads(
            merge_submitted_psp_gateways(gateways_json(gateway("a"), gateway("b", "new-b"), gateway("c")), ui)
        )
        self.assertEqual([r["merchant_id"] for r in merged], ["old-a", "new-b", ""])

    def test_new_checkouts_only_use_an_enabled_gateway(self) -> None:
        ui = {"psp_gateways": gateways_json(gateway("a", enabled=False), gateway("b"), gateway("c"))}
        self.assertEqual(choose_psp_gateway(ui, None).id, "b")
        self.assertEqual(choose_psp_gateway(ui, "c").id, "c")
        for bad in ("a", "zzz"):
            with self.assertRaises(ValueError):
                choose_psp_gateway(ui, bad)
        self.assertEqual([g.id for g in gateway_choices(ui)], ["b", "c"])
        single = {"psp_gateways": gateways_json(gateway("a"), gateway("b", enabled=False))}
        self.assertEqual(gateway_choices(single), [])

    def test_settings_page_field_does_not_render_a_saved_credential(self) -> None:
        from app.api.app import templates

        values = {"psp_gateways": gateways_json(gateway("a", "secret-merchant-a"))}
        html = templates.env.get_template("_settings_field.html").render(
            field=("psp_gateways", "درگاه‌ها", "psp_gateways", ""), values=values
        )
        self.assertIn('data-pay-dest="psp"', html)
        self.assertNotIn("secret-merchant-a", html)
        self.assertIn("has_secret", html)

    def test_picker_buttons_fit_telegram_callback_limit_and_parse_back(self) -> None:
        gateways = parse_psp_gateways(gateways_json(gateway("x" * 32), gateway("b")))
        markup = psp_gateway_picker(gateways, prefix="pay", order_id=987654321, extra_rows=[])
        data = [row[0].callback_data for row in markup.inline_keyboard]
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))
        self.assertEqual(_psp_callback_target(data[1]), (987654321, "b"))
        self.assertEqual(_psp_callback_target("pay:psp:12"), (12, None))


class GatewayCheckoutTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 't.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        self.calls: list[tuple[str, str]] = []
        async with self.Session() as session:
            await set_setting(session, "pay_psp_enabled", "1")
            await set_setting(session, "psp_merchant_id", "legacy-merchant")
            await set_setting(
                session, "psp_gateways", gateways_json(gateway("a", "merchant-a"), gateway("b", "merchant-b"))
            )
            user = BotUser(telegram_id=7001, username="u", full_name="u", referral_code="r71", role=Role.USER.value)
            session.add(user)
            await session.commit()
            self.user_id = int(user.id)
        clear_settings_cache()
        self.patches = [
            patch("app.services.payment_settlement.get_psp_adapter", side_effect=self._adapter),
            patch("app.services.payment_settlement._public_base", return_value="https://bot.test"),
            patch("app.services.payment_settlement.approve_payment", AsyncMock(return_value=None)),
            patch("app.services.payment_settlement.notify_after_settlement", AsyncMock()),
        ]
        for p in self.patches:
            p.start()

    async def asyncTearDown(self) -> None:
        for p in self.patches:
            p.stop()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()

    def _adapter(self, provider: str, *, ui: dict, public_base_url: str) -> FakeAdapter:
        return FakeAdapter(self.calls, str(ui.get("psp_merchant_id") or ""))

    async def _payment(self, session: AsyncSession) -> Payment:
        payment = Payment(
            user_id=self.user_id,
            amount=AMOUNT,
            method=PaymentMethod.PSP.value,
            status=PaymentStatus.PENDING.value,
            is_wallet_topup=True,
        )
        session.add(payment)
        await session.commit()
        return payment

    async def _verify(self, session: AsyncSession, settlement: PaymentSettlement) -> PaymentSettlement:
        return await complete_psp_return(
            session, provider="zarinpal", settlement_id=int(settlement.id), callback_params={"Status": "OK"}
        )

    async def test_checkout_and_verify_use_the_chosen_gateways_merchant(self) -> None:
        async with self.Session() as session:
            settlement = await create_psp_checkout(session, await self._payment(session), gateway_id="b")
            self.assertEqual(settlement.gateway_id, "b")
            # Reordering the list afterwards must not move the verify to another merchant.
            await set_settings_bulk(session, {"psp_gateways": gateways_json(gateway("b"), gateway("a"))})
            settled = await self._verify(session, settlement)
        self.assertEqual(settled.status, SettlementStatus.SETTLED.value)
        self.assertEqual(self.calls, [("checkout", "merchant-b"), ("verify", "merchant-b")])

    async def test_open_checkout_is_kept_when_another_gateway_is_picked(self) -> None:
        async with self.Session() as session:
            payment = await self._payment(session)
            first = await create_psp_checkout(session, payment, gateway_id="a")
            again = await create_psp_checkout(session, payment, gateway_id="b")
        self.assertEqual((again.id, again.gateway_id, again.checkout_url), (first.id, "a", first.checkout_url))
        self.assertEqual(self.calls, [("checkout", "merchant-a")])

    async def test_removed_gateway_is_never_verified_on_another_merchant(self) -> None:
        async with self.Session() as session:
            settlement = await create_psp_checkout(session, await self._payment(session), gateway_id="a")
            await set_settings_bulk(session, {"psp_gateways": gateways_json(gateway("b"))})
            with self.assertRaises(ValueError):
                await self._verify(session, settlement)
            row = await session.get(PaymentSettlement, int(settlement.id))
            await session.refresh(row)
        self.assertEqual(row.status, SettlementStatus.AWAITING.value)
        self.assertNotIn(("verify", "merchant-b"), self.calls)

    async def test_checkout_from_before_gateway_ids_verifies_on_the_older_settings(self) -> None:
        async with self.Session() as session:
            settlement = await create_psp_checkout(session, await self._payment(session), gateway_id="a")
            settlement.gateway_id = None
            await session.commit()
            await self._verify(session, settlement)
        self.assertEqual(self.calls[-1], ("verify", "legacy-merchant"))

    async def test_disabled_gateway_refuses_new_checkouts(self) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, {"psp_gateways": gateways_json(gateway("a", enabled=False), gateway("b"))})
            with self.assertRaisesRegex(ValueError, "فعال نیست"):
                await create_psp_checkout(session, await self._payment(session), gateway_id="a")
        self.assertEqual(self.calls, [])

    async def test_saving_without_a_key_keeps_each_shops_own_credential(self) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, {"psp_gateways": gateways_json(gateway("g", "shop-a-key"))}, reseller_id=SHOP_A)
            await set_settings_bulk(session, {"psp_gateways": gateways_json(gateway("g"))}, reseller_id=SHOP_B)
            await set_settings_bulk(session, {"psp_gateways": gateways_json(gateway("g"))}, reseller_id=SHOP_A)
            stored_a = await get_setting(session, "psp_gateways", reseller_id=SHOP_A)
            stored_b = await get_setting(session, "psp_gateways", reseller_id=SHOP_B)
        self.assertEqual(json.loads(stored_a)[0]["merchant_id"], "shop-a-key")
        self.assertEqual(json.loads(stored_b)[0]["merchant_id"], "")

    async def test_mini_app_top_up_offers_the_gateways_and_uses_the_chosen_one(self) -> None:
        async with self.Session() as session:
            ui = await get_all_settings(session)
            psp = next(m for m in topup_options(ui)["methods"] if m["key"] == "psp")
            self.assertEqual([c["id"] for c in psp["choices"]], ["a", "b"])
            with self.assertRaises(TopupError):
                await start_topup(
                    session, user_id=self.user_id, shop_id=None, amount=AMOUNT, method="psp", dest_id="zzz", ui=ui
                )
            start = await start_topup(
                session, user_id=self.user_id, shop_id=None, amount=AMOUNT, method="psp", dest_id="b", ui=ui
            )
            settlement = await session.get(PaymentSettlement, start.settlement_id)
        self.assertEqual(settlement.gateway_id, "b")
        self.assertEqual(self.calls, [("checkout", "merchant-b")])


class SettlementGatewayMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_rows(self) -> None:
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        path = ROOT / "alembic/versions/0071_settlement_psp_gateway.py"
        spec = importlib.util.spec_from_file_location("m0071", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE payment_settlements (id INTEGER PRIMARY KEY, provider VARCHAR(32))"))
            conn.execute(text("INSERT INTO payment_settlements VALUES (1, 'zarinpal'), (2, 'sunpay')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("payment_settlements")}
                self.assertIn("gateway_id", columns)
                self.assertEqual(
                    conn.execute(text("SELECT COUNT(*) FROM payment_settlements WHERE gateway_id IS NULL")).scalar(), 2
                )
                migration.downgrade()
                migration.downgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("payment_settlements")}
                self.assertNotIn("gateway_id", columns)
                self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM payment_settlements")).scalar(), 2)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
