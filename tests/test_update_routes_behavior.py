"""What the Owner's panel-update routes decide: when an update starts, which version it targets,
and when clearing a stuck run or switching channel is refused."""

from __future__ import annotations

import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from tests.test_update_owner_only import OWNER_SESSION, RoutesHarness

NO_UPDATE = {"update_available": False, "remote_version": "1.0.0"}
NEWER = {"update_available": True, "remote_version": "2.0.0"}
PREFLIGHT_OK = {"blocked": False}


class UpdateRoutesHarness(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.check = AsyncMock(return_value=NEWER)
        self.preflight = AsyncMock(return_value=PREFLIGHT_OK)
        self.clear_idle = MagicMock(return_value={"state": "idle"})
        self.read_status = MagicMock(return_value={"state": "idle"})
        for target, value in (
            ("app.services.updates.check_github_update", self.check),
            ("app.services.updates.clear_update_cache", MagicMock()),
            ("app.services.update_channel.get_update_channel", MagicMock(return_value="main")),
            ("app.services.update_channel.migration_preflight", self.preflight),
            ("app.services.panel_update.clear_idle_status", self.clear_idle),
            ("app.services.panel_update.read_status", self.read_status),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def post(self, path: str, body: dict) -> httpx.Response:
        cookie = f"session={self.signer.dumps(OWNER_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        return await self.client.post(path, headers=headers, json=body)


class UpdateStartTests(UpdateRoutesHarness):
    async def test_a_newer_release_starts_an_update_to_that_version(self):
        response = await self.post("/update/start", {})
        self.assertEqual(response.json(), {"ok": True})
        self.start_update.assert_called_once_with(target_version="2.0.0")

    async def test_a_stale_client_target_is_replaced_by_the_fresh_remote_version(self):
        await self.post("/update/start", {"target": "1.5.0"})
        self.start_update.assert_called_once_with(target_version="2.0.0")

    async def test_nothing_starts_when_there_is_no_newer_release(self):
        self.check.return_value = NO_UPDATE
        response = await self.post("/update/start", {})
        self.assertFalse(response.json()["ok"])
        self.assertIn("نسخه جدیدی", response.json()["error"])
        self.start_update.assert_not_called()

    async def test_a_retry_after_an_error_may_rerun_the_same_version(self):
        self.check.return_value = NO_UPDATE
        self.read_status.return_value = {"state": "error"}
        await self.post("/update/start", {})
        self.start_update.assert_called_once_with(target_version="1.0.0")

    async def test_a_forced_retry_clears_the_leftover_status_and_keeps_the_chosen_target(self):
        self.check.return_value = NO_UPDATE
        await self.post("/update/start", {"force": True, "target": "1.0.0"})
        self.clear_idle.assert_called_once_with()
        self.start_update.assert_called_once_with(target_version="1.0.0")

    async def test_an_incompatible_migration_on_the_target_channel_blocks_the_update(self):
        self.preflight.return_value = {"blocked": True, "message": "مایگریشن ناسازگار"}
        response = await self.post("/update/start", {"force": True})
        self.assertEqual(response.json()["error"], "مایگریشن ناسازگار")
        self.start_update.assert_not_called()


class UpdateClearAndChannelTests(UpdateRoutesHarness):
    async def test_a_running_update_with_a_live_worker_is_not_cleared(self):
        with patch("app.services.panel_update.resolve_stale_update_status", return_value={"state": "running"}), \
                patch("app.services.panel_update._thread_alive", return_value=True):
            response = await self.post("/update/clear", {})
        self.assertFalse(response.json()["ok"])
        self.clear_idle.assert_not_called()

    async def test_a_run_whose_worker_died_is_cleared(self):
        with patch("app.services.panel_update.resolve_stale_update_status", return_value={"state": "running"}), \
                patch("app.services.panel_update._thread_alive", return_value=False):
            response = await self.post("/update/clear", {})
        self.assertEqual(response.json(), {"ok": True, "status": {"state": "idle"}})

    async def test_a_missing_channel_is_refused_without_saving(self):
        with patch("app.services.update_channel.set_update_channel") as set_channel:
            response = await self.post("/update/channel", {"channel": "  "})
        self.assertFalse(response.json()["ok"])
        set_channel.assert_not_called()

    async def test_choosing_a_channel_saves_it_and_rechecks_that_channel(self):
        context = AsyncMock(return_value={"update_channel_label": "توسعه", "can_start_update": True})
        with patch("app.services.update_channel.set_update_channel", return_value="dev") as set_channel, \
                patch("app.services.panel_update.update_page_context", context):
            response = await self.post("/update/channel", {"channel": "dev"})
        set_channel.assert_called_once_with("dev")
        context.assert_awaited_once_with(force_check=True)
        body = response.json()
        self.assertEqual((body["ok"], body["channel"], body["channel_label"]), (True, "dev", "توسعه"))
