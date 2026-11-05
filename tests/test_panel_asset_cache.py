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
