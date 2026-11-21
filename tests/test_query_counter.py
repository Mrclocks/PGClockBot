"""The query profile counts only the measured unit of work and finds statements that repeat or grow."""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from scripts.query_counter import QueryRecorder, merge_runs, statement_shape
from scripts.query_profile_report import growth_section, repeated_section
from tests.postgres_support import scoped_async_engine


class StatementShapeTests(unittest.TestCase):
    def test_parameters_lists_and_numbers_become_one_shape(self) -> None:
        one = statement_shape("SELECT * FROM orders\n WHERE id IN ($1, $2, $3) LIMIT 20")
        other = statement_shape("SELECT * FROM orders WHERE id IN ($1) LIMIT 50")
        self.assertEqual(one, other)
        self.assertEqual(one, "SELECT * FROM orders WHERE id IN (?) LIMIT N")

    def test_statements_that_differ_only_after_the_columns_stay_apart(self) -> None:
        columns = ", ".join(f"orders.col{n}" for n in range(30))
        by_id = statement_shape(f"SELECT {columns} FROM orders WHERE orders.id = $1")
        by_user = statement_shape(f"SELECT {columns} FROM orders WHERE orders.user_id = $1")
        self.assertNotEqual(by_id, by_user)

    def test_names_with_digits_are_kept(self) -> None:
        self.assertIn("table2", statement_shape("SELECT a FROM table2 WHERE b = $1"))


class RecorderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine(tables=False)
        self.addAsyncCleanup(self.engine.dispose)
        self.recorder = QueryRecorder(self.engine)
        self.addCleanup(self.recorder.close)

    async def _select(self, value: int) -> None:
        async with self.engine.connect() as conn:
            await conn.execute(text("SELECT CAST(:v AS integer)"), {"v": value})

    async def test_a_query_per_row_is_reported_as_repeated(self) -> None:
        with self.recorder.measure("page") as tally:
            for value in range(6):
                await self._select(value)
        self.assertEqual(tally.statements, 6)
        self.assertEqual(tally.as_dict()["max_repeat"], 6)
        self.assertEqual(len(tally.repeated()), 1)
        self.assertGreater(tally.db_seconds, 0)

    async def test_a_statement_records_the_app_line_that_ran_it(self) -> None:
        from app.services.users import get_all_settings

        engine = scoped_async_engine()
        self.addAsyncCleanup(engine.dispose)
        self.recorder.attach(engine)
        async with async_sessionmaker(engine)() as session:
            with self.recorder.measure("page") as tally:
                await get_all_settings(session)
        self.assertTrue(any(origin.startswith("app/services/users.py:") for origin in tally.origins.values()),
                        tally.origins)

    async def test_work_outside_the_measured_block_is_not_counted(self) -> None:
        release = asyncio.Event()

        async def background_job() -> None:
            await release.wait()
            await self._select(1)

        job = asyncio.create_task(background_job())  # started before the measurement, like a scheduler job
        with self.recorder.measure("page") as tally:
            await self._select(2)
            release.set()
            await job
        self.assertEqual(tally.statements, 1)

    async def test_tasks_the_measured_code_starts_are_counted(self) -> None:
        with self.recorder.measure("page") as tally:
            await asyncio.gather(self._select(1), self._select(2))
        self.assertEqual(tally.statements, 2)

    async def test_nothing_is_counted_after_close(self) -> None:
        self.recorder.close()
        with self.recorder.measure("page") as tally:
            await self._select(1)
        self.assertEqual(tally.statements, 0)


def _row(label: str, statements: int, *, scale: int = 0, repeated: list | None = None) -> dict:
    top = [{"shape": "SELECT orders", "count": statements, "origin": "app/api/user_pages.py:9 in users"}]
    return {"label": label, "statements": statements, "db_ms": 1.0, "repeated": repeated or [], "scale": scale,
            "top": top}


class ReportTests(unittest.TestCase):
    def test_runs_of_one_label_keep_the_heaviest(self) -> None:
        (merged,) = merge_runs([_row("GET /users/{id}", 4), _row("GET /users/{id}", 9), _row("GET /users/{id}", 5)])
        self.assertEqual((merged["statements"], merged["runs"]), (9, 3))

    def test_a_unit_whose_count_grows_with_the_data_is_listed(self) -> None:
        rows = [_row("GET /users", 5), _row("GET /users", 205, scale=200),
                _row("GET /plans", 3), _row("GET /plans", 3, scale=200)]
        section = growth_section(rows)
        self.assertIn("`GET /users` | 5 | 205 | +200", section)
        self.assertIn("+200× `app/api/user_pages.py:9 in users`", section)
        self.assertNotIn("GET /plans", section)

    def test_a_few_extra_statements_are_not_reported_as_work_per_row(self) -> None:
        small = _row("GET /settings", 7, scale=20)
        cache_expired = _row("GET /settings", 9, scale=200)
        section = growth_section([small, cache_expired])
        self.assertIn("None: no statement runs once per row", section)
        self.assertIn("1 more units differ", section)

    def test_the_cheapest_run_of_each_url_is_compared(self) -> None:
        cold = {**_row("GET /settings", 18, scale=200), "url": "/settings?tab=general"}
        warm = {**_row("GET /settings", 7, scale=200), "url": "/settings?tab=general"}
        small = {**_row("GET /settings", 7, scale=20), "url": "/settings?tab=general"}
        self.assertIn("None", growth_section([small, cold, warm]))

    def test_one_scale_gives_no_growth_section(self) -> None:
        self.assertEqual(growth_section([_row("GET /users", 5)]), "")

    def test_repeated_statements_are_listed_most_frequent_first(self) -> None:
        rows = [_row("a", 9, repeated=[{"shape": "SELECT x", "count": 6}]),
                _row("b", 20, repeated=[{"shape": "SELECT y", "count": 12}])]
        section = repeated_section(rows)
        self.assertLess(section.index("SELECT y"), section.index("SELECT x"))


if __name__ == "__main__":
    unittest.main()
