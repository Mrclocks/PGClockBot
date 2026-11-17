"""Templates and groups offered on the plan forms."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from app.services.pasarguard import PasarGuardError
from app.services.plans_catalog import PG_UNREACHABLE_MESSAGE, load_pg_plan_options

FULL = [{"id": 1, "name": "Monthly", "data_limit": 10}]
SIMPLE = [{"id": 1, "name": "Monthly"}, {"id": 2, "name": "Weekly"}]
GROUPS = [{"id": 5, "name": "Main"}]


def _panel(*, full: object, simple: object, groups: object) -> MagicMock:
    def reader(result: object) -> AsyncMock:
        if isinstance(result, BaseException):
            return AsyncMock(side_effect=result)
        return AsyncMock(return_value=result)

    pg = MagicMock()
    pg.ensure_token = AsyncMock(return_value="tok")
    pg.get_user_templates = reader(full)
    pg.get_user_templates_simple = reader(simple)
    pg.get_groups_simple = reader(groups)
    return pg


class PlanCatalogOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def _load(self, pg: MagicMock) -> tuple[list[dict], list[dict], str | None]:
        with patch("app.services.plans_catalog.get_pg", return_value=pg):
            return await load_pg_plan_options({"role": "admin"}, session=None)

    async def test_read_simple_only_role_still_gets_the_simple_templates(self) -> None:
        pg = _panel(full=PasarGuardError("forbidden", 403), simple=SIMPLE, groups=GROUPS)
        templates, groups, err = await self._load(pg)
        self.assertEqual(templates, SIMPLE)
        self.assertEqual(groups, GROUPS)
        self.assertIsNone(err)

    async def test_full_templates_are_preferred_without_a_second_read(self) -> None:
        pg = _panel(full=FULL, simple=SIMPLE, groups=GROUPS)
        templates, _groups, err = await self._load(pg)
        self.assertEqual(templates, FULL)
        self.assertIsNone(err)
        pg.get_user_templates_simple.assert_not_awaited()

    async def test_wrapped_full_list_is_unwrapped(self) -> None:
        pg = _panel(full={"templates": FULL, "total": 1}, simple=SIMPLE, groups=GROUPS)
        templates, _groups, _err = await self._load(pg)
        self.assertEqual(templates, FULL)

    async def test_empty_full_list_falls_back_to_the_simple_list(self) -> None:
        pg = _panel(full=[], simple=SIMPLE, groups=GROUPS)
        templates, _groups, err = await self._load(pg)
        self.assertEqual(templates, SIMPLE)
        self.assertIsNone(err)

    async def test_role_without_catalog_access_is_not_a_disconnect(self) -> None:
        denied = PasarGuardError("forbidden", 403)
        pg = _panel(full=denied, simple=denied, groups=denied)
        templates, groups, err = await self._load(pg)
        self.assertEqual((templates, groups, err), ([], [], None))

    async def test_rejected_credentials_are_reported_instead_of_an_empty_catalog(self) -> None:
        rejected = PasarGuardError("SECRET_HOST unauthorized", 401)
        pg = _panel(full=rejected, simple=rejected, groups=rejected)
        templates, groups, err = await self._load(pg)
        self.assertEqual((templates, groups), ([], []))
        self.assertEqual(err, PG_UNREACHABLE_MESSAGE)

    async def test_group_outage_is_reported_and_templates_still_load(self) -> None:
        pg = _panel(full=FULL, simple=SIMPLE, groups=httpx.ConnectError("10.0.0.5:8000 refused"))
        templates, groups, err = await self._load(pg)
        self.assertEqual(templates, FULL)
        self.assertEqual(groups, [])
        self.assertEqual(err, PG_UNREACHABLE_MESSAGE)
        self.assertNotIn("10.0.0.5", err)


if __name__ == "__main__":
    unittest.main()
