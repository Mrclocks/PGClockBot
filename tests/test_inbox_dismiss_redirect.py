"""Hiding an inbox alert returns to the page it came from, query string intact."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit


from sqlalchemy import select

from app.db.models import PanelInboxDismissal
from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness


class InboxDismissRedirectTests(RoutesHarness):
    async def dismiss(self, **fields: str):
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post("/inbox/dismiss", headers=headers, data=fields)

    async def test_the_query_of_the_return_page_is_kept_and_the_notice_is_added(self):
        response = await self.dismiss(alert_key="update", mode="forever", return_to="/finance?tab=delivery")
        self.assertEqual(response.status_code, 303)
        target = urlsplit(response.headers["location"])
        query = parse_qs(target.query)
        self.assertEqual(target.path, "/finance")
        self.assertEqual(query["tab"], ["delivery"])
        self.assertEqual(query["ok"], ["اعلان مخفی شد"])

    async def test_forever_is_stored_as_forever(self):
        await self.dismiss(alert_key="update", mode="forever")
        async with self.engine.connect() as conn:
            from sqlalchemy.ext.asyncio import AsyncSession

            async with AsyncSession(bind=conn) as session:
                rows = (await session.scalars(select(PanelInboxDismissal))).all()
        self.assertEqual([(r.alert_key, r.mode) for r in rows], [("update", "forever")])

    async def test_an_invalid_choice_reports_an_error_on_the_same_page(self):
        response = await self.dismiss(alert_key="update", mode="whenever", return_to="/inbox?x=1")
        query = parse_qs(urlsplit(response.headers["location"]).query)
        self.assertEqual(query["x"], ["1"])
        self.assertIn("err", query)
