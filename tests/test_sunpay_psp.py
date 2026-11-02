"""SunPay PSP — request/verify contract, soft-fail return, HTTPS pay_url, invoice match."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

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
from app.services.payment_providers import get_psp_adapter
from app.services.payment_providers.base import CheckoutRequest
from app.services.payment_providers import sunpay as sunpay_mod
from app.services.payment_providers.sunpay import (
    CUSTOMER_NOT_PAID,
    CUSTOMER_REQUEST_FAILED,
    SunPayAdapter,
    SunPayGatewayError,
    close_client,
    ensure_sunpay_pay_host_public,
    normalize_request_method,
    validate_sunpay_pay_url,
)
from app.services import public_https as pub
from app.services.payment_settlement import (
    PSP_POLL_WINDOW,
    callback_indicates_user_abort,
    callback_invoice_matches_settlement,
    complete_psp_return,
    create_psp_checkout,
    deliver_psp_once,
    poll_psp_settlements,
)
from app.services.users import clear_settings_cache, set_setting

PAY_URL = "https://pay.sunpay.test"
API_KEY = "sunpay-test-key-abcdef"
TOKEN = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _patch_public_dns(*addrs: str):
    # Use a globally routed address — TEST-NET (203.0.113/24) is is_private on Py3.12+.
    return patch.object(
        sunpay_mod, "_resolve_host_addresses", return_value=list(addrs) or ["8.8.8.8"]
    )


def _patch_sunpay_http(handler):
    """HTTP mock + public DNS so outbound host checks pass in unit tests."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    http_patch = patch.object(sunpay_mod, "_http", return_value=client)
    dns_patch = _patch_public_dns()
    return http_patch, dns_patch, client


def _patch_https_public_base(base: str = "https://bot.test"):
    return patch.object(pub, "https_public_base", return_value=base)


def _capture_sunpay_warnings():
    """Patch ``logger.warning`` — more reliable than ``assertLogs`` in large suites."""
    records: list[str] = []

    def _warning(msg: object, *args: object, **_kwargs: object) -> None:
        text = str(msg)
        if args:
            try:
                text = text % args
            except (TypeError, ValueError):
                text = f"{text} {' '.join(str(a) for a in args)}"
        records.append(text)

    return records, patch.object(sunpay_mod.logger, "warning", side_effect=_warning)


def _checkout_req(*, settlement_id: int = 42, amount: int = 100_000) -> CheckoutRequest:
    return CheckoutRequest(
        settlement_id=settlement_id,
        payment_id=7,
        amount=amount,
        currency="IRT",
        description="test",
        callback_url="https://bot.test/payments/settlement/psp/sunpay/return/42",
        return_url="https://bot.test/payments/settlement/psp/sunpay/return/42",
        metadata={},
    )


class SunPayUrlValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_https_public_ok(self) -> None:
        self.assertEqual(
            validate_sunpay_pay_url("https://pay.example.com/"),
            "https://pay.example.com",
        )

    def test_http_and_private_rejected(self) -> None:
        for bad in (
            "http://pay.example.com",
            "https://localhost/pay",
            "https://127.0.0.1",
            "https://10.0.0.5",
            "https://192.168.1.1",
            "https://169.254.169.254",
            "https://0.0.0.0",
            "https://" + "[::1]",
            "",
            "ftp://pay.example.com",
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_sunpay_pay_url(bad)

    def test_adapter_rejects_http_pay_url_on_use(self) -> None:
        adapter = SunPayAdapter(api_key=API_KEY, pay_url="http://pay.example.com")
        with self.assertRaises(ValueError):
            adapter._require_config()

    async def test_dns_private_resolution_rejected(self) -> None:
        with _patch_public_dns("10.1.2.3"):
            with self.assertRaises(ValueError):
                await ensure_sunpay_pay_host_public("evil.example.com")

    async def test_dns_public_resolution_ok(self) -> None:
        with _patch_public_dns("1.1.1.1"):
            await ensure_sunpay_pay_host_public("pay.example.com")


class SunPayClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await close_client()

    async def test_request_sends_documented_get_json_body(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"success": 1, "data": {"token": TOKEN}})

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            result = await adapter.create_checkout(_checkout_req())
        await client.aclose()
        self.assertEqual(result.external_ref, TOKEN)
        self.assertEqual(result.checkout_url, f"{PAY_URL}/pay/{TOKEN}")
        sent = seen[0]
        self.assertEqual(sent.method, "GET")
        self.assertEqual(sent.url.path, "/api/request")
        body = json.loads(sent.content)
        self.assertEqual(body["api_key"], API_KEY)
        self.assertEqual(body["amount"], 100_000)
        self.assertEqual(body["invoice_id"], 42)

    async def test_request_rejects_amount_below_minimum_and_bool(self) -> None:
        adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
        with _patch_public_dns():
            with self.assertRaises(ValueError):
                await adapter.create_checkout(_checkout_req(amount=999))
            with self.assertRaises(ValueError):
                await adapter.create_checkout(_checkout_req(amount=True))  # type: ignore[arg-type]

    async def test_request_fail_closed_without_credentials(self) -> None:
        with _patch_public_dns():
            with self.assertRaises(ValueError):
                await SunPayAdapter(api_key="", pay_url=PAY_URL).create_checkout(
                    _checkout_req()
                )
            with self.assertRaises(ValueError):
                await SunPayAdapter(api_key=API_KEY, pay_url="").create_checkout(
                    _checkout_req()
                )

    async def test_request_maps_api_error_without_raw_message(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": 0,
                    "error": {"code": "BAD_KEY", "message": "<b>secret</b>"},
                },
            )

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            with self.assertRaises(SunPayGatewayError) as caught:
                await adapter.create_checkout(_checkout_req())
        await client.aclose()
        self.assertEqual(str(caught.exception), CUSTOMER_REQUEST_FAILED)
        self.assertIn("BAD_KEY", caught.exception.admin_detail)
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("<b>", caught.exception.admin_detail)

    async def test_request_post_method_when_configured(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.method)
            return httpx.Response(200, json={"success": 1, "data": {"token": TOKEN}})

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(
                api_key=API_KEY, pay_url=PAY_URL, request_method="POST"
            )
            await adapter.create_checkout(_checkout_req())
        await client.aclose()
        self.assertEqual(seen, ["POST"])
        self.assertEqual(normalize_request_method("post"), "POST")
        self.assertEqual(normalize_request_method("nope"), "GET")

    async def test_success_zero_without_error_code_logs_body_not_api_key(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"success": 0, "message": "gateway busy", "api_key": "LEAK"},
            )

        records, warn_p = _capture_sunpay_warnings()
        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p, warn_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            with self.assertRaises(SunPayGatewayError) as caught:
                await adapter.create_checkout(_checkout_req())
        await client.aclose()
        self.assertEqual(str(caught.exception), CUSTOMER_REQUEST_FAILED)
        joined = "\n".join(records)
        self.assertTrue(records, "expected sunpay warning log")
        self.assertIn("status=200", joined)
        self.assertIn("gateway busy", joined)
        self.assertNotIn(API_KEY, joined)
        self.assertNotIn("LEAK", joined)

    async def test_http_405_json_and_non_json_are_logged_distinctly(self) -> None:
        async def run(body, *, json_mode: bool) -> str:
            def handler(_request: httpx.Request) -> httpx.Response:
                if json_mode:
                    return httpx.Response(405, json=body)
                return httpx.Response(422, text=str(body))

            records, warn_p = _capture_sunpay_warnings()
            http_p, dns_p, client = _patch_sunpay_http(handler)
            with http_p, dns_p, warn_p:
                adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
                with self.assertRaises(SunPayGatewayError):
                    await adapter.create_checkout(_checkout_req())
            await client.aclose()
            self.assertTrue(records, "expected sunpay warning log")
            return "\n".join(records)

        out_json = await run({"error": "method not allowed"}, json_mode=True)
        self.assertIn("status=405", out_json)
        self.assertIn("method not allowed", out_json)
        out_text = await run("plain failure", json_mode=False)
        self.assertIn("status=422", out_text)
        self.assertIn("non-json", out_text)

    async def test_error_as_string_is_logged_safely(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json={"success": 0, "error": "<script>api_key=steal</script>"}
            )

        records, warn_p = _capture_sunpay_warnings()
        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p, warn_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            with self.assertRaises(SunPayGatewayError) as caught:
                await adapter.create_checkout(_checkout_req())
        await client.aclose()
        self.assertEqual(str(caught.exception), CUSTOMER_REQUEST_FAILED)
        self.assertIn("api_key=<redacted>", "\n".join(records))

    async def test_own_api_key_value_is_scrubbed_even_without_field_name(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": 0,
                    "message": f"rejected key {API_KEY} please rotate",
                },
            )

        records, warn_p = _capture_sunpay_warnings()
        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p, warn_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            with self.assertRaises(SunPayGatewayError):
                await adapter.create_checkout(_checkout_req())
        await client.aclose()
        joined = "\n".join(records)
        self.assertTrue(records, "expected sunpay warning log")
        self.assertNotIn(API_KEY, joined)
        self.assertIn("<redacted>", joined)

    async def test_verify_posts_token_and_matches_amount_and_invoice(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "data": {
                        "amount": 50_000,
                        "invoice_id": 9,
                        "reference": "REF-1",
                        "paid_at": "2026-09-27 09:30:00",
                        "detail": {},
                    },
                },
            )

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            verified = await adapter.verify(
                external_ref=TOKEN,
                amount=50_000,
                callback_params={"success": "1", "invoice_id": "9", "token": TOKEN},
                expected_invoice_id=9,
            )
        await client.aclose()
        self.assertTrue(verified.ok)
        self.assertEqual(verified.amount, 50_000)
        self.assertEqual(json.loads(seen[0].content), {"api_key": API_KEY, "token": TOKEN})

    async def test_verify_rejects_invoice_mismatch_against_settlement_id(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "data": {
                        "amount": 50_000,
                        "invoice_id": 99,
                        "reference": "X",
                        "paid_at": "2026-09-27 09:30:00",
                        "detail": {},
                    },
                },
            )

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            # Attacker-controlled callback invoice matches settlement, but gateway says 99.
            verified = await adapter.verify(
                external_ref=TOKEN,
                amount=50_000,
                callback_params={"success": 1, "token": TOKEN, "invoice_id": "5"},
                expected_invoice_id=5,
            )
        await client.aclose()
        self.assertFalse(verified.ok)
        self.assertIn("فاکتور", verified.message)

    async def test_verify_soft_abort_without_calling_api(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500, text="nope")

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            verified = await adapter.verify(
                external_ref=TOKEN,
                amount=50_000,
                callback_params={"success": "0", "token": TOKEN, "invoice_id": "1"},
            )
        await client.aclose()
        self.assertFalse(verified.ok)
        self.assertEqual(calls["n"], 0)

    async def test_verify_amount_mismatch(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "data": {
                        "amount": 40_000,
                        "invoice_id": 1,
                        "reference": "X",
                        "paid_at": "2026-09-27 09:30:00",
                        "detail": {},
                    },
                },
            )

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            verified = await adapter.verify(
                external_ref=TOKEN,
                amount=50_000,
                callback_params={"success": 1, "token": TOKEN},
                expected_invoice_id=1,
            )
        await client.aclose()
        self.assertFalse(verified.ok)
        self.assertEqual(verified.amount, 40_000)

    async def _verify_with_response(self, body: dict) -> object:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with http_p, dns_p:
            adapter = SunPayAdapter(api_key=API_KEY, pay_url=PAY_URL)
            verified = await adapter.verify(
                external_ref=TOKEN,
                amount=50_000,
                callback_params={"success": "1", "token": TOKEN},
                expected_invoice_id=1,
            )
        await client.aclose()
        return verified

    async def test_lookup_without_reference_is_not_a_payment(self) -> None:
        for reference in (None, "", "   "):
            with self.subTest(reference=reference):
                verified = await self._verify_with_response(
                    {
                        "success": 1,
                        "data": {
                            "amount": 50_000,
                            "invoice_id": 1,
                            "reference": reference,
                            "paid_at": "2026-09-27 09:30:00",
                        },
                    }
                )
                self.assertFalse(verified.ok)
                self.assertEqual(verified.message, CUSTOMER_NOT_PAID)

    async def test_lookup_with_empty_paid_at_is_not_a_payment(self) -> None:
        for paid_at in (None, ""):
            with self.subTest(paid_at=paid_at):
                verified = await self._verify_with_response(
                    {
                        "success": 1,
                        "data": {
                            "amount": 50_000,
                            "invoice_id": 1,
                            "reference": "REF-1",
                            "paid_at": paid_at,
                        },
                    }
                )
                self.assertFalse(verified.ok)
                self.assertEqual(verified.message, CUSTOMER_NOT_PAID)

    async def test_reference_without_paid_at_key_is_accepted(self) -> None:
        verified = await self._verify_with_response(
            {
                "success": 1,
                "data": {"amount": 50_000, "invoice_id": 1, "reference": "REF-1"},
            }
        )
        self.assertTrue(verified.ok)
        self.assertEqual(verified.message, "REF-1")

    async def test_only_documented_success_value_counts(self) -> None:
        for flag in ("ok", "yes", "true", "success", 2):
            with self.subTest(flag=flag):
                verified = await self._verify_with_response(
                    {
                        "success": flag,
                        "data": {
                            "amount": 50_000,
                            "invoice_id": 1,
                            "reference": "REF-1",
                            "paid_at": "2026-09-27 09:30:00",
                        },
                    }
                )
                self.assertFalse(verified.ok)


