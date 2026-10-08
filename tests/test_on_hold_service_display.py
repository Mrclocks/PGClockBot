"""Display the actual PasarGuard pending limits; missing info is not unlimited."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.api.miniapp_pages import _enrich_services, _fetch_pg_info, _serialize_service
from app.services.formatting import (
    hold_duration_from_info,
    on_hold_expire_duration_seconds,
    pg_expire_fields,
    service_card,
    status_label,
    status_label_plain,
)

GB = 1024**3


@pytest.fixture
def pending_info():
    # Sanitized shape of the reported /sub/{token}/info response. No credentials.
    return {
        "id": 123,
        "username": "pending_user",
        "status": "on_hold",
        "expire": None,
        "data_limit": 30 * GB,
        "used_traffic": 0,
        "on_hold_expire_duration": 60 * 86400,
        "on_hold_timeout": None,
        "online_at": None,
    }


@pytest.fixture
def service():
    return SimpleNamespace(
        id=7,
        bot_user_id=1,
        pg_user_id=123,
        pg_username="pending_user",
        plan_id=2,
        subscription_token="test-token",
        subscription_url="https://pg.example/sub/test-token",
        plan=SimpleNamespace(name="Template", duration_days=30, data_limit_gb=10),
    )


def test_reported_payload_in_miniapp(service, pending_info):
    out = _serialize_service(service, pending_info)
    assert out["status_fa"] == "در انتظار اتصال"
    assert out["pending_start"] is True
    assert out["traffic"] == "0 از 30 گیگ"
    assert out["traffic_pct"] == 0
    assert out["expire_days"] == 60
    assert out["expire_days_label"] == "60 روز (پس از اتصال)"
    assert out["expire"] == "60 روز · در انتظار اتصال"
    assert out["error"] is None
    assert "نامحدود" not in str(out)


def test_bot_card_and_qr_show_received_limits(pending_info):
    from app.services.notifications import build_qr_caption

    card = service_card(pending_info)
    caption, _ = build_qr_caption(
        sub_url="https://pg.example/sub/test-token", info=pending_info,
    )
    for text in (card, caption):
        assert "در انتظار اتصال" in text
        assert "0 از 30 گیگ" in text
        assert "60 روز (پس از اتصال)" in text
        assert "نامحدود" not in text


def test_pending_duration_does_not_count_down(pending_info):
    for created_at in ("2026-01-01T00:00:00Z", "2026-10-07T17:03:19Z"):
        fields = pg_expire_fields({**pending_info, "created_at": created_at})
        assert fields["days_left"] == 60
        assert fields["time_label"] == "60 روز (پس از اتصال)"


@pytest.mark.parametrize("status", ["on_hold", "ON_HOLD", "on-hold", "onhold"])
def test_pending_status_labels(status):
    assert status_label_plain(status) == "در انتظار اتصال"
    assert status_label(status) == "🟡 در انتظار اتصال"


@pytest.mark.parametrize("fields", [
    {"on_hold_expire_duration": 60 * 86400, "expire_duration": 30 * 86400},
    {"on_hold_expire_duration": "5184000", "expire_duration": 0},
    {"on_hold_expire_duration": 0, "expire_duration": "5184000"},
    {"on_hold_expire_duration": None, "expire_duration": "invalid", "hold_expire_duration": 60 * 86400},
    {"expire_duration": float("inf"), "hold_expire_duration": 60 * 86400},
])
def test_duration_aliases_skip_unset_or_invalid_values(fields):
    info = {"status": "on_hold", "expire": None, **fields}
    assert hold_duration_from_info(info) == 60 * 86400
    assert on_hold_expire_duration_seconds(info) == 60 * 86400
    assert pg_expire_fields(info)["days_left"] == 60


@pytest.mark.parametrize("info", [None, {}, {"error": "upstream_unavailable"}])
def test_absent_info_never_implies_unlimited(service, info):
    out = _serialize_service(service, info)
    assert out["traffic"] == "—"
    assert out["expire_days"] is None
    assert out["expire_days_label"] == "—"
    assert out["error"] == "upstream_unavailable"
    assert "نامحدود" not in str(out)


def test_partial_active_info_does_not_invent_limits(service):
    out = _serialize_service(service, {"status": "active", "used_traffic": 0})
    assert out["traffic"] == "—"
    assert out["expire_days_label"] == "—"


def test_pending_unknown_duration_is_pending(service):
    out = _serialize_service(service, {"status": "on_hold", "expire": None})
    assert out["status_fa"] == "در انتظار اتصال"
    assert out["traffic"] == "—"
    assert out["expire_days"] is None
    assert out["expire_days_label"] == "پس از اتصال"
    assert "نامحدود" not in str(out)


@pytest.mark.parametrize("unset", [0, None])
def test_explicit_unlimited_active_time_remains_unlimited(service, unset):
    out = _serialize_service(service, {"status": "active", "expire": unset, "data_limit": unset})
    assert out["pending_start"] is False
    assert out["expire_days_label"] == "نامحدود"


def test_miniapp_summary_does_not_expose_raw_upstream_fields(service, pending_info):
    info = {**pending_info, "proxy_settings": {"example": "private-value"}, "ip": "192.0.2.1"}
    out = _serialize_service(service, info)
    for key in ("proxy_settings", "ip", "subscription_token", "on_hold_timeout"):
        assert key not in out
    assert "private-value" not in str(out)


def test_fetch_and_enrich_reported_payload(service, pending_info):
    import asyncio

    pg = SimpleNamespace(subscription_info=AsyncMock(return_value=pending_info))
    with patch("app.api.miniapp_pages.get_pg", return_value=pg):
        out = asyncio.run(_enrich_services([service]))
    pg.subscription_info.assert_awaited_once_with(
        "test-token",
        subscription_url="https://pg.example/sub/test-token",
    )
    assert out[0]["status_fa"] == "در انتظار اتصال"
    assert out[0]["traffic"] == "0 از 30 گیگ"
    assert out[0]["expire_days"] == 60


@pytest.mark.parametrize("payload", [None, {}, [], "invalid"])
def test_empty_or_invalid_subscription_response_is_unavailable(payload):
    import asyncio

    pg = SimpleNamespace(subscription_info=AsyncMock(return_value=payload))
    with patch("app.api.miniapp_pages.get_pg", return_value=pg):
        out = asyncio.run(_fetch_pg_info("test-token"))
    assert out == {"error": "upstream_unavailable"}


def test_missing_token_never_falls_back_to_admin_read():
    import asyncio

    with patch("app.api.miniapp_pages.get_pg", side_effect=AssertionError("must not read panel")):
        out = asyncio.run(_fetch_pg_info(None))
    assert out == {"error": "upstream_unavailable"}


def test_unfetched_services_are_unknown_instead_of_unlimited(service, pending_info):
    import asyncio

    with patch("app.api.miniapp_pages._fetch_pg_info", new=AsyncMock(return_value=pending_info)):
        out = asyncio.run(_enrich_services([service] * 21))
    assert len(out) == 21
    assert out[20]["traffic"] == "—"
    assert out[20]["expire_days_label"] == "—"


def test_live_snapshot_prefers_template_response_over_plan(service, pending_info):
    import asyncio
    from app.services.bot_user_admin import service_snapshot

    pg = SimpleNamespace(get_user_by_id=AsyncMock(return_value=pending_info))
    with patch("app.services.bot_user_admin._pg_client_for_bot_service", new=AsyncMock(return_value=pg)):
        snapshot = asyncio.run(service_snapshot(AsyncMock(), service))
    assert snapshot.error is None
    assert snapshot.status_fa == "در انتظار اتصال"
    assert snapshot.days_left == 60
    assert snapshot.limit_text == "30 گیگ"
    assert snapshot.remain_gb_text == "30 گیگ"
    assert snapshot.time_label == "60 روز (پس از اتصال)"


def test_delivery_card_receives_pending_info(service, pending_info):
    import asyncio
    from app.services.delivery import build_delivery_content

    order = SimpleNamespace(id=11, service_id=7, plan_id=2, plan=service.plan,
                            reseller_id=None, quantity=1, note=None, subscription_url=None)
    pg = SimpleNamespace(subscription_info=AsyncMock(return_value=pending_info))
    session = AsyncMock()
    session.get.return_value = service
    with patch("app.services.delivery.get_pg", return_value=pg), patch(
        "app.services.delivery.get_all_settings", new=AsyncMock(return_value={})
    ), patch("app.services.delivery.kb.back_home", return_value=None), patch(
        "app.services.delivery.kb.service_actions", return_value=None
    ):
        out = asyncio.run(build_delivery_content(session, None, order))
    assert "در انتظار اتصال" in out["text"]
    assert "0 از 30 گیگ" in out["text"]
    assert "60 روز (پس از اتصال)" in out["text"]
