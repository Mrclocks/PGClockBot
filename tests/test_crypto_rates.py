"""Crypto amounts from a shop's own rate: safe fetching, exact integer math, locked quotes, per-shop isolation."""

from __future__ import annotations

import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import Base
from app.db.models import BotUser, Order, Payment, Role
from app.services import crypto_rates as cr
from app.services import url_safety
from app.services.crypto_rates import (
    RateConfig,
    RateUnavailable,
    amount_micro,
    attach_quote,
    config_from_settings,
    current_rate,
    extract_number,
    fetch_api_rate,
    format_amount,
    parse_decimal,
    quote_for,
    quote_lines,
    refresh_api_rates,
    store_api_rate,
)
from app.services.url_safety import UnsafeUrlError, ensure_host_resolves_public, validate_public_https_url
from app.services.users import DEFAULT_SETTINGS, SETTING_GROUPS

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)


def _config(**changes) -> RateConfig:
    base = {
        "mode": "manual", "asset": "USDT", "manual_rate": Decimal("98500"), "api_url": "https://rates.example/price",
        "api_path": "data.price", "divisor": Decimal(1), "margin_bps": 0, "quote_minutes": 15,
    }
    base.update(changes)
    return RateConfig(**base)


class UrlSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_only_a_public_https_address_on_the_default_port_is_accepted(self) -> None:
        self.assertEqual(validate_public_https_url("https://rates.example/v1/p?x=1")[1], "rates.example")
        for bad in (
            "http://rates.example/p", "ftp://rates.example", "https://user:pw@rates.example/p", "https://rates.example:8443/p",
            "https://localhost/p", "https://127.0.0.1/p", "https://10.0.0.5/p", "https://192.168.1.1/p", "https://169.254.169.254/",
            "https://[::1]/p", "https://metadata.google.internal/", "https://printer.local/", "https://intranet/p", "", "   ",
            "https://rates.example/p q", "https://" + "a" * 600 + ".example/", "https://rates.example/\x00",
        ):
            with self.subTest(url=bad), self.assertRaises(UnsafeUrlError):
                validate_public_https_url(bad)

    async def test_a_name_that_resolves_to_a_private_address_is_refused(self) -> None:
        with patch.object(url_safety, "_resolve", return_value=["93.184.216.34"]):
            await ensure_host_resolves_public("rates.example")
        for answers in (["10.0.0.7"], ["93.184.216.34", "127.0.0.1"], ["fd00::1"], []):
            with self.subTest(answers=answers), patch.object(url_safety, "_resolve", return_value=answers), self.assertRaises(UnsafeUrlError):
                await ensure_host_resolves_public("rates.example")
        with patch.object(url_safety, "_resolve", side_effect=OSError("no such host")), self.assertRaises(UnsafeUrlError):
            await ensure_host_resolves_public("rates.example")


class ConfigTests(unittest.TestCase):
    def test_conversion_is_off_by_default_and_for_anything_unknown(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["crypto_rate_mode"], "off")
        for mode in (None, "", "off", "auto", "MANUALL", True):
            with self.subTest(mode=mode):
                self.assertIsNone(config_from_settings({"crypto_rate_mode": mode}))
        self.assertIsNone(config_from_settings({"crypto_rate_mode": "manual", "crypto_rate_asset": "US DT!"}))

    def test_values_are_read_strictly(self) -> None:
        config = config_from_settings({
            "crypto_rate_mode": "api", "crypto_rate_asset": " usdt ", "crypto_rate_manual": "98500.5",
            "crypto_rate_api_url": " https://rates.example/p ", "crypto_rate_api_path": "data.price",
            "crypto_rate_api_divisor": "10", "crypto_rate_margin_percent": "1.5", "crypto_quote_minutes": "30",
        })
        self.assertEqual((config.mode, config.asset, config.manual_rate, config.divisor, config.margin_bps, config.quote_minutes),
                         ("api", "USDT", Decimal("98500.5"), Decimal(10), 150, 30))
        self.assertEqual(config.api_url, "https://rates.example/p")

    def test_malformed_numbers_fall_back_to_safe_values(self) -> None:
        config = config_from_settings({
            "crypto_rate_mode": "manual", "crypto_rate_manual": "-5", "crypto_rate_api_divisor": "abc",
            "crypto_rate_margin_percent": "500", "crypto_quote_minutes": "9999",
        })
        self.assertEqual((config.manual_rate, config.divisor, config.margin_bps, config.quote_minutes), (None, Decimal(1), 0, 120))
        for raw in ("0", "-1", "1e5", "1,5", "NaN", "inf", "٥", "1.1234567", "99999999999999"):
            with self.subTest(raw=raw):
                self.assertIsNone(parse_decimal(raw))
        self.assertEqual(parse_decimal("98500"), Decimal("98500"))

    def test_the_settings_are_offered_in_the_crypto_settings_group(self) -> None:
        keys = {item[0] for item in SETTING_GROUPS["رمزارز"]}
        self.assertTrue({"crypto_rate_mode", "crypto_rate_manual", "crypto_rate_api_url", "crypto_rate_margin_percent", "crypto_quote_minutes"} <= keys)


