"""PAYG prices for each change to a shop's panel user (create, renew, volume, days, reset)."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.services.payg_pricing import (
    GB,
    ChargeEvent,
    PaygPriceError,
    PaygRates,
    ServiceChange,
    assert_billable_rates,
    assert_quote_unchanged,
    quote_change,
    rates_from_plan,
)


def rates(basis: str = "traffic", *, upfront: bool = True, gb: int = 1000, user: int = 0,
          per_30d: bool = False, unlimited: int | None = None) -> PaygRates:
    return PaygRates(basis=basis, charge_upfront=upfront, price_per_gb=gb, user_price=user,
                     user_price_per_30d=per_30d, unlimited_price=unlimited)


def change(event: ChargeEvent, *, gb: int | None = None, prev_gb: int = 0, days: int | None = None) -> ServiceChange:
    return ServiceChange(event=event, data_limit_bytes=None if gb is None else gb * GB,
                         previous_limit_bytes=prev_gb * GB, days=days)


class TrafficTests(unittest.TestCase):
    def test_create_charges_the_volume_set_on_the_user(self) -> None:
        quote = quote_change(rates(gb=1500), change(ChargeEvent.CREATE, gb=50, days=30))
        self.assertEqual(quote.amount, 75_000)
        self.assertEqual(quote.bytes_charged, 50 * GB)

    def test_partial_gigabyte_is_rounded_up(self) -> None:
        quote = quote_change(rates(gb=1000), ServiceChange(ChargeEvent.CREATE, data_limit_bytes=GB + 1))
        self.assertEqual(quote.amount, 1001)

    def test_volume_increase_charges_only_the_difference(self) -> None:
        self.assertEqual(quote_change(rates(), change(ChargeEvent.ADD_VOLUME, gb=80, prev_gb=50)).amount, 30_000)

    def test_volume_decrease_costs_nothing(self) -> None:
        self.assertTrue(quote_change(rates(), change(ChargeEvent.ADD_VOLUME, gb=20, prev_gb=50)).is_free)

    def test_reset_charges_the_whole_volume(self) -> None:
        self.assertEqual(quote_change(rates(), change(ChargeEvent.RESET, gb=50)).amount, 50_000)

    def test_renew_and_reactivate_charge_the_new_period_volume(self) -> None:
        for event in (ChargeEvent.RENEW, ChargeEvent.REACTIVATE):
            self.assertEqual(quote_change(rates(), change(event, gb=10, days=30)).amount, 10_000)

    def test_extra_days_are_free_on_traffic(self) -> None:
        self.assertTrue(quote_change(rates(), change(ChargeEvent.ADD_DAYS, gb=10, days=30)).is_free)

    def test_usage_billed_traffic_has_no_charge_at_creation(self) -> None:
        r = rates(upfront=False)
        self.assertTrue(r.bills_usage)
        self.assertTrue(quote_change(r, change(ChargeEvent.CREATE, gb=50)).is_free)


class UserTests(unittest.TestCase):
    def test_flat_user_price_per_user(self) -> None:
        r = rates("user", gb=0, user=20_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.CREATE, gb=500, days=90)).amount, 20_000)
        self.assertTrue(quote_change(r, change(ChargeEvent.ADD_DAYS, days=30)).is_free)
        self.assertTrue(quote_change(r, change(ChargeEvent.RESET, gb=500)).is_free)

    def test_time_based_user_price_counts_started_30_days(self) -> None:
        r = rates("user", gb=0, user=20_000, per_30d=True)
        self.assertEqual(quote_change(r, change(ChargeEvent.CREATE, gb=5, days=30)).amount, 20_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.CREATE, gb=5, days=31)).amount, 40_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.CREATE, gb=5, days=90)).amount, 60_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.ADD_DAYS, days=15)).amount, 20_000)

    def test_user_plus_traffic_adds_both_parts(self) -> None:
        r = rates("user_traffic", gb=1000, user=10_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.CREATE, gb=30, days=30)).amount, 40_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.ADD_VOLUME, gb=40, prev_gb=30)).amount, 10_000)


class UnlimitedTests(unittest.TestCase):
    def test_unlimited_volume_is_refused_without_a_price(self) -> None:
        with self.assertRaises(PaygPriceError):
            quote_change(rates(), change(ChargeEvent.CREATE, gb=None, days=30))

    def test_unlimited_volume_uses_its_price(self) -> None:
        quote = quote_change(rates(unlimited=300_000), change(ChargeEvent.CREATE, gb=None, days=30))
        self.assertEqual(quote.amount, 300_000)
        self.assertTrue(quote.is_unlimited)

    def test_turning_a_limited_user_unlimited_costs_the_unlimited_price(self) -> None:
        quote = quote_change(rates(unlimited=300_000), change(ChargeEvent.ADD_VOLUME, gb=None, prev_gb=50))
        self.assertEqual(quote.amount, 300_000)

    def test_unlimited_period_on_time_based_user_price(self) -> None:
        r = rates("user", gb=0, user=20_000, per_30d=True)
        with self.assertRaises(PaygPriceError):
            quote_change(r, change(ChargeEvent.CREATE, gb=5, days=None))
        with self.assertRaises(PaygPriceError):
            quote_change(r, change(ChargeEvent.ADD_DAYS, days=None))

    def test_unlimited_price_replaces_only_the_unlimited_part(self) -> None:
        r = rates("user_traffic", gb=1000, user=10_000, unlimited=200_000)
        self.assertEqual(quote_change(r, change(ChargeEvent.CREATE, gb=None, days=30)).amount, 210_000)


class ValidationTests(unittest.TestCase):
    def test_hostile_numbers_are_refused(self) -> None:
        for bad in (True, -1, 1.5, "10"):
            with self.subTest(bad=bad), self.assertRaises(PaygPriceError):
                quote_change(rates(), ServiceChange(ChargeEvent.CREATE, data_limit_bytes=bad))

    def test_an_amount_above_the_column_range_is_refused(self) -> None:
        with self.assertRaises(PaygPriceError):
            quote_change(rates(gb=2_000_000_000), change(ChargeEvent.CREATE, gb=10))

    def test_plan_rates_are_read_safely(self) -> None:
        plan = SimpleNamespace(payg_basis="USER", payg_charge_upfront=True, price_per_gb=0,
                               payg_user_price=5000, payg_user_price_per_30d=True, payg_unlimited_price=None)
        r = rates_from_plan(plan)
        self.assertEqual((r.basis, r.user_price, r.unlimited_price), ("user", 5000, None))
        self.assertEqual(rates_from_plan(SimpleNamespace(payg_basis="bogus")).basis, "traffic")
        self.assertFalse(rates_from_plan(None).charge_upfront)
        with self.assertRaises(PaygPriceError):
            rates_from_plan(SimpleNamespace(price_per_gb=-5))

    def test_package_that_would_bill_nothing_is_refused(self) -> None:
        with self.assertRaises(PaygPriceError):
            assert_billable_rates(rates(gb=0))
        with self.assertRaises(PaygPriceError):
            assert_billable_rates(rates("user", gb=0, user=0))
        assert_billable_rates(rates("user", gb=0, user=1))

    def test_a_changed_price_is_not_charged_silently(self) -> None:
        quote = quote_change(rates(), change(ChargeEvent.CREATE, gb=10))
        assert_quote_unchanged(10_000, quote)
        for shown in (9_000, True, "10000"):
            with self.subTest(shown=shown), self.assertRaises(PaygPriceError):
                assert_quote_unchanged(shown, quote)


if __name__ == "__main__":
    unittest.main()
