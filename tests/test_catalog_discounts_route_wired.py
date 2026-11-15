"""``/catalog-discounts`` must be registered on the panel app (marketing tab target)."""

from __future__ import annotations

import unittest


class CatalogDiscountsRouteWiredTests(unittest.TestCase):
    def test_create_api_app_exposes_catalog_discounts_routes(self) -> None:
        from app.api.app import create_api_app

        paths = {getattr(route, "path", "") for route in create_api_app().routes}
        self.assertIn("/catalog-discounts", paths)
        self.assertIn("/catalog-discounts/{discount_id}/stop", paths)


if __name__ == "__main__":
    unittest.main()
