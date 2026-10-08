"""Custom subscription routes, fresh customer reads, and safe panel settings."""

import asyncio
import json
import socket
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from starlette.datastructures import FormData

from app.api.miniapp_pages import _enrich_services, _serialize_service
from app.config import Settings, normalize_pg_subscription_path
from app.services.formatting import service_card
from app.services.pasarguard import (
    PasarGuardClient,
    PasarGuardError,
    extract_sub_token,
    subscription_path_from_url,
)
from app.services.service_live_info import (
    fetch_live_service_info,
    validate_service_info,
)

GB = 1024**3


@pytest.fixture
def svc():
    return SimpleNamespace(
        id=7,
        bot_user_id=1,
        pg_user_id=123,
        pg_username="customer",
        subscription_token=None,
        subscription_url="https://pg.example:8443/apilog/test-token",
        plan_id=2,
    )


def payload(status="active"):
    return {
        "id": 123,
        "status": status,
        "data_limit": 30 * GB,
        "used_traffic": 2 * GB,
        "expire": 2000000000 if status == "active" else None,
        "on_hold_expire_duration": 60 * 86400,
    }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, "/sub"),
        ("", "/sub"),
        (" /apilog/ ", "/apilog"),
        ("apilog", "/apilog"),
        ("/custom/sub/", "/custom/sub"),
    ],
)
def test_path_normalization(raw, expected):
    assert normalize_pg_subscription_path(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://pg.example/sub",
        "/../sub",
        "/sub?x=1",
        "/sub#x",
        "//sub",
        "/",
        "/sub\\path",
        "/sub/\nnext",
        "/" + "x" * 128,
    ],
)
def test_invalid_paths(raw):
    with pytest.raises(ValueError):
        normalize_pg_subscription_path(raw)


def test_settings_env_alias_and_quoted_value():
    assert (
        Settings(_env_file=None, PG_SUBSCRIPTION_PATH='"/apilog/"').pg_subscription_path
        == "/apilog"
    )


@pytest.mark.parametrize(
    "suffix", ["", "/", "/info", "/usage", "/raw", "/apps", "?x=1#section"]
)
def test_custom_link_token_and_path(suffix):
    link = "https://pg.example:8443/custom/apilog/test-token" + suffix
    assert extract_sub_token(link) == "test-token"
    assert subscription_path_from_url(link) == "/custom/apilog"
    assert extract_sub_token("/apilog/test-token") == "test-token"


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "vless://token@host",
        "bare-token",
        "https://pg.example/../token",
        "/apilog/a%2Fb",
        "/apilog/a%20b",
    ],
)
def test_invalid_links(link):
    assert extract_sub_token(link) is None


