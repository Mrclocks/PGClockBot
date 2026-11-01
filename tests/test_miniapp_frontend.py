"""Runs the Mini App browser-behavior checks (escaping, double taps, lost responses, back)."""

from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser-behavior checks")
class MiniAppFrontendTests(unittest.TestCase):
    def run_node(self, name: str) -> None:
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests" / name)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_miniapp_behavior(self) -> None:
        self.run_node("miniapp_app.test.cjs")

    def test_wallet_top_up_behavior(self) -> None:
        self.run_node("miniapp_topup.test.cjs")

    def test_support_behavior(self) -> None:
        self.run_node("miniapp_support.test.cjs")

    def test_codes_behavior(self) -> None:
        self.run_node("miniapp_codes.test.cjs")

    def test_guides_behavior(self) -> None:
        self.run_node("miniapp_guides.test.cjs")

    def test_club_behavior(self) -> None:
        self.run_node("miniapp_club.test.cjs")

    def test_receipt_review_behavior(self) -> None:
        self.run_node("miniapp_review.test.cjs")

    def test_brand_and_shortcuts_behavior(self) -> None:
        self.run_node("miniapp_brand.test.cjs")

    def test_pull_to_refresh_and_badges_behavior(self) -> None:
        self.run_node("miniapp_pull_badges.test.cjs")

    def test_launch_without_the_telegram_sdk(self) -> None:
        self.run_node("miniapp_launch.test.cjs")


if __name__ == "__main__":
    unittest.main()