class JsonPathTests(unittest.TestCase):
    def test_numbers_are_read_from_nested_keys_and_list_positions(self) -> None:
        payload = {"stats": {"usdt-rls": {"latest": "1051000.5"}}, "data": [{"price": 98700}], "n": 3.5}
        self.assertEqual(extract_number(payload, "stats.usdt-rls.latest"), Decimal("1051000.5"))
        self.assertEqual(extract_number(payload, "data.0.price"), Decimal(98700))
        self.assertEqual(extract_number(payload, "n"), Decimal("3.5"))

    def test_anything_that_is_not_a_positive_finite_number_is_refused(self) -> None:
        payload = {"a": True, "b": None, "c": [1], "d": "abc", "e": "NaN", "f": "Infinity", "g": -4, "h": 0, "i": {"j": 1}}
        for path in ("a", "b", "c", "d", "e", "f", "g", "h", "i", "missing", "i.k", "c.5", "", ".", "a.b.c.d.e.f.g.h.i.j"):
            with self.subTest(path=path), self.assertRaises(RateUnavailable):
                extract_number(payload, path)


class MathTests(unittest.TestCase):
    def test_the_amount_is_exact_integer_math_rounded_up_to_two_decimals(self) -> None:
        micro = amount_micro(90_000, Decimal("98500"), 0)
        self.assertIsInstance(micro, int)
        self.assertEqual(format_amount(micro), "0.92")  # 0.9137 USDT -> 0.92
        self.assertEqual(micro % 10_000, 0)
        self.assertEqual(format_amount(amount_micro(985_000, Decimal("98500"), 0)), "10.00")
        self.assertEqual(format_amount(amount_micro(985_001, Decimal("98500"), 0)), "10.01")

    def test_the_margin_raises_the_amount_and_the_customer_never_underpays(self) -> None:
        rate = Decimal("98500.25")
        for toman in (1, 999, 50_000, 90_000, 1_234_567, 99_999_999):
            for bps in (0, 150, 2000):
                with self.subTest(toman=toman, bps=bps):
                    micro = amount_micro(toman, rate, bps)
                    self.assertGreaterEqual(Decimal(micro) / 10**6 * rate, Decimal(toman) * (10_000 + bps) / 10_000 - Decimal("0.0001"))
        self.assertGreater(amount_micro(1_000_000, rate, 200), amount_micro(1_000_000, rate, 0))


class RateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        cr.forget_rates()
        self.addCleanup(cr.forget_rates)

    def test_api_mode_uses_a_fresh_cached_rate_then_falls_back_to_the_manual_one(self) -> None:
        config = _config(mode="api")
        store_api_rate(0, Decimal("99000"), now=1000.0)
        self.assertEqual(current_rate(config, 0, now=1100.0), Decimal("99000"))
        self.assertEqual(current_rate(config, 0, now=1000.0 + cr.RATE_MAX_AGE_SECONDS + 1), Decimal("98500"))
        self.assertIsNone(current_rate(_config(mode="api", manual_rate=None), 0, now=1000.0 + cr.RATE_MAX_AGE_SECONDS + 1))
        self.assertEqual(current_rate(_config(mode="manual"), 0, now=1100.0), Decimal("98500"))

    def test_a_sudden_jump_is_ignored_until_it_has_lasted(self) -> None:
        self.assertTrue(store_api_rate(0, Decimal("100000"), now=0.0))
        self.assertFalse(store_api_rate(0, Decimal("160000"), now=60.0))
        self.assertFalse(store_api_rate(0, Decimal("60000"), now=120.0))
        self.assertTrue(store_api_rate(0, Decimal("110000"), now=180.0))
        self.assertTrue(store_api_rate(0, Decimal("200000"), now=180.0 + cr.JUMP_GUARD_SECONDS + 1))

    def test_each_shop_has_its_own_rate(self) -> None:
        store_api_rate(0, Decimal("100000"), now=0.0)
        store_api_rate(7, Decimal("90000"), now=0.0)
        config = _config(mode="api", manual_rate=None)
        self.assertEqual(current_rate(config, 0, now=10.0), Decimal("100000"))
        self.assertEqual(current_rate(config, 7, now=10.0), Decimal("90000"))
        self.assertIsNone(current_rate(config, 8, now=10.0))