@pytest.mark.parametrize(
    ("configured", "link", "expected"),
    [
        (
            "/sub",
            "https://pg.example:8443/apilog/test-token",
            "/apilog/test-token/info",
        ),
        (
            "/apilog",
            "https://pg.example:8443/sub/test-token",
            "/sub/test-token/info",
        ),
        ("/custom/sub", None, "/custom/sub/test-token/info"),
        (
            "/sub",
            "https://untrusted.example/apilog/test-token",
            "/apilog/test-token/info",
        ),
    ],
)
def test_client_uses_custom_route_own_origin_and_fresh_public_request(
    configured, link, expected
):
    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(
                200, json={**payload(), "used_traffic": len(requests) * GB}
            )

        pg = PasarGuardClient.__new__(PasarGuardClient)
        pg.settings = SimpleNamespace(pg_subscription_path=configured)
        pg.base_url = "https://pg.example:8443/dashboard"
        pg._token = "private-owner-token"
        pg._headers = AsyncMock(
            side_effect=AssertionError("public read must not authenticate")
        )
        pg._ensure_api_base = AsyncMock(
            side_effect=AssertionError("public route needs no discovery")
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            pg._client = client
            one = await pg.subscription_info("test-token", subscription_url=link)
            two = await pg.subscription_info("test-token", subscription_url=link)
            assert one["used_traffic"] != two["used_traffic"]
            assert len(requests) == 2
            for request in requests:
                assert str(request.url) == "https://pg.example:8443" + expected
                assert "authorization" not in request.headers
                assert request.headers["accept"] == "application/json"
            await pg.subscription_usage("test-token", subscription_url=link)
            assert requests[-1].url.path == expected.removesuffix("info") + "usage"
        pg._headers.assert_not_awaited()
        pg._ensure_api_base.assert_not_awaited()

    asyncio.run(run())


@pytest.mark.parametrize("status", ["active", "on_hold"])
def test_live_url_fallback_populates_bot_and_miniapp(svc, status):
    pg = SimpleNamespace(subscription_info=AsyncMock(return_value=payload(status)))
    info = asyncio.run(fetch_live_service_info(svc, client_factory=lambda: pg))
    pg.subscription_info.assert_awaited_once_with(
        "test-token", subscription_url=svc.subscription_url
    )
    assert info["info_fetched_at"]
    assert not info.get("error")
    out = _serialize_service(svc, info)
    assert out["traffic"] == "2 از 30 گیگ"
    assert out["status"] == status
    assert "2 از 30 گیگ" in service_card(info)
    if status == "on_hold":
        assert out["expire_days"] == 60
        assert "60 روز (پس از اتصال)" in service_card(info)
    else:
        assert out["expire_days"] > 0
    assert out["error"] is None


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (httpx.ReadTimeout("private-token"), "timeout"),
        (PasarGuardError("private-token", 404), "subscription_not_found"),
        (PasarGuardError("private-token", 403), "access_denied"),
        (
            RuntimeError("private-token: Connection refused at private host"),
            "upstream_unavailable",
        ),
    ],
)
def test_failures_are_visible_without_secrets(svc, failure, code, caplog):
    pg = SimpleNamespace(subscription_info=AsyncMock(side_effect=failure))
    info = asyncio.run(fetch_live_service_info(svc, client_factory=lambda: pg))
    out = _serialize_service(svc, info)
    assert out["error_code"] == code
    assert out["error_message"]
    assert out["traffic"] == out["expire"] == "—"
    card = service_card(info)
    assert code in card
    assert info["error_message"] in card
    assert "private-token" not in json.dumps(out) + card + caplog.text
    assert "test-token" not in caplog.text


@pytest.mark.parametrize(
    "bad",
    [
        "<html>Dashboard</html>",
        [],
        {},
        None,
        {"error": "upstream_unavailable", "error_code": []},
    ],
)
def test_invalid_response(bad):
    info = validate_service_info(bad)
    assert info["error"] == "upstream_unavailable"
    assert info["error_message"]


@pytest.mark.parametrize("field", ["status", "used_traffic", "data_limit", "expire"])
def test_missing_field_reports_partial_data_without_unlimited(svc, field):
    data = payload()
    del data[field]
    out = _serialize_service(svc, data)
    assert out["error_code"] == "incomplete_response"
    assert out["error_message"]
    if field in {"data_limit", "used_traffic"}:
        assert out["traffic"] == "—"
    if field == "expire":
        assert out["expire"] == out["expire_days_label"] == "—"
    assert "نامحدود" not in str(out)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("data_limit", -1),
        ("used_traffic", "bad"),
        ("used_traffic", float("inf")),
        ("expire", "bad"),
        ("expire", False),
    ],
)
def test_corrupt_fields_become_unknown(svc, field, value):
    out = _serialize_service(svc, {**payload(), field: value})
    assert out["error_code"] == "incomplete_response"
    assert "نامحدود" not in str(out)


def test_mismatched_service_is_rejected(svc):
    pg = SimpleNamespace(
        subscription_info=AsyncMock(return_value={**payload(), "id": 999})
    )
    out = asyncio.run(fetch_live_service_info(svc, client_factory=lambda: pg))
    assert out["error_code"] == "service_mismatch"
    assert "data_limit" not in out


def test_missing_link_never_reads_admin_api(svc):
    svc.subscription_url = None
    factory = Mock(side_effect=AssertionError("no panel read"))
    out = asyncio.run(fetch_live_service_info(svc, client_factory=factory))
    assert out["error_code"] == "missing_subscription_token"
    factory.assert_not_called()


def test_unfetched_service_has_refresh_explanation(svc):
    with patch(
        "app.api.miniapp_pages._fetch_pg_info", new=AsyncMock(return_value=payload())
    ):
        rows = asyncio.run(_enrich_services([svc] * 21))
    assert rows[20]["error_code"] == "not_loaded"
    assert rows[20]["error_message"]


