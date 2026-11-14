"""Changed panel scripts must bypass immutable browser caches."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.services.panel_assets import build_asset_cache_key, get_panel_asset_cache_key


class PanelAssetCacheTests(unittest.TestCase):
    def test_changed_content_changes_cache_key_without_release_bump(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "panel.js"
            path.write_bytes(b"first script")
            original = build_asset_cache_key(path)
            path.write_bytes(b"updated script")
            updated = build_asset_cache_key(path)
        self.assertNotEqual(original, updated)

    def test_unknown_asset_does_not_read_arbitrary_files(self) -> None:
        with self.assertRaises(ValueError):
            get_panel_asset_cache_key("../.env")

    def test_money_and_list_editors_use_the_current_script_content(self) -> None:
        static_dir = Path(__file__).resolve().parents[1] / "app/web/static"
        for name in ("money-inputs.js", "list-inputs.js", "panel_ok_flash.js"):
            with self.subTest(script=name):
                self.assertEqual(get_panel_asset_cache_key(name), build_asset_cache_key(static_dir / name))


class StaticAssetUrlTests(unittest.TestCase):
    """Every stylesheet and script URL carries its content key, so an edit reaches cached browsers."""

    def test_stylesheets_and_scripts_use_their_content_key(self) -> None:
        from app.services.panel_assets import asset_version

        static_dir = Path(__file__).resolve().parents[1] / "app/web/static"
        for name in ("panel.css", "panel-ds.css", "panel-ds.js", "panel-search.js", "miniapp/app.js"):
            with self.subTest(asset=name):
                self.assertEqual(asset_version(name), build_asset_cache_key(static_dir / name))

    def test_unknown_names_fall_back_without_reading_files(self) -> None:
        from app.services.panel_assets import asset_version

        self.assertEqual(asset_version("../.env", fallback="0.0.0"), "0.0.0")

    def test_base_page_links_the_current_stylesheets(self) -> None:
        import os
        import re
        from types import SimpleNamespace

        os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        from app.api.app import templates
        from app.services.panel_assets import asset_version

        html = templates.env.get_template("login.html").render(request=SimpleNamespace(query_params={}, url=SimpleNamespace(path="/login"), cookies={}))
        for name in ("panel.css", "panel-ds.css"):
            self.assertIn(f"/static/{name}?v={asset_version(name)}", html)
        self.assertFalse(re.search(r"/static/[\w./-]+\.(css|js)\?v=0\.", html))
