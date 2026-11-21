"""Tests for host metrics and overall home overview helpers."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.host_metrics import cpu_percent, format_bytes_short, host_metrics, memory_stats
from app.services.home_overview import _node_tone, _tone_class, check_bot_connection


class HostMetricsTests(unittest.TestCase):
    def test_memory_stats_linux(self):
        mem = memory_stats()
        self.assertIsNotNone(mem)
        assert mem is not None
        self.assertGreater(mem["total"], 0)
        self.assertGreaterEqual(mem["percent"], 0)
        self.assertLessEqual(mem["percent"], 100)

    def test_cpu_percent_samples(self):
        pct = cpu_percent(wait_sec=0.08)
        self.assertIsNotNone(pct)
        assert pct is not None
        self.assertGreaterEqual(pct, 0)
        self.assertLessEqual(pct, 100)

    def test_host_metrics_shape(self):
        snap = host_metrics(wait_cpu=0.05)
        self.assertTrue(snap.get("ok"))
        self.assertIn("memory_used_text", snap)
        self.assertIn("cpu_percent", snap)

    def test_format_bytes_short(self):
        self.assertEqual(format_bytes_short(512), "512 بایت")
        self.assertIn("کیلوبایت", format_bytes_short(2048))
        self.assertEqual(format_bytes_short(None), "نامحدود")


class HomeOverviewHelpersTests(unittest.IsolatedAsyncioTestCase):
    def test_node_tone(self):
        self.assertEqual(_node_tone("connected"), "ok")
        self.assertEqual(_node_tone("connecting"), "warn")
        self.assertEqual(_node_tone("offline"), "err")
        self.assertEqual(_node_tone("disconnected"), "err")
        self.assertEqual(_node_tone(""), "neutral")

    def test_tone_class(self):
        self.assertEqual(_tone_class(10), "ok")
        self.assertEqual(_tone_class(80), "warn")
        self.assertEqual(_tone_class(95), "err")
        self.assertEqual(_tone_class(None), "neutral")

    async def test_bot_connection_missing_token(self):
        st = await check_bot_connection("")
        self.assertFalse(st["ok"])
        self.assertIn("توکن", st["error"])
        self.assertIsNone(st.get("username"))

    async def test_bot_connection_never_falls_back_to_platform_token(self):
        """Empty/missing token must NOT probe platform BOT_TOKEN (tenant isolation)."""
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get = AsyncMock()
        with (
            patch(
                "app.services.home_overview.current_setup_values",
                return_value={"BOT_TOKEN": "999999:PLATFORM_MAIN_TOKEN"},
            ),
            patch("app.services.home_overview.httpx.AsyncClient", return_value=mock_client),
        ):
            st_empty = await check_bot_connection("")
            st_none = await check_bot_connection(None)
        self.assertFalse(st_empty["ok"])
        self.assertFalse(st_none["ok"])
        self.assertIsNone(st_empty.get("username"))
        self.assertIsNone(st_none.get("username"))
        mock_client.get.assert_not_called()

    async def test_bot_connection_ok(self):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "ok": True,
            "result": {"username": "demo_bot", "first_name": "Demo", "id": 1},
        }
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("app.services.home_overview.httpx.AsyncClient", return_value=mock_client):
            st = await check_bot_connection("123:ABC")
        self.assertTrue(st["ok"])
        self.assertEqual(st["username"], "demo_bot")
        mock_client.get.assert_awaited()
        called_url = mock_client.get.await_args.args[0]
        self.assertIn("123:ABC", called_url)
        self.assertNotIn("PLATFORM", called_url)


if __name__ == "__main__":
    unittest.main()
