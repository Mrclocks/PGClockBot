"""Finance report windows, chart reconciliation, access control and tenant isolation."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.finance_pages import register_finance_pages
from app.db import Base
from app.db.models import BotUser, BotUserShop, Order
from app.services.dashboard_trend import TEHRAN
from app.services.finance_report_analytics import build_finance_dashboard
from app.services.finance_report_ranges import report_intervals, resolve_finance_range

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=TEHRAN)
START = '2026-10-17T10:15'
END = '2026-10-17T12:45'
SHOP = 10
OTHER_SHOP = 20


class FinanceFilterHtml(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.form: dict[str, str | None] = {}
        self.toggle: dict[str, str | None] = {}
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == 'form' and 'data-finance-range' in attributes:
            self.form = attributes
        if tag == 'button' and 'data-finance-custom-toggle' in attributes:
            self.toggle = attributes


class FinanceRangeTests(unittest.TestCase):
    def test_custom_range_uses_tehran_time_and_exact_minutes(self) -> None:
        window = resolve_finance_range(period='custom', start=START, end=END)
        self.assertEqual(window.start, datetime(2026, 10, 17, 6, 45, tzinfo=timezone.utc))
        self.assertEqual(window.end - window.start, timedelta(hours=2, minutes=30))
        self.assertEqual(window.start_input, START)
        self.assertEqual(window.end_input, END)
        self.assertEqual(window.previous_start, window.start - timedelta(hours=2, minutes=30))

    def test_presets_start_at_tehran_midnight_and_include_current_minute(self) -> None:
        for period, days in [('day', 1), ('week', 7), ('month', 30)]:
            with self.subTest(period=period):
                window = resolve_finance_range(period=period, now=NOW)
                self.assertEqual(window.start.astimezone(TEHRAN), (NOW - timedelta(days=days - 1)).replace(hour=0))
                self.assertEqual(window.end.astimezone(TEHRAN), NOW + timedelta(minutes=1))
        self.assertEqual(resolve_finance_range(period='invalid', now=NOW).period, 'week')

    def test_invalid_or_incomplete_ranges_are_rejected(self) -> None:
        cases = [('', END), (START, ''), (END, START), (START, START),
                 ('2026-02-30T00:00', END), ('2026-10-17T10:15Z', END),
                 ('2026-10-17T10:15+03:30', END), ('1999-10-17T10:15', END),
                 ('9999-01-01T00:00', '9999-01-02T00:00'),
                 ('<script>', END), ('2026-01-01T00:00', '2027-01-03T00:00')]
        for start, end in cases:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                resolve_finance_range(period='custom', start=start, end=end)

    def test_intervals_are_bounded_and_cover_partial_first_and_last_buckets(self) -> None:
        for start, end, label in [(START, END, 'ساعتی'),
                                 ('2026-01-01T10:15', '2026-02-15T12:45', 'روزانه'),
                                 ('2026-01-01T10:15', '2027-01-02T10:15', 'هفتگی')]:
            with self.subTest(label=label):
                window = resolve_finance_range(period='custom', start=start, end=end)
                actual_label, boundaries = report_intervals(window)
                self.assertEqual(actual_label, label)
                self.assertEqual(boundaries[0], window.start)
                self.assertEqual(boundaries[-1], window.end)
                self.assertLessEqual(len(boundaries), 62)
                self.assertTrue(all(a < b for a, b in zip(boundaries, boundaries[1:])))


class FinanceDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.window = resolve_finance_range(period='custom', start=START, end=END)
        self.before_end = self.window.end - timedelta(microseconds=1)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code='platform', created_at=self.window.start),
                BotUser(id=2, telegram_id=102, referral_code='demo', is_demo=True, created_at=self.window.start),
                BotUser(id=SHOP, telegram_id=110, referral_code='shop', role='reseller', created_at=NOW),
                BotUser(id=OTHER_SHOP, telegram_id=120, referral_code='other', role='reseller', created_at=NOW),
            ])
            await session.flush()
            session.add_all([
                BotUser(id=3, telegram_id=103, referral_code='buyer', reseller_id=SHOP, created_at=self.window.start),
                BotUser(id=4, telegram_id=104, referral_code='other-buyer', reseller_id=OTHER_SHOP, created_at=self.window.start),
                BotUser(id=5, telegram_id=105, referral_code='after-end', created_at=self.window.end),
            ])
            await session.flush()
            session.add(BotUserShop(bot_user_id=1, shop_id=SHOP))
            session.add_all([
                self.order(1, 1000, self.window.start),
                self.order(1, 2000, self.before_end, method='wallet'),
                self.order(1, 500, self.window.previous_start),
                self.order(1, 999, self.window.start - timedelta(microseconds=1)),
                self.order(1, 55555, self.window.end),
                self.order(1, 55555, self.window.previous_start - timedelta(microseconds=1)),
                self.order(1, 200, self.window.start, status='pending'),
                self.order(1, 300, self.window.start, status='cancelled'),
                self.order(2, 88888, self.window.start),
                self.order(3, 7000, self.window.start, shop=SHOP),
                self.order(4, 9137, self.window.start, shop=OTHER_SHOP),
                self.order(1, 11000, self.window.start, shop=SHOP),
            ])
            await session.commit()

    def order(self, user: int, amount: int, when: datetime, *, shop: int | None = None,
              status: str = 'delivered', method: str = 'card') -> Order:
        return Order(user_id=user, amount=amount, created_at=when, reseller_id=shop,
                     status=status, payment_method=method)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def report(self, shop: int | None = None) -> dict:
        async with self.Session() as session:
            return await build_finance_dashboard(session, reseller_id=shop, window=self.window)

    async def test_platform_totals_use_exact_bounds_and_exclude_other_shops_and_demo_users(self) -> None:
        report = await self.report()
        self.assertEqual(report['active'], {
            'orders': 4, 'delivered': 2, 'revenue': 3000, 'pending': 1,
            'unsuccessful': 1, 'avg_order': 1500, 'new_users': 1,
        })
        self.assertEqual(report['previous']['revenue'], 1499)
        self.assertEqual(report['previous']['delivered'], 2)
        self.assertEqual(report['change_percent'], 100)

    async def test_second_shop_and_second_customer_cannot_leak_into_report(self) -> None:
        report = await self.report(SHOP)
        other = await self.report(OTHER_SHOP)
        self.assertEqual(report['active']['revenue'], 18000)
        self.assertEqual(other['active']['revenue'], 9137)
        self.assertEqual(report['active']['new_users'], 2)
        self.assertEqual(report['ops']['total_users'], 2)
        self.assertEqual(other['ops']['total_users'], 1)
        self.assertEqual(sum(point.revenue for point in report['timeline']), 18000)
        self.assertEqual(sum(method['amount'] for method in other['payment_methods']), 9137)

    async def test_chart_and_methods_reconcile_with_summary_including_zero_buckets(self) -> None:
        report = await self.report()
        points = report['timeline']
        self.assertEqual([point.revenue for point in points], [1000, 0, 2000])
        self.assertEqual(sum(point.revenue for point in points), report['active']['revenue'])
        self.assertEqual(sum(point.delivered for point in points), report['active']['delivered'])
        self.assertEqual(sum(method['amount'] for method in report['payment_methods']), 3000)
        self.assertEqual(sum(method['count'] for method in report['payment_methods']), 2)
        self.assertTrue(all(isinstance(point.revenue, int) for point in points))

    async def test_midnight_belongs_to_next_tehran_day(self) -> None:
        window = resolve_finance_range(period='custom', start='2026-10-16T00:00', end='2026-10-19T00:00')
        midnight = datetime(2026, 10, 18, tzinfo=TEHRAN).astimezone(timezone.utc)
        async with self.Session() as session:
            session.add(self.order(1, 123, midnight - timedelta(microseconds=1)))
            session.add(self.order(1, 456, midnight))
            await session.commit()
            report = await build_finance_dashboard(session, reseller_id=None, window=window)
        self.assertEqual(report['interval_label'], 'روزانه')
        self.assertEqual(report['timeline'][-1].revenue, 456)
        self.assertEqual(sum(point.revenue for point in report['timeline']), report['active']['revenue'])

    async def test_unknown_payment_methods_are_grouped_without_losing_sales(self) -> None:
        async with self.Session() as session:
            session.add_all(self.order(1, 100, self.window.start, method=f'unknown-{index}') for index in range(25))
            await session.commit()
        report = await self.report()
        methods = report['payment_methods']
        self.assertEqual(len(methods), 3)
        self.assertEqual(sum(method['amount'] for method in methods), report['active']['revenue'])
        unknown = next(method for method in methods if method['label'] == 'نامشخص')
        self.assertEqual(unknown['count'], 25)
        self.assertEqual(unknown['amount'], 2500)

    async def test_empty_and_free_sales_have_defined_chart_and_average(self) -> None:
        async with self.Session() as session:
            window = resolve_finance_range(period='custom', start='2025-01-01T00:00', end='2025-01-01T00:01')
            empty = await build_finance_dashboard(session, reseller_id=None, window=window)
            self.assertEqual(empty['active']['revenue'], 0)
            self.assertEqual(empty['active']['avg_order'], 0)
            self.assertIsNone(empty['change_percent'])
            session.add(self.order(1, 0, window.start))
            await session.commit()
            free = await build_finance_dashboard(session, reseller_id=None, window=window)
        self.assertEqual(free['active']['delivered'], 1)
        self.assertEqual(free['active']['avg_order'], 0)
        self.assertEqual(free['timeline'][0].x, 400)
        self.assertEqual(free['chart_points'], '400,200')

    async def test_small_revenue_change_is_rounded_without_overstating_the_decline(self) -> None:
        window = resolve_finance_range(period='custom', start='2025-01-01T00:00', end='2025-01-01T00:01')
        async with self.Session() as session:
            session.add(self.order(1, 10000, window.previous_start))
            session.add(self.order(1, 9999, window.start))
            await session.commit()
            report = await build_finance_dashboard(session, reseller_id=None, window=window)
        self.assertEqual(report['change_percent'], 0)

    async def test_long_range_does_not_query_per_chart_point(self) -> None:
        statements: list[str] = []
        def record(_conn, _cursor, statement, _parameters, _context, _many) -> None:
            statements.append(statement)
        event.listen(self.engine.sync_engine, 'before_cursor_execute', record)
        try:
            await self.report()
            short_count = len(statements)
            statements.clear()
            window = resolve_finance_range(period='custom', start='2026-01-01T00:00', end='2027-01-01T00:00')
            async with self.Session() as session:
                report = await build_finance_dashboard(session, reseller_id=None, window=window)
            self.assertEqual(len(statements), short_count)
            self.assertLessEqual(len(report['timeline']), 61)
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', record)

    async def test_order_aggregates_compile_for_postgresql_and_use_sqlite_indexes(self) -> None:
        statements: list[tuple[str, tuple]] = []
        def record(_conn, _cursor, statement, parameters, context, _many) -> None:
            if 'FROM orders' in statement and context.compiled is not None:
                context.compiled.statement.compile(dialect=postgresql.dialect())
                statements.append((statement, parameters))
        event.listen(self.engine.sync_engine, 'before_cursor_execute', record)
        try:
            await self.report()
        finally:
            event.remove(self.engine.sync_engine, 'before_cursor_execute', record)
        self.assertGreaterEqual(len(statements), 3)
        async with self.engine.connect() as connection:
            for statement, parameters in statements:
                plan = await connection.exec_driver_sql('EXPLAIN QUERY PLAN ' + statement, parameters)
                descriptions = [row[-1] for row in plan]
                self.assertTrue(any('SEARCH orders USING INDEX' in line for line in descriptions), descriptions)

    def app(self, staff: dict) -> FastAPI:
        from app.api.app import render
        app = FastAPI()
        async def require_staff() -> dict:
            return staff
        async def get_db():
            async with self.Session() as session:
                yield session
        register_finance_pages(app, render=render, require_staff=require_staff, get_db=get_db)
        return app

    async def page(self, query: dict[str, str], *, staff: dict | None = None) -> httpx.Response:
        identity = staff if staff is not None else {
            'role': 'admin', 'user_id': 1, 'bot_user_id': 1,
            'org_principal_id': 1, 'org_depth': 0, 'org_status': 'active',
        }
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app(identity)), base_url='http://test') as client:
            return await client.get('/finance', params={'tab': 'reports', **query})

    async def test_custom_range_renders_summary_chart_tables_and_filter_values(self) -> None:
        response = await self.page({'period': 'custom', 'start': START, 'end': END})
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="' + START + '"', response.text)
        self.assertIn('روند فروش موفق', response.text)
        self.assertIn('3,000', response.text)
        self.assertIn('viewBox="-12 -12 824 224"', response.text)
        self.assertIn('وضعیت فعلی فروشگاه', response.text)
        filters = FinanceFilterHtml(response.text)
        self.assertNotIn('hidden', filters.form)
        self.assertEqual(filters.toggle['aria-expanded'], 'true')

    async def test_preset_reports_hide_date_time_fields_until_custom_range_is_opened(self) -> None:
        for period in ('day', 'week', 'month'):
            with self.subTest(period=period):
                response = await self.page({'period': period})
                filters = FinanceFilterHtml(response.text)
                self.assertIn('hidden', filters.form)
                self.assertEqual(filters.toggle['aria-expanded'], 'false')
                self.assertEqual(filters.toggle['aria-controls'], filters.form['id'])

    async def test_invalid_filter_shows_safe_error_without_misleading_default_report(self) -> None:
        response = await self.page({'period': 'custom', 'start': '<script>alert(1)</script>', 'end': END})
        self.assertEqual(response.status_code, 200)
        self.assertIn('role="alert"', response.text)
        self.assertIn('&lt;script&gt;', response.text)
        self.assertNotIn('<script>alert(1)</script>', response.text)
        self.assertNotIn('id="finance-summary-title"', response.text)

    async def test_custom_web_report_keeps_shop_from_authenticated_identity(self) -> None:
        staff = {'role': 'reseller', 'user_id': SHOP, 'bot_user_id': SHOP,
                 'permissions': ['orders', 'payments'], 'reseller_id': SHOP}
        response = await self.page({'period': 'custom', 'start': START, 'end': END, 'reseller_id': str(OTHER_SHOP)}, staff=staff)
        self.assertEqual(response.status_code, 200)
        self.assertIn('18,000', response.text)
        self.assertNotIn('9,137', response.text)

    async def test_missing_permission_never_builds_report(self) -> None:
        with patch('app.api.finance_report_context.build_finance_dashboard') as build:
            response = await self.page({'period': 'custom', 'start': START, 'end': END}, staff={'role': 'pg_staff'})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers['location'], '/home')
        build.assert_not_called()

    async def test_unresolved_shop_never_falls_back_to_platform(self) -> None:
        with patch('app.api.finance_report_context.build_finance_dashboard') as build:
            response = await self.page({}, staff={'role': 'reseller', 'permissions': ['orders']})
        self.assertEqual(response.status_code, 200)
        self.assertIn('محدوده فروشگاه مشخص نیست', response.text)
        build.assert_not_called()

    async def test_report_is_read_only_and_repeatable(self) -> None:
        first, second = await self.report(), await self.report()
        self.assertEqual(first['active'], second['active'])
        async with self.Session() as session:
            orders = list(await session.scalars(select(Order)))
        self.assertEqual(len(orders), 12)
        self.assertEqual(sum(order.amount for order in orders if order.status == 'pending'), 200)


if __name__ == '__main__':
    unittest.main()