@pytest.mark.parametrize("handler_name", ["svc_view", "svc_link"])
@pytest.mark.parametrize("failure", [False, True])
def test_bot_handlers_read_custom_link_and_display_result(svc, handler_name, failure):
    import app.bot.handlers.services as handlers

    pg = SimpleNamespace(
        subscription_info=AsyncMock(
            side_effect=httpx.ReadTimeout("secret-token") if failure else None,
            return_value=payload("on_hold"),
        )
    )
    callback = SimpleNamespace(
        data=f"svc:{'view' if handler_name == 'svc_view' else 'link'}:7",
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
        bot=Mock(),
    )
    session = AsyncMock()
    session.get.return_value = svc
    with (
        patch.object(handlers, "get_all_settings", new=AsyncMock(return_value={})),
        patch.object(handlers, "get_pg", return_value=pg),
        patch.object(handlers, "safe_edit_text", new=AsyncMock()) as edit,
        patch(
            "app.services.delivery.send_subscription_qr_photo", new=AsyncMock()
        ) as qr,
    ):
        asyncio.run(
            getattr(handlers, handler_name)(
                callback, session, SimpleNamespace(id=1, telegram_id=42)
            )
        )
    pg.subscription_info.assert_awaited_once_with(
        "test-token", subscription_url=svc.subscription_url
    )
    text = edit.call_args.args[1]
    if failure:
        assert "timeout" in text
        assert "secret-token" not in text
        if handler_name == "svc_link":
            assert qr.call_args.kwargs["info"] is None
    else:
        assert "2 از 30 گیگ" in text
        assert "60 روز (پس از اتصال)" in text


@pytest.fixture(scope="module")
def settings_endpoint():
    from app.api.app import create_api_app

    app = create_api_app()
    return next(
        route.endpoint
        for route in app.routes
        if route.path == "/settings" and "POST" in route.methods
    )


def call_settings(endpoint, *, dns_error=False, **overrides):
    data = {
        "PG_BASE_URL": "https://pg.example:8443/dashboard",
        "PG_SUBSCRIPTION_PATH": "/apilog/",
        "PG_USERNAME": "panel-user",
        "PG_PASSWORD": "",
        "ajax": "1",
        **overrides,
    }
    request = SimpleNamespace(
        query_params={"tab": "pasarguard"}, form=AsyncMock(return_value=FormData(data))
    )
    with ExitStack() as stack:
        # Unit tests must not depend on external DNS (pg.example is reserved).
        # Keep URL/SSRF validation real; only control the resolver's result.
        stack.enter_context(
            patch(
                "socket.getaddrinfo",
                side_effect=socket.gaierror("unavailable") if dns_error else None,
                return_value=[
                    (
                        socket.AF_INET,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                        "",
                        ("8.8.8.8", 8443),
                    )
                ],
            )
        )
        stack.enter_context(
            patch(
                "app.services.setup_wizard.current_setup_values",
                return_value={"PG_PASSWORD": "stored-secret"},
            )
        )
        save = stack.enter_context(patch("app.api.app.update_env_keys"))
        reset = stack.enter_context(patch("app.services.pasarguard.reset_pg"))
        clear = stack.enter_context(
            patch("app.services.pg_access.clear_platform_pg_capability_cache")
        )
        probe = stack.enter_context(
            patch(
                "app.services.pg_access.resolve_platform_pg_capabilities",
                new=AsyncMock(return_value={"ok": True}),
            )
        )
        response = asyncio.run(
            endpoint(request, staff={"role": "admin"}, session=AsyncMock())
        )
    return response, save, reset, clear, probe


def test_panel_settings_save_preserves_password_and_changes_only_pg(settings_endpoint):
    response, save, reset, clear, probe = call_settings(settings_endpoint)
    assert response.status_code == 200, response.body.decode()
    assert json.loads(response.body)["ok"]
    save.assert_called_once_with(
        {
            "PG_BASE_URL": "https://pg.example:8443/dashboard",
            "PG_SUBSCRIPTION_PATH": "/apilog",
            "PG_USERNAME": "panel-user",
            "PG_PASSWORD": "stored-secret",
        }
    )
    reset.assert_called_once()
    clear.assert_called_once()
    probe.assert_not_awaited()
    assert "stored-secret" not in response.body.decode()