class QuoteTests(unittest.TestCase):
    def setUp(self) -> None:
        cr.forget_rates()
        self.addCleanup(cr.forget_rates)

    def test_no_quote_when_conversion_is_off_or_the_wallet_is_another_asset(self) -> None:
        self.assertIsNone(quote_for(None, scope=0, wallet_asset="USDT", toman=90_000))
        self.assertIsNone(quote_for(_config(), scope=0, wallet_asset="TRX", toman=90_000))
        self.assertIsNotNone(quote_for(_config(), scope=0, wallet_asset=" usdt ", toman=90_000))

    def test_a_quote_locks_amount_rate_and_expiry(self) -> None:
        quote = quote_for(_config(quote_minutes=20), scope=0, wallet_asset="USDT", toman=985_000, now=NOW)
        self.assertEqual((quote.asset, format_amount(quote.amount_micro), quote.rate_scaled), ("USDT", "10.00", 985_000_000))
        self.assertEqual(quote.expires_at, NOW + timedelta(minutes=20))

    def test_without_a_usable_rate_the_customer_is_told_instead_of_shown_a_wrong_amount(self) -> None:
        with self.assertRaises(RateUnavailable):
            quote_for(_config(manual_rate=None), scope=0, wallet_asset="USDT", toman=90_000)
        with self.assertRaises(RateUnavailable):
            quote_for(_config(mode="api", manual_rate=None), scope=3, wallet_asset="USDT", toman=90_000)
        with self.assertRaises(RateUnavailable):
            quote_for(_config(), scope=0, wallet_asset="USDT", toman=0)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []

    def _client(self, handler) -> httpx.AsyncClient:
        def wrapped(request: httpx.Request) -> httpx.Response:
            self.calls.append(str(request.url))
            return handler(request)

        return httpx.AsyncClient(transport=httpx.MockTransport(wrapped), follow_redirects=False)

    async def _fetch(self, handler, **config) -> Decimal:
        client = self._client(handler)
        try:
            with patch.object(cr, "_client", client), patch.object(url_safety, "_resolve", return_value=["93.184.216.34"]):
                return await fetch_api_rate(_config(mode="api", **config))
        finally:
            await client.aclose()

    async def test_a_valid_response_gives_the_rate_with_the_divisor_applied(self) -> None:
        rate = await self._fetch(lambda r: httpx.Response(200, json={"data": {"price": "985000"}}), divisor=Decimal(10))
        self.assertEqual(rate, Decimal("98500"))

    async def test_every_bad_answer_is_unavailable_not_a_crash(self) -> None:
        cases = {
            "status": httpx.Response(503, json={}),
            "redirect": httpx.Response(302, headers={"location": "http://10.0.0.1/"}),
            "not json": httpx.Response(200, content=b"<html>hi</html>"),
            "no number": httpx.Response(200, json={"data": {}}),
            "absurd": httpx.Response(200, json={"data": {"price": "99999999999"}}),
            "too large": httpx.Response(200, content=b"[" + b"1," * (cr.MAX_RESPONSE_BYTES // 2) + b"1]"),
        }
        for label, response in cases.items():
            with self.subTest(label), self.assertRaises(RateUnavailable):
                await self._fetch(lambda r, response=response: response)

    async def test_a_redirect_is_never_followed(self) -> None:
        with self.assertRaises(RateUnavailable):
            await self._fetch(lambda r: httpx.Response(302, headers={"location": "https://other.example/p"}))
        self.assertEqual(len(self.calls), 1)

    async def test_an_unsafe_address_is_refused_before_any_request_is_made(self) -> None:
        for url in ("http://rates.example/p", "https://127.0.0.1/p", "https://rates.example:8443/p", "https://user:pw@rates.example/p"):
            with self.subTest(url=url), self.assertRaises(RateUnavailable):
                await self._fetch(lambda r: httpx.Response(200, json={"data": {"price": 1}}), api_url=url)
        with patch.object(url_safety, "_resolve", return_value=["10.0.0.9"]), self.assertRaises(RateUnavailable):
            await fetch_api_rate(_config(mode="api"))
        self.assertEqual(self.calls, [])


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        cr.forget_rates()
        self.addCleanup(cr.forget_rates)

    async def test_only_shops_in_api_mode_are_fetched_and_one_failure_does_not_stop_the_rest(self) -> None:
        settings = {
            None: {"crypto_rate_mode": "api", "crypto_rate_api_url": "https://a.example/p", "crypto_rate_api_path": "p"},
            1: {"crypto_rate_mode": "manual", "crypto_rate_manual": "98000"},
            2: {"crypto_rate_mode": "api", "crypto_rate_api_url": "https://broken.example/p", "crypto_rate_api_path": "p"},
            3: {"crypto_rate_mode": "api", "crypto_rate_api_url": "https://c.example/p", "crypto_rate_api_path": "p"},
            4: {},
        }

        async def settings_for(shop_id):
            return settings[shop_id]

        async def fake_fetch(config):
            if "broken" in config.api_url:
                raise RateUnavailable("down")
            return Decimal("99000") if "a.example" in config.api_url else Decimal("97000")

        with patch.object(cr, "fetch_api_rate", side_effect=fake_fetch) as fetch:
            stored = await refresh_api_rates(settings_for, [None, 1, 2, 3, 4])
        self.assertEqual((stored, fetch.await_count), (2, 3))
        config = _config(mode="api", manual_rate=None)
        self.assertEqual(current_rate(config, 0), Decimal("99000"))
        self.assertEqual(current_rate(config, 3), Decimal("97000"))
        self.assertIsNone(current_rate(config, 1))
        self.assertIsNone(current_rate(config, 2))


class PaymentQuoteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmpdir.name) / 'c.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        cr.forget_rates()
        self.addCleanup(cr.forget_rates)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self._tmpdir.cleanup()

    async def _payment(self, session, *, status: str = "pending") -> Payment:
        self.counter = getattr(self, "counter", 0) + 1
        user = BotUser(
            telegram_id=self.counter, username=f"u{self.counter}", full_name="Buyer", role=Role.USER.value,
            referral_code=f"R{self.counter}",
        )
        session.add(user)
        await session.flush()
        order = Order(user_id=user.id, amount=985_000, status="awaiting_receipt")
        session.add(order)
        await session.flush()
        payment = Payment(order_id=order.id, user_id=user.id, amount=985_000, method="crypto", status=status)
        session.add(payment)
        await session.commit()
        return payment

    async def test_the_quote_is_saved_on_an_undecided_payment_only(self) -> None:
        quote = quote_for(_config(), scope=0, wallet_asset="USDT", toman=985_000, now=NOW)
        async with self.Session() as session:
            pending = await self._payment(session)
            await attach_quote(session, pending.id, quote)
            saved = await session.get(Payment, pending.id, populate_existing=True)
            self.assertEqual((saved.crypto_asset, saved.crypto_amount_micro, saved.crypto_rate_scaled), ("USDT", 10_000_000, 985_000_000))
            decided = await self._payment(session, status="approved")
            await attach_quote(session, decided.id, quote)
            self.assertIsNone((await session.get(Payment, decided.id, populate_existing=True)).crypto_amount_micro)

    async def test_staff_see_the_amount_rate_and_validity_to_check_a_receipt_against(self) -> None:
        quote = quote_for(_config(quote_minutes=15), scope=0, wallet_asset="USDT", toman=985_000, now=NOW)
        async with self.Session() as session:
            payment = await self._payment(session)
            await attach_quote(session, payment.id, quote)
            payment = await session.get(Payment, payment.id, populate_existing=True)
        self.assertEqual(quote_lines(payment, now=NOW), ("10.00 USDT", "98,500.00", "15 دقیقه دیگر"))
        self.assertEqual(quote_lines(payment, now=NOW + timedelta(minutes=16))[2], "منقضی شده")
        self.assertIsNone(quote_lines(Payment(amount=1), now=NOW))

    async def test_the_pending_approval_notice_carries_the_expected_amount(self) -> None:
        from app.services import notifications

        quote = quote_for(_config(), scope=0, wallet_asset="USDT", toman=985_000)
        async with self.Session() as session:
            payment = await self._payment(session)
            await attach_quote(session, payment.id, quote)
            payment = await session.get(Payment, payment.id, populate_existing=True)
            dispatch = AsyncMock(return_value=1)
            with patch.object(notifications, "_dispatch_dual_notify", dispatch):
                await notifications.notify_pending_approval(AsyncMock(), session, payment, 11)
        body = dispatch.await_args.args[3]
        self.assertIn("مبلغ مورد انتظار", body)
        self.assertIn("10.00 USDT", body)


class CryptoPayHandlerTests(unittest.IsolatedAsyncioTestCase):
    """The order-payment handler: shows the converted amount, locks it, and never claims the order without a rate."""

    def setUp(self) -> None:
        cr.forget_rates()
        self.addCleanup(cr.forget_rates)

    async def _tap(self, ui: dict):
        from app.bot.handlers import shop

        order = SimpleNamespace(id=5, amount=985_000, user_id=3)
        callback = SimpleNamespace(data="pay:crypto:5", answer=AsyncMock(), message=SimpleNamespace(), bot=AsyncMock())
        session = SimpleNamespace(get=AsyncMock(return_value=order))
        user = SimpleNamespace(id=3, telegram_id=33)
        pay = SimpleNamespace(id=9)
        start = AsyncMock(return_value=pay)
        attach = AsyncMock()
        shown = AsyncMock()
        with (
            patch.object(shop, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(shop, "is_customer_order_in_current_shop", return_value=True),
            patch.object(shop, "start_method_payment", start),
            patch.object(shop, "attach_quote", attach),
            patch.object(shop, "_await_order_receipt", shown),
        ):
            await shop.pay_crypto_cb(callback, session, user, state=None)
        return callback, start, attach, shown

    def _ui(self, **extra) -> dict:
        wallets = '[{"id":"w1","asset":"USDT","network":"TRC20","address":"TXaddr","enabled":true}]'
        return {"pay_crypto_enabled": "1", "payment_crypto_wallets": wallets, "crypto_pay_text": DEFAULT_SETTINGS["crypto_pay_text"], "shop_title": "Shop", **extra}

    async def test_a_converted_payment_shows_the_crypto_amount_and_locks_the_quote(self) -> None:
        callback, start, attach, shown = await self._tap(self._ui(crypto_rate_mode="manual", crypto_rate_manual="98500"))
        start.assert_awaited_once()
        attach.assert_awaited_once()
        self.assertEqual(attach.await_args.args[1], 9)
        self.assertEqual(attach.await_args.args[2].amount_micro, 10_000_000)
        body = shown.await_args.kwargs["body"]
        self.assertIn("10.00 USDT", body)
        self.assertIn("معادل", body)
        self.assertIn("معتبر", body)

    async def test_without_a_rate_the_order_is_not_claimed_and_the_customer_is_told(self) -> None:
        callback, start, attach, shown = await self._tap(self._ui(crypto_rate_mode="api", crypto_rate_api_url="https://a.example/p"))
        start.assert_not_awaited()
        attach.assert_not_awaited()
        shown.assert_not_awaited()
        self.assertIn("نرخ", callback.answer.await_args.args[0])

    async def test_with_conversion_off_the_toman_price_is_shown_as_before(self) -> None:
        callback, start, attach, shown = await self._tap(self._ui())
        start.assert_awaited_once()
        attach.assert_not_awaited()
        self.assertNotIn("USDT (معادل", shown.await_args.kwargs["body"])


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_payments(self) -> None:
        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations

        spec = importlib.util.spec_from_file_location("crypto_quote_migration", ROOT / "alembic/versions/0076_payment_crypto_quote.py")
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = sa.create_engine("sqlite:///:memory:")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE payments (id INTEGER PRIMARY KEY, amount INTEGER)"))
            conn.execute(text("INSERT INTO payments VALUES (1, 985000)"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                columns = {c["name"] for c in sa.inspect(conn).get_columns("payments")}
                self.assertTrue({name for name, _ in migration.COLUMNS} <= columns)
                self.assertIsNone(conn.execute(text("SELECT crypto_amount_micro FROM payments")).scalar())
                migration.downgrade()
                migration.downgrade()
                self.assertEqual(conn.execute(text("SELECT amount FROM payments")).scalar(), 985000)
                self.assertFalse({name for name, _ in migration.COLUMNS} & {c["name"] for c in sa.inspect(conn).get_columns("payments")})
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
