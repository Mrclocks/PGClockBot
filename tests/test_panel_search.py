"""Web panel search: only places the viewer may open, Persian folding, real links, login required."""

from __future__ import annotations

import os
import unittest
from urllib.parse import urlsplit

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_api_app
from app.api.panel_search_pages import register_panel_search_pages
from app.services.panel_nav import NavViewer
from app.services.panel_search import PANEL_SEARCH_LIMIT, panel_search, search_entries

OWNER = NavViewer(role="admin", perms=frozenset(), pg_perms=frozenset({"pg_users"}), is_owner=True,
                  can_add_representative=False, can_manage_representatives=True, marketing_hrefs=())
RESELLER = NavViewer(role="reseller", perms=frozenset({"dashboard", "orders", "shop_settings"}), pg_perms=frozenset(),
                     is_owner=False, can_add_representative=False, can_manage_representatives=False, marketing_hrefs=())
NO_SETTINGS = NavViewer(role="reseller", perms=frozenset({"dashboard"}), pg_perms=frozenset(), is_owner=False,
                        can_add_representative=False, can_manage_representatives=False, marketing_hrefs=())


def hrefs(viewer: NavViewer, query: str) -> list[str]:
    return [h.href for h in panel_search(viewer, query)]


class PanelSearchScopeTests(unittest.TestCase):
    def test_owner_finds_card_to_card_settings_in_bot_settings(self) -> None:
        self.assertTrue(any(h.startswith("/settings?tab=") for h in hrefs(OWNER, "کارت")))

    def test_payment_settings_open_the_finance_settings_modal(self) -> None:
        self.assertIn("/finance?tab=orders&settings=payment", hrefs(OWNER, "تأیید خودکار کارت"))

    def test_modal_settings_need_the_page_that_hosts_the_modal(self) -> None:
        links = [e.section_id for e in search_entries(NO_SETTINGS)]
        self.assertFalse(any(h.startswith(("/finance", "/tickets", "/loyalty")) for h in links))

    def test_reseller_never_gets_owner_settings(self) -> None:
        links = [e.section_id for e in search_entries(RESELLER)]
        self.assertTrue(any(h.startswith("/shop-settings") for h in links))
        self.assertFalse(any(urlsplit(h).path == "/settings" for h in links))

    def test_no_settings_results_without_the_settings_page(self) -> None:
        links = [e.section_id for e in search_entries(NO_SETTINGS)]
        self.assertFalse(any(h.startswith(("/settings", "/shop-settings")) for h in links))

    def test_arabic_letters_and_joiners_match_persian_text(self) -> None:
        self.assertEqual(hrefs(OWNER, "كارت"), hrefs(OWNER, "کارت"))

    def test_results_are_bounded_and_unique(self) -> None:
        hits = panel_search(OWNER, "ت")
        self.assertLessEqual(len(hits), PANEL_SEARCH_LIMIT)
        self.assertEqual(len({(h.label, h.href) for h in hits}), len(hits))

    def test_empty_query_returns_nothing(self) -> None:
        self.assertEqual(panel_search(OWNER, "   "), [])


class PanelSearchLinkTests(unittest.TestCase):
    def test_every_result_link_is_a_panel_route(self) -> None:
        app = create_api_app()
        paths = {getattr(r, "path", "") for r in app.routes}
        for viewer in (OWNER, RESELLER):
            for entry in search_entries(viewer):
                path = urlsplit(entry.section_id).path
                self.assertTrue(path.startswith("/") and not path.startswith("//"), entry.section_id)
                self.assertTrue(path in paths or path.rstrip("/") in paths or f"{path.rstrip('/')}/" in paths,
                                entry.section_id)


class PanelSearchRouteTests(unittest.TestCase):
    def test_anonymous_request_gets_no_results(self) -> None:
        client = TestClient(create_api_app())
        res = client.get("/panel-search", params={"q": "کارت"}, follow_redirects=False)
        self.assertNotEqual(res.status_code, 200)

    def test_overlong_query_is_rejected(self) -> None:
        client = TestClient(create_api_app())
        res = client.get("/panel-search", params={"q": "x" * 200}, follow_redirects=False)
        self.assertNotEqual(res.status_code, 200)

    def test_signed_in_reseller_gets_json_without_owner_settings(self) -> None:
        app = FastAPI()

        async def reseller_staff() -> dict:
            return {"role": "reseller", "username": "shop", "permissions": ["dashboard", "shop_settings"]}

        register_panel_search_pages(app, require_staff=reseller_staff)
        res = TestClient(app).get("/panel-search", params={"q": "کارت"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["cache-control"], "no-store")
        results = res.json()["results"]
        self.assertTrue(results)
        self.assertEqual(set(results[0]), {"label", "where", "href"})
        self.assertFalse(any(urlsplit(r["href"]).path == "/settings" for r in results))


if __name__ == "__main__":
    unittest.main()
