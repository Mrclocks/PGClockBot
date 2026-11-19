"""Architecture cleanup contracts — authz aliases and login_guard."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bot_pg_authz_modules_importable():
    from app.services import bot_pg_catalog_authz, bot_pg_object_authz, bot_pg_user_authz
    from app.services.bot_pg_catalog_authz import authorize_bot_pg_catalog_op
    from app.services.bot_pg_object_authz import authorize_bot_pg_object_op
    from app.services.bot_pg_user_authz import authorize_bot_pg_user_op

    assert callable(authorize_bot_pg_user_op)
    assert callable(authorize_bot_pg_catalog_op)
    assert callable(authorize_bot_pg_object_op)
    assert bot_pg_user_authz.__doc__ and "Phase 4" not in bot_pg_user_authz.__doc__
    assert bot_pg_catalog_authz.__doc__ and "Phase 4" not in bot_pg_catalog_authz.__doc__
    assert bot_pg_object_authz.__doc__ and "Phase 4" not in bot_pg_object_authz.__doc__


def test_bot_pg_pilot_shims_reexport_authz():
    """Legacy ``*_pilot`` import paths stay as thin compatibility shims."""
    from app.services import (
        bot_pg_catalog_authz,
        bot_pg_catalog_pilot,
        bot_pg_object_authz,
        bot_pg_object_pilot,
        bot_pg_user_authz,
        bot_pg_user_pilot,
    )

    assert bot_pg_user_pilot.authorize_bot_pg_user_op is bot_pg_user_authz.authorize_bot_pg_user_op
    assert (
        bot_pg_catalog_pilot.authorize_bot_pg_catalog_op
        is bot_pg_catalog_authz.authorize_bot_pg_catalog_op
    )
    assert (
        bot_pg_object_pilot.authorize_bot_pg_object_op
        is bot_pg_object_authz.authorize_bot_pg_object_op
    )

    for name in (
        "bot_pg_user_pilot.py",
        "bot_pg_catalog_pilot.py",
        "bot_pg_object_pilot.py",
    ):
        src = (ROOT / "app" / "services" / name).read_text(encoding="utf-8")
        tree = ast.parse(src)
        # Shim files should stay tiny: import re-export only, no new authz logic.
        assert len(src.splitlines()) <= 12
        assert any(
            isinstance(n, (ast.ImportFrom, ast.Import)) for n in tree.body
        ), f"{name} must re-export from authz"


def test_login_guard_extracted_from_app():
    from app.api import login_guard
    from app.api.login_guard import client_ip, login_blocked, login_fail, login_success

    assert callable(client_ip)
    assert callable(login_blocked)
    assert callable(login_fail)
    assert callable(login_success)

    app_src = (ROOT / "app" / "api" / "app.py").read_text(encoding="utf-8")
    assert "from app.api.login_guard import" in app_src
    assert "def _client_ip(" not in app_src
    assert "def _login_blocked(" not in app_src
    assert "_LOGIN_FAILURES" not in app_src
    assert "login" in (login_guard.__doc__ or "").lower()
    assert "Extracted from" in (login_guard.__doc__ or "")

    # Behavioral smoke: failures accumulate then clear on success.
    login_guard._LOGIN_FAILURES.clear()
    ip = "203.0.113.50"
    assert login_blocked(ip) is False
    for _ in range(login_guard._LOGIN_MAX_FAILURES):
        login_fail(ip)
    assert login_blocked(ip) is True
    login_success(ip)
    assert login_blocked(ip) is False
