"""When PasarGuard does not answer, «سرویس من» shows the last saved expiry and volume.

The saved values are labelled as possibly outdated, the status stays unknown,
usage is never invented, and nothing is written back as live data.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.formatting import service_card
from app.services.service_live_info import service_info_error, with_stored_quota

GB = 1024**3


def _service(**overrides) -> SimpleNamespace:
    values = {
        "id": 7,
        "pg_username": "user7",
        "quota_synced_at": datetime(2026, 10, 1, 8, 30, tzinfo=timezone.utc),
        "quota_expire_at": datetime.now(timezone.utc) + timedelta(days=10),
        "quota_data_limit_bytes": 50 * GB,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class WithStoredQuotaTests(unittest.TestCase):
    def test_timeout_uses_saved_expiry_and_volume_limit(self) -> None:
        svc = _service()
        info = with_stored_quota(service_info_error("timeout"), svc)
        self.assertEqual(info["expire"], int(svc.quota_expire_at.timestamp()))
        self.assertEqual(info["stored_data_limit"], 50 * GB)
        self.assertEqual(info["stored_at"], svc.quota_synced_at.isoformat())
        self.assertNotIn("status", info)
        self.assertNotIn("used_traffic", info)
        self.assertEqual(info["error_code"], "timeout")

    def test_unreachable_panel_also_uses_saved_values(self) -> None:
        info = with_stored_quota(service_info_error("upstream_unavailable"), _service())
        self.assertIn("stored_at", info)

    def test_definite_panel_answers_never_fall_back(self) -> None:
        for code in ("subscription_not_found", "access_denied", "service_mismatch", "invalid_response"):
            with self.subTest(code=code):
                info = service_info_error(code)
                self.assertIs(with_stored_quota(info, _service()), info)

    def test_live_answer_is_returned_unchanged(self) -> None:
        live = {"status": "active", "expire": 0, "data_limit": 0, "used_traffic": 5}
        self.assertIs(with_stored_quota(live, _service()), live)

    def test_never_synced_service_has_nothing_to_show(self) -> None:
        info = service_info_error("timeout")
        self.assertIs(with_stored_quota(info, _service(quota_synced_at=None)), info)

    def test_synced_without_expiry_means_unlimited(self) -> None:
        info = with_stored_quota(service_info_error("timeout"), _service(quota_expire_at=None, quota_data_limit_bytes=0))
        self.assertEqual(info["expire"], 0)
        self.assertEqual(info["stored_data_limit"], 0)

    def test_naive_database_times_are_read_as_utc(self) -> None:
        svc = _service(
            quota_synced_at=datetime(2026, 10, 1, 8, 30),
            quota_expire_at=datetime(2026, 12, 1, 0, 0),
        )
        info = with_stored_quota(service_info_error("timeout"), svc)
        self.assertEqual(info["expire"], int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()))
        self.assertTrue(info["stored_at"].endswith("+00:00"))

    def test_invalid_saved_limit_is_ignored(self) -> None:
        for limit in (None, -1, True):
            with self.subTest(limit=limit):
                info = with_stored_quota(service_info_error("timeout"), _service(quota_data_limit_bytes=limit))
                self.assertNotIn("stored_data_limit", info)


class StoredServiceCardTests(unittest.TestCase):
    def _card(self, **service) -> str:
        info = with_stored_quota(service_info_error("timeout"), _service(**service))
        info["username"] = "user7"
        return service_card(info)

    def test_card_labels_saved_values_and_keeps_status_unknown(self) -> None:
        card = self._card()
        self.assertIn("نامشخص", card)
        self.assertIn("مصرف نامشخص از", card)
        self.assertIn("ممکن است به‌روز نباشد", card)
        self.assertIn("پنل در زمان مقرر پاسخ نداد", card)
        self.assertNotIn("نامحدود", card)

    def test_card_shows_saved_unlimited_service(self) -> None:
        card = self._card(quota_expire_at=None, quota_data_limit_bytes=0)
        self.assertIn("مصرف نامشخص از نامحدود", card)

    def test_live_card_has_no_saved_values_note(self) -> None:
        card = service_card({"username": "u", "status": "active", "expire": 0, "data_limit": 0, "used_traffic": 0})
        self.assertNotIn("ممکن است به‌روز نباشد", card)


class ServiceViewHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_values_are_shown_but_not_synced_as_live(self) -> None:
        from app.bot.handlers import services as handlers

        svc = _service()
        session = MagicMock()
        session.get = AsyncMock(return_value=svc)
        session.commit = AsyncMock()
        callback = MagicMock()
        callback.data = "svc:view:7"
        callback.answer = AsyncMock()
        sync = MagicMock()
        edit = AsyncMock()
        with (
            patch.object(handlers, "get_all_settings", new=AsyncMock(return_value={})),
            patch.object(handlers, "customer_may_use_service", new=AsyncMock(return_value=True)),
            patch.object(handlers, "fetch_live_service_info", new=AsyncMock(return_value=service_info_error("timeout"))),
            patch.object(handlers, "safe_edit_text", new=edit),
            patch("app.bot.nav_inline.service_card_keyboard", return_value=None),
            patch("app.services.bot_user_admin.sync_service_quota_cache", new=sync),
        ):
            await handlers.svc_view(callback, session, SimpleNamespace(id=1))
        sync.assert_not_called()
        session.commit.assert_not_awaited()
        text = edit.await_args.args[1]
        self.assertIn("ممکن است به‌روز نباشد", text)


if __name__ == "__main__":
    unittest.main()