def test_panel_connection_test_does_not_save(settings_endpoint):
    response, save, reset, clear, probe = call_settings(
        settings_endpoint, action="test", PG_PASSWORD="new-secret"
    )
    assert response.status_code == 200, response.body.decode()
    probe.assert_awaited_once_with(
        username="panel-user",
        password="new-secret",
        base_url="https://pg.example:8443/dashboard",
        use_cache=False,
    )
    save.assert_not_called()
    reset.assert_not_called()
    clear.assert_not_called()
    assert "new-secret" not in response.body.decode()


@pytest.mark.parametrize(
    "bad", ["https://bad.example/apilog", "/../apilog", "/apilog?token=secret"]
)
def test_panel_settings_bad_path_does_not_write(settings_endpoint, bad):
    response, save, reset, clear, probe = call_settings(
        settings_endpoint, PG_SUBSCRIPTION_PATH=bad
    )
    assert response.status_code == 400
    save.assert_not_called()
    probe.assert_not_awaited()
    assert "secret" not in response.body.decode()


def test_panel_settings_template_keeps_password_out_of_html():
    from pathlib import Path
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from app.services.users import SETTINGS_DOMAIN_REDIRECTS, SETTINGS_TABS

    templates = Path(__file__).resolve().parents[1] / "app/web/templates"
    env = Environment(
        loader=FileSystemLoader(templates),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.globals["modal_title"] = lambda *a, **k: ""
    html = env.get_template("_settings_bot.html").render(
        env_values={
            "PG_PASSWORD": "stored-secret",
            "PG_SUBSCRIPTION_PATH": "/apilog",
            "PG_BASE_URL": "https://pg.example",
            "BOT_TOKEN": "",
            "BOT_USERNAME": "",
            "ADMIN_IDS": "",
            "WEBHOOK_URL": "",
            "WEBHOOK_PATH": "/telegram/webhook",
            "PUBLIC_BASE_URL": "",
        },
        bot_status={"ok": False, "label": ""},
        bot_token_masked="",
        bot_update_mode="polling",
        webhook_full_url="",
        request=SimpleNamespace(query_params={}),
        csrf_token="",
        csp_nonce="",
    )
    assert 'id="pg-password-input"' in html
    assert 'type="password"' in html
    assert "stored-secret" not in html
    assert 'action="/settings?tab=bot"' in html
    assert "وب و عمومی" not in html
    assert ("pasarguard", "اتصال پاسارگارد") not in SETTINGS_TABS
    assert SETTINGS_DOMAIN_REDIRECTS.get("pasarguard") == "/settings?tab=bot"


def test_actual_path_overrides_other_configured_path():
    pg = PasarGuardClient.__new__(PasarGuardClient)
    pg.settings = SimpleNamespace(pg_subscription_path="/configured")
    pg.base_url = "https://pg.example:8443/dashboard"
    assert (
        pg._subscription_endpoint("tok", "info", "https://pg.example/apilog/tok")
        == "https://pg.example:8443/apilog/tok/info"
    )


def test_custom_relative_link_and_token_only_fallback():
    from app.services.pasarguard import user_subscription_url

    with (
        patch(
            "app.services.pasarguard.public_pg_sub_origin",
            return_value="https://pg.example:8443",
        ),
        patch(
            "app.services.pasarguard.get_settings",
            return_value=SimpleNamespace(pg_subscription_path="/apilog"),
        ),
    ):
        assert (
            user_subscription_url({"links": ["/apilog/tok"]})
            == "https://pg.example:8443/apilog/tok"
        )
        assert (
            user_subscription_url({"subscription_token": "tok"})
            == "https://pg.example:8443/apilog/tok"
        )


@pytest.mark.parametrize(
    "base",
    ["https://user:secret@pg.example", "http://169.254.169.254", "file:///secret", ""],
)
def test_panel_settings_unsafe_url_never_saves_or_dials(settings_endpoint, base):
    response, save, reset, clear, probe = call_settings(
        settings_endpoint, PG_BASE_URL=base
    )
    assert response.status_code == 400
    save.assert_not_called()
    probe.assert_not_awaited()
    assert "secret" not in response.body.decode()


def test_panel_settings_dns_failure_never_saves_or_dials(settings_endpoint):
    response, save, reset, clear, probe = call_settings(
        settings_endpoint, dns_error=True
    )
    assert response.status_code == 400
    assert "قابل resolve نیست" in json.loads(response.body)["error"]
    save.assert_not_called()
    probe.assert_not_awaited()