class SoftCancelHelperTests(unittest.TestCase):
    def test_zarinpal_nok_and_sunpay_success_zero(self) -> None:
        self.assertTrue(callback_indicates_user_abort({"Status": "NOK"}))
        self.assertTrue(callback_indicates_user_abort({"success": "0"}))
        self.assertFalse(callback_indicates_user_abort({"Status": "OK"}))
        self.assertFalse(callback_indicates_user_abort({"success": "1"}))
        self.assertFalse(callback_indicates_user_abort({}))

    def test_invoice_must_match_settlement(self) -> None:
        callback_invoice_matches_settlement({"invoice_id": "15"}, settlement_id=15)
        with self.assertRaises(ValueError):
            callback_invoice_matches_settlement({"invoice_id": "16"}, settlement_id=15)


class RegistryTests(unittest.TestCase):
    def test_get_psp_adapter_sunpay(self) -> None:
        adapter = get_psp_adapter(
            "sunpay",
            ui={"psp_merchant_id": API_KEY, "psp_pay_url": PAY_URL},
            public_base_url="https://bot.test",
        )
        self.assertIsInstance(adapter, SunPayAdapter)
        self.assertEqual(adapter.name, "sunpay")


class SunPaySettlementFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        from app.config import get_settings

        get_settings.cache_clear()
        clear_settings_cache()
        await close_client()
        self._tmp = tempfile.TemporaryDirectory()
        db_path = Path(self._tmp.name) / "t.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        from app.config import get_settings

        await close_client()
        await self.engine.dispose()
        self._tmp.cleanup()
        clear_settings_cache()
        get_settings.cache_clear()

    async def _configure_platform_sunpay(self, session: AsyncSession) -> None:
        await set_setting(session, "pay_psp_enabled", "1")
        await set_setting(session, "psp_provider", "sunpay")
        await set_setting(session, "psp_merchant_id", API_KEY)
        await set_setting(session, "psp_pay_url", PAY_URL)

    async def _seed_wallet_topup(
        self, session: AsyncSession, *, amount: int = 50_000, wallet_shop_id=None
    ) -> Payment:
        user = BotUser(
            telegram_id=1001,
            username="u",
            full_name="u",
            referral_code="r1",
            role=Role.USER.value,
            reseller_id=wallet_shop_id,
        )
        session.add(user)
        await session.flush()
        payment = Payment(
            user_id=user.id,
            amount=amount,
            method=PaymentMethod.PSP.value,
            status=PaymentStatus.PENDING.value,
            is_wallet_topup=True,
            wallet_shop_id=wallet_shop_id,
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)
        return payment

    def _request_handler(self, *, verify_amount: int = 50_000):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/request"):
                return httpx.Response(
                    200, json={"success": 1, "data": {"token": TOKEN}}
                )
            if request.url.path.endswith("/api/verified"):
                # invoice_id filled by caller via side channel — see tests that bind sid.
                invoice = getattr(self, "_verify_invoice_id", 1)
                return httpx.Response(
                    200,
                    json={
                        "success": 1,
                        "data": {
                            "amount": verify_amount,
                            "invoice_id": invoice,
                            "reference": "REF-OK",
                            "paid_at": "2026-09-27 09:30:00",
                            "detail": {},
                        },
                    },
                )
            return httpx.Response(404, text="missing")

        return handler

    async def _checkout(self, payment_id: int) -> int:
        http_p, dns_p, client = _patch_sunpay_http(self._request_handler())
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
        ):
            async with self.Session() as session:
                payment = await session.get(Payment, payment_id)
                assert payment is not None
                settlement = await create_psp_checkout(session, payment)
                self.assertEqual(settlement.status, SettlementStatus.AWAITING.value)
                self.assertTrue(
                    (settlement.checkout_url or "").startswith("https://pay.sunpay.test/")
                )
                sid = int(settlement.id)
        await client.aclose()
        return sid

    async def test_checkout_and_return_settles_once(self) -> None:
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)
        self._verify_invoice_id = sid

        approve = AsyncMock(return_value=None)
        http_p, dns_p, client = _patch_sunpay_http(self._request_handler())
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
            patch("app.services.payment_settlement.approve_payment", approve),
        ):
            async with self.Session() as session:
                settled = await complete_psp_return(
                    session,
                    provider="sunpay",
                    settlement_id=sid,
                    callback_params={
                        "success": "1",
                        "invoice_id": str(sid),
                        "token": TOKEN,
                    },
                    reseller_id=None,
                )
                self.assertEqual(settled.status, SettlementStatus.SETTLED.value)
                approve.assert_awaited()
        await client.aclose()

    async def test_return_without_params_does_not_burn_settlement(self) -> None:
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)

        for params in ({}, {"invoice_id": str(sid)}):
            with self.subTest(params=params):
                async with self.Session() as session:
                    with self.assertRaises(ValueError):
                        await complete_psp_return(
                            session,
                            provider="sunpay",
                            settlement_id=sid,
                            callback_params=params,
                            reseller_id=None,
                        )
                    row = await session.get(PaymentSettlement, sid)
                    assert row is not None
                    self.assertEqual(row.status, SettlementStatus.AWAITING.value)

    async def test_verify_timeout_keeps_settlement_awaiting(self) -> None:
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/verified"):
                raise httpx.ConnectTimeout("slow")
            return httpx.Response(404)

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
        ):
            async with self.Session() as session:
                with self.assertRaises(ValueError):
                    await complete_psp_return(
                        session,
                        provider="sunpay",
                        settlement_id=sid,
                        callback_params={
                            "success": "1",
                            "invoice_id": str(sid),
                            "token": TOKEN,
                        },
                        reseller_id=None,
                    )
                row = await session.get(PaymentSettlement, sid)
                assert row is not None
                self.assertEqual(row.status, SettlementStatus.AWAITING.value)
        await client.aclose()

    async def test_success_zero_does_not_burn_settlement(self) -> None:
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)

        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await complete_psp_return(
                    session,
                    provider="sunpay",
                    settlement_id=sid,
                    callback_params={
                        "success": "0",
                        "invoice_id": str(sid),
                        "token": TOKEN,
                    },
                    reseller_id=None,
                )
            row = await session.get(PaymentSettlement, sid)
            assert row is not None
            self.assertEqual(row.status, SettlementStatus.AWAITING.value)

    async def test_wrong_shop_cannot_complete_return(self) -> None:
        async with self.Session() as session:
            shop = BotUser(
                telegram_id=2002,
                username="shop",
                full_name="shop",
                referral_code="shop1",
                role=Role.RESELLER.value,
            )
            session.add(shop)
            await session.flush()
            await set_setting(session, "pay_psp_enabled", "1", reseller_id=shop.id)
            await set_setting(session, "psp_provider", "sunpay", reseller_id=shop.id)
            await set_setting(session, "psp_merchant_id", API_KEY, reseller_id=shop.id)
            await set_setting(session, "psp_pay_url", PAY_URL, reseller_id=shop.id)
            payment = await self._seed_wallet_topup(
                session, wallet_shop_id=shop.id
            )
            shop_id = int(shop.id)
            payment_id = int(payment.id)

        http_p, dns_p, client = _patch_sunpay_http(self._request_handler())
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
        ):
            async with self.Session() as session:
                payment = await session.get(Payment, payment_id)
                assert payment is not None
                settlement = await create_psp_checkout(
                    session, payment, reseller_id=shop_id
                )
                sid = int(settlement.id)

            async with self.Session() as session:
                with self.assertRaises(ValueError):
                    await complete_psp_return(
                        session,
                        provider="sunpay",
                        settlement_id=sid,
                        callback_params={
                            "success": "1",
                            "invoice_id": str(sid),
                            "token": TOKEN,
                        },
                        reseller_id=999999,
                    )
        await client.aclose()

    async def test_poller_settles_without_browser_return(self) -> None:
        """Background poll re-verifies AWAITING rows after the customer closes the tab."""
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)
        self._verify_invoice_id = sid

        approve = AsyncMock(return_value=None)
        http_p, dns_p, client = _patch_sunpay_http(self._request_handler())
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
            patch("app.services.payment_settlement.approve_payment", approve),
            patch(
                "app.services.payment_settlement.notify_after_settlement",
                AsyncMock(),
            ),
        ):
            async with self.Session() as session:
                n = await poll_psp_settlements(session)
                self.assertEqual(n, 1)
                row = await session.get(PaymentSettlement, sid)
                assert row is not None
                self.assertEqual(row.status, SettlementStatus.SETTLED.value)
                approve.assert_awaited()
        await client.aclose()

    async def test_poller_does_not_settle_unpaid_lookup(self) -> None:
        """An abandoned checkout's lookup has no reference/paid_at and must not credit."""
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "success": 1,
                    "data": {
                        "amount": 50_000,
                        "invoice_id": sid,
                        "reference": None,
                        "paid_at": None,
                        "detail": {},
                    },
                },
            )

        approve = AsyncMock(return_value=None)
        http_p, dns_p, client = _patch_sunpay_http(handler)
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
            patch("app.services.payment_settlement.approve_payment", approve),
        ):
            async with self.Session() as session:
                n = await poll_psp_settlements(session)
                self.assertEqual(n, 0)
                row = await session.get(PaymentSettlement, sid)
                assert row is not None
                self.assertEqual(row.status, SettlementStatus.AWAITING.value)
                approve.assert_not_awaited()
        await client.aclose()

    async def test_poller_skips_settlements_older_than_window(self) -> None:
        from datetime import datetime, timezone

        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)
        self._verify_invoice_id = sid

        async with self.Session() as session:
            from datetime import timedelta

            row = await session.get(PaymentSettlement, sid)
            assert row is not None
            # Just outside the window so created_at >= since fails.
            row.created_at = datetime.now(timezone.utc) - PSP_POLL_WINDOW - timedelta(seconds=30)
            await session.commit()

        verify_calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/verified"):
                verify_calls["n"] += 1
            return httpx.Response(500, text="should not be called")

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
            patch("app.services.payment_settlement.approve_payment", AsyncMock()),
        ):
            async with self.Session() as session:
                n = await poll_psp_settlements(session)
                self.assertEqual(n, 0)
                row = await session.get(PaymentSettlement, sid)
                assert row is not None
                self.assertEqual(row.status, SettlementStatus.AWAITING.value)
        await client.aclose()
        self.assertEqual(verify_calls["n"], 0)

    async def test_callback_uses_live_https_when_env_is_http(self) -> None:
        seen_callbacks: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/api/request"):
                body = json.loads(request.content)
                seen_callbacks.append(body.get("callback", ""))
                return httpx.Response(200, json={"success": 1, "data": {"token": TOKEN}})
            return httpx.Response(404)

        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with (
            http_p,
            dns_p,
            patch.object(pub, "https_is_active", return_value=True),
            patch.object(
                pub, "public_panel_base_url", return_value="https://panel.live.example"
            ),
            patch.object(pub, "env_public_base", return_value="http://203.0.113.5:9000"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="http://203.0.113.5:9000",
            ),
        ):
            # https_public_base is a real function — don't stub it; let TLS win.
            async with self.Session() as session:
                payment = await session.get(Payment, payment_id)
                assert payment is not None
                settlement = await create_psp_checkout(session, payment)
                self.assertEqual(settlement.status, SettlementStatus.AWAITING.value)
        await client.aclose()
        self.assertEqual(len(seen_callbacks), 1)
        self.assertTrue(
            seen_callbacks[0].startswith(
                "https://panel.live.example/payments/settlement/psp/sunpay/return/"
            )
        )

    async def test_sunpay_refuses_without_https_before_network(self) -> None:
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(500)

        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        http_p, dns_p, client = _patch_sunpay_http(handler)
        with (
            http_p,
            dns_p,
            patch.object(pub, "https_is_active", return_value=False),
            patch.object(pub, "env_public_base", return_value="http://203.0.113.5:9000"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="http://203.0.113.5:9000",
            ),
        ):
            async with self.Session() as session:
                payment = await session.get(Payment, payment_id)
                assert payment is not None
                with self.assertRaises(ValueError) as caught:
                    await create_psp_checkout(session, payment)
                self.assertIn("در دسترس نیست", str(caught.exception))
        await client.aclose()
        self.assertEqual(calls["n"], 0)

    async def test_deliver_psp_once_notifies_only_once(self) -> None:
        async with self.Session() as session:
            await self._configure_platform_sunpay(session)
            payment = await self._seed_wallet_topup(session)
            payment_id = int(payment.id)

        sid = await self._checkout(payment_id)
        self._verify_invoice_id = sid

        notify = AsyncMock()
        approve = AsyncMock(return_value=None)
        http_p, dns_p, client = _patch_sunpay_http(self._request_handler())
        with (
            http_p,
            dns_p,
            _patch_https_public_base("https://bot.test"),
            patch(
                "app.services.payment_settlement._public_base",
                return_value="https://bot.test",
            ),
            patch("app.services.payment_settlement.approve_payment", approve),
            patch("app.services.payment_settlement.notify_after_settlement", notify),
        ):
            async with self.Session() as session:
                settled = await complete_psp_return(
                    session,
                    provider="sunpay",
                    settlement_id=sid,
                    callback_params={
                        "success": "1",
                        "invoice_id": str(sid),
                        "token": TOKEN,
                    },
                    reseller_id=None,
                )
                await deliver_psp_once(session, settled)
                await deliver_psp_once(session, settled)
                self.assertEqual(notify.await_count, 1)
        await client.aclose()


if __name__ == "__main__":
    unittest.main()
