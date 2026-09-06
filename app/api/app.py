from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, BadTimeSignature, URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import DATA_DIR, get_settings, normalize_pg_base_url
from app.db.models import (
    BotUser,
    Order,
    OrderStatus,
    Payment,
    PaymentStatus,
    Plan,
    ResellerProfile,
    Role,
    UserService,
)
from app.db.session import SessionLocal
from app.services.orders import (
    approve_payment,
    cancel_order,
    deliver_order,
    manual_fulfill_unpaid_order,
    reject_order,
    reject_payment,
)
from app.services.list_query import filter_by_search, normalize_search_q
from app.services.payer_notify import notify_payer
from app.services.pasarguard import get_pg
from app.services.resellers import (
    setup_is_complete,
    DEFAULT_FEATURE_PERMS,
)
from app.services.setup_wizard import (
    begin_setup,
    current_setup_values,
    ensure_setup_gate_token,
    ensure_web_secret,
    is_local_setup_client,
    is_setup_complete,
    mark_setup_complete,
    normalize_webhook_base_url,
    normalize_webhook_path,
    resolve_bot_update_mode,
    setup_finish_login_url,
    setup_pg_access_audit,
    wizard_panel_url_hint,
    parse_admin_ids,
    revoke_setup_gate,
    rotate_setup_gate_token,
    setup_gate_cookie_max_age,
    setup_gate_ok,
    update_env_keys,
)
from app.services.security_policy import (
    PUBLIC_FORM_MAX_BODY_BYTES,
    content_length_ok,
    request_host_allowed,
)
from app.services.updates import local_version
from app.services.users import (
    SETTINGS_DOMAIN_REDIRECTS,
    SETTING_GROUPS,
    SETTINGS_TABS,
    PANEL_SETTINGS_KEYS,
    PANEL_SETTINGS_TABS,
    TAB_SETTING_GROUPS,
    get_all_settings,
    set_setting,
    set_settings_bulk,
)
from app.services.web_auth import (
    admin_session_version,
    load_web_admin,
    save_web_admin,
    validate_password_strength,
    verify_password_hash,
    verify_web_admin,
)
from app.api.home_pages import register_home_pages
from app.api.pg_pages import register_pg_pages

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

from app.services.formatting import format_bytes, format_bytes_ratio, format_gb, format_number, format_expire_short, order_status_fa, ticket_status_fa

templates.env.filters["bytes"] = format_bytes
templates.env.filters["bytes_ratio"] = format_bytes_ratio
templates.env.filters["gb"] = format_gb
templates.env.filters["num"] = format_number
templates.env.filters["expire"] = format_expire_short
templates.env.filters["order_status"] = order_status_fa
templates.env.filters["ticket_status"] = ticket_status_fa
templates.env.globals["app_version"] = local_version()
templates.env.globals["order_status_fa"] = order_status_fa
templates.env.globals["ticket_status_fa"] = ticket_status_fa
templates.env.globals["format_bytes"] = format_bytes
templates.env.globals["format_bytes_ratio"] = format_bytes_ratio

# Login brute-force tracking: ip -> list of failure timestamps
_LOGIN_FAILURES: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW_SEC = 15 * 60
_LOGIN_MAX_FAILURES = 8


class NotAuthenticated(Exception):
    def __init__(self, message: str | None = None, *, login_error: str | None = None):
        self.message = message
        self.login_error = login_error or message
        super().__init__(message or "not authenticated")


class NotAdmin(Exception):
    """Raised when staff lacks a permission.

    Optional ``redirect`` uses *live* ACL (not the session cookie) so role
    downgrades cannot infinite-redirect via stale ``pg_permissions``.
    """

    def __init__(self, redirect: str | None = None):
        self.redirect = redirect


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = dict(context or {})
    ctx.setdefault("flash_ok", None)
    ctx.setdefault("flash_err", None)
    ctx.setdefault("open_edit", None)
    ctx.setdefault("app_version", local_version())
    if "pwa_name" not in ctx:
        try:
            from app.services.pwa import panel_display_name

            ctx["pwa_name"] = panel_display_name()
        except Exception:
            ctx["pwa_name"] = "MrClockBot"
    # Sidebar update badge — prefer explicit context, else request.state, else cache peek.
    if "update" not in ctx:
        upd = getattr(request.state, "panel_update", None)
        if upd is None:
            try:
                from app.services.updates import peek_update_cache

                upd = peek_update_cache()
            except Exception:
                upd = None
        if upd is not None:
            ctx["update"] = upd
    if "tickets_unread" not in ctx:
        ctx["tickets_unread"] = int(getattr(request.state, "panel_tickets_unread", 0) or 0)
    if "inbox_alert" not in ctx:
        ctx["inbox_alert"] = bool(getattr(request.state, "panel_inbox_alert", False))
    if ctx.get("staff") and "identity" not in ctx:
        try:
            from app.services.identity_chrome import hierarchy_identity

            ctx["identity"] = hierarchy_identity(ctx.get("staff"))
        except Exception:
            ctx["identity"] = {}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


from app.api.safe_next import safe_internal_next


def _settings_next(request: Request, fallback: str) -> str:
    return safe_internal_next(request.query_params.get("next"), fallback)


def _redirect_msg(path: str, *, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    q = []
    if ok:
        q.append(f"ok={quote(ok)}")
    if err:
        q.append(f"err={quote(err)}")
    # Bust caches that key only on stable ?ok= text (table must refresh with flash)
    q.append(f"_={int(time.time())}")
    sep = "&" if "?" in path else "?"
    url = f"{path}{sep}{'&'.join(q)}"
    return RedirectResponse(url, status_code=303)


SESSION_MAX_AGE = 60 * 60 * 24 * 7


def _client_ip(request: Request) -> str:
    """Best-effort real client IP, resistant to X-Forwarded-For spoofing.

    X-Forwarded-For is fully attacker-controlled except for the hop(s) your
    own trusted reverse proxy appends. Reading the LEFT-most entry (the
    classic mistake) lets any client claim to be any IP — including
    loopback/private ranges, which would bypass login lockouts and the
    setup-wizard local-IP auto-open gate. Instead we read the entry counted
    from the RIGHT that corresponds to ``trust_proxy_hops`` (default: a
    single reverse proxy directly in front of the app).
    """
    try:
        settings = get_settings()
        if settings.trust_proxy:
            raw = request.headers.get("x-forwarded-for") or ""
            parts = [p.strip() for p in raw.split(",") if p.strip()]
            hops = max(1, int(getattr(settings, "trust_proxy_hops", 1) or 1))
            if len(parts) >= hops:
                candidate = parts[-hops]
                if candidate:
                    return candidate
    except Exception:
        pass
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _login_blocked(ip: str) -> bool:
    now = time.time()
    stamps = [t for t in _LOGIN_FAILURES.get(ip, []) if now - t < _LOGIN_WINDOW_SEC]
    _LOGIN_FAILURES[ip] = stamps
    return len(stamps) >= _LOGIN_MAX_FAILURES


def _login_fail(ip: str) -> None:
    now = time.time()
    stamps = [t for t in _LOGIN_FAILURES.get(ip, []) if now - t < _LOGIN_WINDOW_SEC]
    stamps.append(now)
    _LOGIN_FAILURES[ip] = stamps


def _login_success(ip: str) -> None:
    _LOGIN_FAILURES.pop(ip, None)


def _cookie_secure(request: Request) -> bool:
    try:
        from app.services.ssl_certs import https_is_active

        if not https_is_active():
            return False
    except Exception:
        return False
    try:
        if get_settings().trust_proxy:
            fwd = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
            if fwd == "https":
                return True
    except Exception:
        pass
    return request.url.scheme == "https"


def _panel_redirect(request: Request, path: str, *, status_code: int = 303) -> RedirectResponse:
    """Redirect using HTTP until HTTPS is live; then use the public HTTPS base.

    Must never raise — a failure here 500s login/logout landing for every role.
    """
    if not path.startswith("/"):
        path = "/" + path
    try:
        from app.services.ssl_certs import https_is_active, public_panel_base_url

        if https_is_active():
            base = (public_panel_base_url() or "").rstrip("/")
            if base:
                return RedirectResponse(f"{base}{path}", status_code=status_code)
    except Exception:
        logging.getLogger(__name__).exception("_panel_redirect https probe failed")
    try:
        host = (request.headers.get("host") or getattr(request.url, "netloc", None) or "").strip()
        if host:
            return RedirectResponse(f"http://{host}{path}", status_code=status_code)
    except Exception:
        logging.getLogger(__name__).exception("_panel_redirect host fallback failed")
    return RedirectResponse(path, status_code=status_code)


def create_api_app(lifespan=None) -> FastAPI:
    app = FastAPI(title="PGClockBot Panel", docs_url=None, redoc_url=None, lifespan=lifespan)
    from starlette.middleware.gzip import GZipMiddleware

    app.add_middleware(GZipMiddleware, minimum_size=400)
    class _CachedStatic(StaticFiles):
        async def get_response(self, path, scope):  # type: ignore[override]
            response = await super().get_response(path, scope)
            # Versioned assets (?v=) can be cached aggressively by browsers.
            if response.status_code == 200:
                response.headers.setdefault("Cache-Control", "public, max-age=604800, immutable")
            return response

    class _PublicUploads(StaticFiles):
        """Public shop media only — never serve private ticket attachments."""

        async def get_response(self, path, scope):  # type: ignore[override]
            rel = (path or "").lstrip("/").replace("\\", "/")
            if rel == "tickets" or rel.startswith("tickets/"):
                from starlette.responses import Response

                return Response(status_code=404)
            return await super().get_response(path, scope)

    app.mount("/static", _CachedStatic(directory=str(WEB_DIR / "static")), name="static")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        DATA_DIR.chmod(0o700)
    except OSError:
        pass
    uploads_dir = DATA_DIR / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    # CRITICAL: never mount DATA_DIR itself — that would expose bot.db, backups, web_admin.json
    app.mount("/media/uploads", _PublicUploads(directory=str(uploads_dir)), name="media_uploads")
    try:
        from app.services.ssl_certs import WEBROOT_DIR, ensure_dirs as ensure_ssl_dirs

        ensure_ssl_dirs()
        acme_dir = WEBROOT_DIR / ".well-known" / "acme-challenge"
        app.mount(
            "/.well-known/acme-challenge",
            StaticFiles(directory=str(acme_dir)),
            name="acme_challenge",
        )
    except Exception:
        pass

    def get_signer() -> URLSafeTimedSerializer:
        # Never fall back to a hardcoded or process-only secret.
        secret = ensure_web_secret()
        if not secret:
            from app.services.setup_wizard import WebSecretPersistenceError

            raise WebSecretPersistenceError(
                "WEB_SECRET is missing and could not be persisted. "
                "Set WEB_SECRET in the environment or make .env writable."
            )
        return URLSafeTimedSerializer(secret, salt="pgclock-session")

    async def get_db():
        async with SessionLocal() as session:
            try:
                yield session
            except Exception:
                from app.services.db_safe import rollback_quiet

                await rollback_quiet(session)
                raise

    def get_session_user(request: Request) -> Optional[dict]:
        cookie = request.cookies.get("session")
        if not cookie:
            return None
        try:
            data = get_signer().loads(cookie, max_age=SESSION_MAX_AGE)
        except (BadSignature, BadTimeSignature):
            return None
        except Exception:
            logging.getLogger(__name__).exception("get_session_user signer/load failed")
            return None
        if not isinstance(data, dict):
            return None
        # Admin sessions bind to web_admin.json token — password/username change revokes them
        if data.get("role") == "admin":
            try:
                expected = admin_session_version()
            except Exception:
                logging.getLogger(__name__).exception("admin_session_version failed")
                return None
            if not expected or data.get("sv") != expected:
                return None
        return data

    async def require_staff(
        request: Request,
        session: AsyncSession = Depends(get_db),
    ) -> dict:
        user = get_session_user(request)
        if not user or user.get("role") not in {
            "admin",
            "reseller",
            "pg_staff",
            "principal",
        }:
            raise NotAuthenticated()
        if user.get("role") == "reseller":
            from sqlalchemy import select

            from app.db.models import ResellerProfile
            from app.services.resellers import setup_is_complete

            bot_user_id = user.get("bot_user_id")
            if not bot_user_id:
                raise NotAuthenticated()
            result = await session.execute(
                select(ResellerProfile).where(ResellerProfile.user_id == int(bot_user_id))
            )
            profile = result.scalar_one_or_none()
            if not profile or not profile.is_active or not setup_is_complete(profile):
                from app.services.pg_staff_access import PG_ACCESS_DENIED_MSG

                raise NotAuthenticated(login_error=PG_ACCESS_DENIED_MSG)
            # Password change invalidates older cookies
            pwd = (profile.web_password_hash or "")[:24]
            if pwd and user.get("pv") != pwd:
                raise NotAuthenticated()
            # Live PasarGuard gate when linked to a PG admin
            if profile.pg_admin_username:
                from app.services.pg_staff_access import enforce_pg_admin_web_gate

                allowed, deny_msg = await enforce_pg_admin_web_gate(
                    session, profile.pg_admin_username, revoke_if_missing=True
                )
                if not allowed:
                    raise NotAuthenticated(login_error=deny_msg)
            # Always re-read ACL from DB — never trust stale cookie permissions.
            # Must match Bot has_bot_perm / authz.resolve_shop_permissions_from_profile
            # (empty string = intentional deny; None = DEFAULT).
            from app.services.authz import resolve_shop_permissions_from_profile

            user = dict(user)
            resolved = resolve_shop_permissions_from_profile(profile)
            user["permissions"] = list(resolved or [])
            user["bot_user_id"] = int(bot_user_id)
            if profile.pg_admin_username:
                user["pg_admin_username"] = profile.pg_admin_username
            # Live GET /api/admin nested role — same source as quota boxes.
            from app.services.pg_access import enrich_staff_pg_from_role, resolve_acl_from_client

            pg_client = None
            if profile.pg_admin_username:
                try:
                    from app.services.pasarguard import get_pg_for_reseller

                    pg_client = await get_pg_for_reseller(session, int(profile.user_id))
                except Exception:
                    pg_client = None
            user.pop("pg_permissions", None)
            if pg_client is not None:
                features, role, _admin = await resolve_acl_from_client(
                    pg_client, username=profile.pg_admin_username
                )
                user = enrich_staff_pg_from_role(user, features, role)
                if isinstance(role, dict) and role.get("id") is not None:
                    try:
                        user["pg_role_id"] = int(role["id"])
                    except (TypeError, ValueError):
                        pass
            elif profile.pg_role_id:
                user["pg_role_id"] = int(profile.pg_role_id)
            # Principal from the server-loaded ResellerProfile only.
            # Cookie org_principal_id / parent / depth / scope are not selectors.
            from app.services.org_principals import (
                attach_org_principal_fields,
                bind_reseller_profile_principal,
            )
            from app.services.org_scope import visible_principal_ids

            user.pop("org_principal_id", None)
            user.pop("org_parent_id", None)
            user.pop("org_depth", None)
            user.pop("org_visible_principal_ids", None)
            user.pop("web_owner", None)
            user["reseller_profile_id"] = int(profile.id)
            principal = await bind_reseller_profile_principal(session, profile)
            if principal is None:
                raise NotAuthenticated()
            visible = await visible_principal_ids(session, principal)
            user = attach_org_principal_fields(
                user, principal, visible_principal_ids=visible
            )
        elif user.get("role") == "pg_staff":
            from app.services.pg_access import enrich_staff_pg_from_role, resolve_acl_from_client
            from app.services.pg_staff_access import (
                PG_ACCESS_DENIED_MSG,
                access_by_web_username,
                enforce_pg_admin_web_gate,
                staff_has_stored_pg_password,
            )

            web_u = (user.get("username") or "").strip().lower()
            row = await access_by_web_username(session, web_u)
            if not row or not row.is_active:
                raise NotAuthenticated(login_error=PG_ACCESS_DENIED_MSG)
            pwd = (row.web_password_hash or "")[:24]
            if pwd and user.get("pv") != pwd:
                raise NotAuthenticated()
            allowed, deny_msg = await enforce_pg_admin_web_gate(
                session, row.pg_username, revoke_if_missing=True
            )
            if not allowed:
                # Gate may have revoked the row — deny without a second DB round-trip
                raise NotAuthenticated(login_error=deny_msg)
            user = dict(user)
            user["permissions"] = []
            user["pg_admin_username"] = row.pg_username
            user["pg_staff_id"] = int(row.id)
            # Phase C5: advertise stored PG password so menus/reads match client selection
            user["pg_credentials_ready"] = staff_has_stored_pg_password(row)
            if row.pg_role_id:
                user["pg_role_id"] = int(row.pg_role_id)
            pg_client = None
            try:
                from app.services.pasarguard import get_pg_for_staff

                pg_client = await get_pg_for_staff(session, staff_id=int(row.id))
            except Exception:
                pg_client = None
            user.pop("pg_permissions", None)
            features, role = [], None
            if pg_client is not None:
                features, role, _admin = await resolve_acl_from_client(
                    pg_client, username=row.pg_username
                )
            user = enrich_staff_pg_from_role(user, features, role)
            if isinstance(role, dict) and role.get("id") is not None:
                try:
                    user["pg_role_id"] = int(role["id"])
                except (TypeError, ValueError):
                    pass
            if not (user.get("pg_permissions") or []):
                # No mapped features → deny panel use
                raise NotAuthenticated(login_error=PG_ACCESS_DENIED_MSG)
            # Phase 1G: pg_staff without an existing OrgPrincipal → DENY (no role fallback).
            from app.services.org_principals import (
                attach_org_principal_fields,
                resolve_org_principal_for_staff,
            )
            from app.services.org_scope import visible_principal_ids

            principal = await resolve_org_principal_for_staff(session, user)
            if principal is None:
                raise NotAuthenticated(login_error=PG_ACCESS_DENIED_MSG)
            visible = await visible_principal_ids(session, principal)
            user = attach_org_principal_fields(
                user, principal, visible_principal_ids=visible
            )
        elif user.get("role") == "principal":
            # Phase 2B: Level-1 OrgPrincipal Web identity — resolve server-side only.
            from app.services.principal_web_identity import (
                PrincipalWebIdentityError,
                resolve_principal_web_session,
            )

            try:
                user = await resolve_principal_web_session(session, user)
            except PrincipalWebIdentityError as exc:
                raise NotAuthenticated(login_error=exc.message) from exc
        elif user.get("role") == "admin":
            # Hybrid Owner: shop stays full; PG menus/actions follow env PG role.
            # C2/1G: bind Web Owner session to explicit Org Owner principal (not role alone).
            from app.services.org_principals import (
                attach_org_principal_fields,
                resolve_org_principal_for_staff,
            )
            from app.services.org_scope import visible_principal_ids
            from app.services.pg_access import enrich_platform_admin_staff

            user = await enrich_platform_admin_staff(dict(user))
            # Valid admin cookie is always web_admin.json-bound (sv check above).
            user["web_owner"] = True
            principal = await resolve_org_principal_for_staff(session, user)
            if principal is None:
                raise NotAuthenticated()
            visible = await visible_principal_ids(session, principal)
            user = attach_org_principal_fields(
                user, principal, visible_principal_ids=visible
            )
        # Skip unread COUNT on mutations / JSON polls that never render the sidebar.
        try:
            from app.services.panel_tickets import should_skip_unread_count, sidebar_unread_count

            if should_skip_unread_count(request.url.path, request.method):
                request.state.panel_tickets_unread = 0
                request.state.panel_inbox_alert = False
            else:
                from app.services.panel_sidebar_cache import (
                    peek_sidebar_counts,
                    sidebar_cache_key,
                    store_sidebar_counts,
                )

                cache_key = sidebar_cache_key(user)
                cached = peek_sidebar_counts(cache_key)
                if cached is not None:
                    request.state.panel_tickets_unread = cached[0]
                    request.state.panel_inbox_alert = cached[1]
                else:
                    unread = await sidebar_unread_count(session, user)
                    from app.services.panel_inbox import sidebar_inbox_has_alerts

                    alert = await sidebar_inbox_has_alerts(session, request, user)
                    store_sidebar_counts(cache_key, unread, alert)
                    request.state.panel_tickets_unread = unread
                    request.state.panel_inbox_alert = alert
        except Exception:
            from app.services.db_safe import rollback_quiet

            await rollback_quiet(session)
            request.state.panel_tickets_unread = 0
            request.state.panel_inbox_alert = False
        from app.services.db_safe import recover_session

        await recover_session(session)
        return user

    async def require_admin(
        request: Request,
        session: AsyncSession = Depends(get_db),
    ) -> dict:
        from app.services.platform_identity import is_explicit_owner_staff

        user = await require_staff(request, session)
        # C2: role=admin alone is not Owner — require attached Owner principal.
        if user.get("role") != "admin" or not is_explicit_owner_staff(user):
            raise NotAdmin()
        return user

    def require_perm(perm: str):
        async def _dep(
            request: Request,
            session: AsyncSession = Depends(get_db),
        ) -> dict:
            from app.services.authz import authz_from_staff, can_shop

            user = await require_staff(request, session)
            if not can_shop(authz_from_staff(user), perm):
                raise NotAdmin()
            return user

        return _dep

    def _live_pg_home(features: list[str] | tuple[str, ...] | set[str]) -> str:
        feats = set(features or [])
        if "pg_overview" in feats:
            return "/pg"
        if "pg_users" in feats:
            return "/pg/users"
        if "pg_templates" in feats:
            return "/pg/templates"
        if "pg_groups" in feats:
            return "/pg/groups"
        if "pg_hosts" in feats:
            return "/pg/hosts"
        if "pg_nodes" in feats:
            return "/pg/nodes"
        if "pg_inbounds" in feats:
            return "/pg/inbounds"
        return "/logout"

    def require_pg_perm(perm: str):
        """Require mapped PG feature (Hybrid: Owner also clamped to env PG role).

        Phase 2C: Level-1 Principals use the same PG page keys + local safety.
        """

        async def _dep(
            request: Request,
            session: AsyncSession = Depends(get_db),
        ) -> dict:
            from app.services.authz import authz_from_staff, can_pg_page, is_platform_admin
            from app.services.pg_read import effective_pg_menu_keys
            from app.services.principal_pg_authz import (
                is_level1_principal_staff,
                staff_may_pg_page,
            )

            user = await require_staff(request, session)
            ctx = authz_from_staff(user)
            if is_level1_principal_staff(user):
                if not staff_may_pg_page(user, perm):
                    features = list(ctx.pg_permissions)
                    raise NotAdmin(redirect=_live_pg_home(features))
                return user
            if not can_pg_page(ctx, perm):
                features = list(ctx.pg_permissions)
                # Platform admin without PG features → shop home, not logout
                if is_platform_admin(ctx) and not features:
                    raise NotAdmin(redirect="/home")
                raise NotAdmin(redirect=_live_pg_home(features))
            # C1: uncredentialed pg_staff may only open overview (menu clamp alone is insufficient)
            if not is_platform_admin(ctx):
                allowed = set(effective_pg_menu_keys(user))
                if perm not in allowed:
                    raise NotAdmin(redirect="/pg")
            return user

        return _dep

    @app.middleware("http")
    async def setup_gate(request: Request, call_next):
        path = request.url.path
        complete = is_setup_complete()

        if complete:
            if path == "/setup" or path.startswith("/setup/"):
                return RedirectResponse("/", status_code=303)
            return await call_next(request)

        # First-run: only wizard + static/health. Everything else → /
        allowed = (
            path == "/"
            or path == "/setup"
            or path.startswith("/setup/")
            or path.startswith("/static")
            or path.startswith("/.well-known/")
            or path.startswith("/pwa/")
            or path == "/health"
            or path == "/sw.js"
            or path == "/manifest.webmanifest"
        )
        if not allowed:
            return RedirectResponse("/", status_code=303)

        # Protect open wizard from remote takeover with a one-time gate token
        needs_gate = path == "/" or path == "/setup" or path.startswith("/setup/")
        if needs_gate:
            gate_q = (request.query_params.get("gate") or "").strip()
            gate_c = (request.cookies.get("setup_gate") or "").strip()
            q_ok = bool(gate_q) and setup_gate_ok(gate_q)
            c_ok = bool(gate_c) and setup_gate_ok(gate_c)
            local_ok = is_local_setup_client(_client_ip(request))
            if q_ok or c_ok or local_ok:
                response = await call_next(request)
                cookie_ttl = setup_gate_cookie_max_age()
                tok = ensure_setup_gate_token()
                if local_ok and not c_ok and tok and cookie_ttl > 0:
                    response.set_cookie(
                        "setup_gate",
                        tok,
                        httponly=True,
                        samesite="strict",
                        secure=_cookie_secure(request),
                        max_age=cookie_ttl,
                        path="/",
                    )
                elif q_ok and not c_ok:
                    new_tok = rotate_setup_gate_token()
                    cookie_ttl = setup_gate_cookie_max_age()
                    if new_tok and cookie_ttl > 0:
                        response.set_cookie(
                            "setup_gate",
                            new_tok,
                            httponly=True,
                            samesite="strict",
                            secure=_cookie_secure(request),
                            max_age=cookie_ttl,
                            path="/",
                        )
                return response
            return HTMLResponse(
                "<!DOCTYPE html><html lang='fa' dir='rtl'><head><meta charset='utf-8'/>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'/>"
                "<title>لینک ویزارد نصب</title></head><body style='font-family:Vazirmatn,sans-serif;"
                "max-width:42rem;margin:3rem auto;padding:0 1rem;line-height:1.8;color:#18181b'>"
                "<h1>ویزارد نصب اولیه</h1>"
                "<p>برای امنیت، ورود از اینترنت فقط با <b>لینک یک‌بارمصرف</b> ممکن است "
                "(اعتبار حداکثر ۱۵ دقیقه؛ پس از اتمام تنظیمات یا ورود به پنل غیرفعال می‌شود).</p>"
                "<p><b>روی سرور</b> لینک را از خروجی نصب یا این دستور بگیرید:</p>"
                "<p><code style='background:#f4f4f5;padding:6px 10px;border-radius:4px;display:block'>"
                "bash pgclock.sh status</code></p>"
                "<p style='font-size:14px;color:#71717a'>"
                "اگر از همان سرور با <code>127.0.0.1</code> باز کنید، معمولاً بدون لینک هم باز می‌شود."
                "</p></body></html>",
                status_code=403,
            )
        return await call_next(request)

    @app.middleware("http")
    async def csrf_origin_guard(request: Request, call_next):
        """Reject cross-site unsafe requests that carry a session cookie (defense-in-depth)."""
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            has_session = bool(request.cookies.get("session") or request.cookies.get("setup_gate"))
            if has_session:
                origin = request.headers.get("origin")
                referer = request.headers.get("referer")
                host = request.headers.get("host")
                # Require Origin or Referer for cookie-authenticated mutations
                if not origin and not referer:
                    return HTMLResponse("CSRF rejected", status_code=403)
                if not (
                    request_host_allowed(host, origin)
                    or request_host_allowed(host, referer)
                ):
                    return HTMLResponse("CSRF rejected", status_code=403)
        path = request.url.path
        if request.method == "POST" and path in {"/login", "/setup", "/setup/save", "/"}:
            if not content_length_ok(request.headers.get("content-length"), PUBLIC_FORM_MAX_BODY_BYTES):
                return HTMLResponse("Request too large", status_code=413)
        return await call_next(request)

    @app.middleware("http")
    async def panel_timing_middleware(request: Request, call_next):
        """Observation-only timing for panel HTML/JSON — never touches auth/ACL."""
        from app.services.panel_timing import (
            begin,
            finish_log,
            server_timing_header,
            should_time,
        )

        if request.method != "GET" or not should_time(request.url.path):
            return await call_next(request)
        begin(request)
        response = await call_next(request)
        snap = finish_log(request, status_code=getattr(response, "status_code", 0) or 0)
        hdr = server_timing_header(snap)
        if hdr:
            response.headers.setdefault("Server-Timing", hdr)
        return response

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        is_miniapp = request.url.path.startswith("/miniapp")
        # Mini App opens inside Telegram WebView — allow framing only there
        if is_miniapp:
            response.headers["X-Frame-Options"] = "SAMEORIGIN"
        else:
            response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # Panel pages only; keep CSP moderate so inline preview/scripts still work
        if not request.url.path.startswith("/static") and not request.url.path.startswith("/media"):
            script_src = "script-src 'self' 'unsafe-inline'"
            frame_ancestors = "frame-ancestors 'none'"
            # Mini App needs Telegram WebApp SDK (+ Telegram may embed the sheet)
            if is_miniapp:
                script_src = "script-src 'self' 'unsafe-inline' https://telegram.org"
                frame_ancestors = (
                    "frame-ancestors 'self' https://web.telegram.org https://telegram.org"
                )
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; "
                f"{script_src}; "
                "font-src 'self' data:; connect-src 'self'; "
                f"{frame_ancestors}; base-uri 'self'; form-action 'self'",
            )
        path = request.url.path
        ct = (response.headers.get("content-type") or "").lower()
        # Authenticated panel HTML must never be cached — flash + table must stay in sync
        if "text/html" in ct:
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
        elif path in {"/login", "/setup", "/security"} or path.startswith("/setup/"):
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
        if _cookie_secure(request):
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response

    @app.exception_handler(NotAuthenticated)
    async def _unauth(request: Request, exc: NotAuthenticated):
        err = getattr(exc, "login_error", None) or getattr(exc, "message", None)
        # Always clear the session cookie so stale roles (e.g. pg_staff after
        # conversion to reseller) cannot loop /login ↔ /dashboard.
        target = f"/login?err={quote(str(err), safe='')}" if err else "/login"
        resp = RedirectResponse(target, status_code=303)
        resp.delete_cookie("session", path="/")
        return resp

    @app.exception_handler(NotAdmin)
    async def _not_admin(request: Request, exc: NotAdmin):
        # Prefer live-ACL redirect from the dependency that raised (avoids stale cookie loops).
        live = getattr(exc, "redirect", None)
        if live:
            if live != request.url.path:
                return RedirectResponse(live, status_code=303)
            return RedirectResponse("/logout", status_code=303)
        user = None
        try:
            cookie = request.cookies.get("session")
            if cookie:
                user = get_signer().loads(cookie, max_age=SESSION_MAX_AGE)
        except Exception:
            user = None
        role = (user or {}).get("role")
        if role in {"reseller", "pg_staff", "principal"}:
            pg = (user or {}).get("pg_permissions") or []
            if role in {"pg_staff", "principal"}:
                return RedirectResponse("/pg", status_code=303)
            for path in (
                "/home",
                "/pg/users" if "pg_users" in pg else None,
                "/pg" if pg else None,
            ):
                if path and path != request.url.path:
                    return RedirectResponse(path, status_code=303)
            perms = (user or {}).get("permissions") or []
            for path, key in (
                ("/home", "dashboard"),
                ("/dashboard", "dashboard"),
                ("/plans", "plans"),
                ("/finance", "orders"),
                ("/finance", "payments"),
                ("/tickets", "tickets"),
                ("/loyalty", "loyalty"),
                ("/shop-settings", "shop_settings"),
            ):
                if key in perms and path != request.url.path:
                    return RedirectResponse(path, status_code=303)
            return RedirectResponse("/home", status_code=303)
        return RedirectResponse("/home", status_code=303)

    def _status_page(
        request: Request,
        *,
        code: int,
        title: str,
        message: str,
        ref: str | None = None,
        primary_href: str = "/home",
        primary_label: str = "بازگشت به داشبورد",
        secondary_href: str = "/logout",
        secondary_label: str = "خروج",
    ):
        from app.version import __version__ as _ver

        try:
            from app.services.pwa import panel_display_name

            pname = panel_display_name()
        except Exception:
            pname = "MrClockBot"
        return templates.TemplateResponse(
            request,
            "panel_status.html",
            {
                "code": code,
                "title": title,
                "message": message,
                "ref": ref,
                "primary_href": primary_href,
                "primary_label": primary_label,
                "secondary_href": secondary_href,
                "secondary_label": secondary_label,
                "app_version": _ver,
                "pwa_name": pname,
            },
            status_code=code,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        accept = (request.headers.get("accept") or "").lower()
        wants_json = (
            "application/json" in accept
            or request.url.path.startswith("/api")
            or request.url.path.endswith(".json")
        )
        if wants_json:
            detail = exc.detail if isinstance(exc.detail, (str, dict, list)) else "خطا"
            return JSONResponse({"detail": detail}, status_code=exc.status_code)
        if exc.status_code == 404:
            return _status_page(
                request,
                code=404,
                title="صفحه پیدا نشد",
                message="این آدرس وجود ندارد یا جابه‌جا شده است.",
                primary_href="/home",
                primary_label="بازگشت به داشبورد",
                secondary_href="/login",
                secondary_label="صفحه ورود",
            )
        if exc.status_code in {401, 403}:
            return _status_page(
                request,
                code=exc.status_code,
                title="دسترسی مجاز نیست",
                message="برای دیدن این بخش وارد شوید یا سطح دسترسی کافی ندارید.",
                primary_href="/login",
                primary_label="صفحه ورود",
                secondary_href="/logout",
                secondary_label="خروج",
            )
        # Other HTTP errors → branded status without leaking details
        msg = "درخواست قابل انجام نیست."
        if isinstance(exc.detail, str) and exc.detail and len(exc.detail) < 120:
            # Only safe short Persian product messages (no traces)
            if not any(c in exc.detail for c in ("\n", "Traceback", "/", "\\")):
                msg = exc.detail
        return _status_page(
            request,
            code=exc.status_code,
            title="خطا",
            message=msg,
            primary_href="/home",
            primary_label="بازگشت به داشبورد",
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        """Last-resort HTML/JSON 500 — log server-side only; never echo secrets/traces."""
        import uuid

        ref = uuid.uuid4().hex[:8]
        logging.getLogger(__name__).exception(
            "unhandled error ref=%s method=%s path=%s",
            ref,
            request.method,
            request.url.path,
        )
        accept = (request.headers.get("accept") or "").lower()
        wants_json = (
            "application/json" in accept
            or request.url.path.startswith("/home/metrics")
            or request.url.path.startswith("/dashboard/metrics")
            or request.url.path.startswith("/pg/metrics")
            or request.url.path.startswith("/api")
            or request.url.path.endswith(".json")
        )
        if wants_json:
            return JSONResponse(
                {"detail": "خطای داخلی سرور", "ref": ref}, status_code=500
            )
        return _status_page(
            request,
            code=500,
            title="خطای داخلی سرور",
            message="مشکلی پیش آمد. چند لحظه دیگر دوباره تلاش کنید.",
            ref=ref,
            primary_href="/login",
            primary_label="صفحه ورود",
            secondary_href="/logout",
            secondary_label="خروج",
        )

    register_home_pages(
        app,
        render=render,
        require_admin=require_admin,
        require_staff=require_staff,
        get_db=get_db,
    )
    register_pg_pages(
        app,
        render=render,
        require_pg_perm=require_pg_perm,
        get_db=get_db,
    )
    from app.api.reseller_pages import register_reseller_pages
    from app.api.security import register_security_pages
    from app.api.shop_settings import register_shop_settings
    from app.api.user_pages import register_user_pages

    register_reseller_pages(
        app, render=render, require_admin=require_admin, require_staff=require_staff, get_db=get_db
    )
    from app.api.principal_pages import register_principal_pages

    register_principal_pages(
        app,
        render=render,
        require_admin=require_admin,
        require_staff=require_staff,
        get_db=get_db,
    )
    register_user_pages(
        app,
        render=render,
        require_admin=require_admin,
        require_perm=require_perm,
        get_db=get_db,
    )
    from app.api.bulk_pages import register_bulk_pages

    register_bulk_pages(
        app,
        require_admin=require_admin,
        require_perm=require_perm,
        require_pg_perm=require_pg_perm,
        require_staff=require_staff,
        get_db=get_db,
    )
    from app.api.finance_pages import register_finance_pages
    register_finance_pages(
        app,
        render=render,
        require_staff=require_staff,
        get_db=get_db,
    )
    from app.api.settlement_pages import register_settlement_pages

    register_settlement_pages(app, get_db=get_db)
    from app.api.ux20_pages import register_ux20_pages
    register_ux20_pages(
        app,
        render=render,
        require_staff=require_staff,
        require_admin=require_admin,
        get_db=get_db,
    )
    register_shop_settings(
        app,
        render=render,
        require_staff=require_staff,
        require_shop_settings=require_perm("shop_settings"),
        get_db=get_db,
    )
    from app.api.loyalty_pages import register_loyalty_pages

    register_loyalty_pages(
        app,
        render=render,
        require_perm=require_perm,
        require_admin=require_admin,
        get_db=get_db,
    )
    from app.api.message_variables_pages import register_message_variables_pages

    register_message_variables_pages(
        app,
        render=render,
        require_perm=require_perm,
        require_admin=require_admin,
        get_db=get_db,
    )
    from app.api.backup_pages import register_backup_pages

    register_backup_pages(app, render=render, require_admin=require_admin, get_db=get_db)
    register_security_pages(
        app,
        render=render,
        require_staff=require_staff,
        get_db=get_db,
        get_signer=get_signer,
        cookie_secure=_cookie_secure,
    )
    from app.api.panel_tickets_pages import register_panel_tickets_pages

    register_panel_tickets_pages(
        app,
        render=render,
        require_staff=require_staff,
        get_db=get_db,
    )
    from app.api.miniapp_pages import register_miniapp_pages

    register_miniapp_pages(app, render=render, get_db=get_db)

    @app.get("/settings/ssl/progress")
    async def ssl_progress(staff: dict = Depends(require_admin)):
        from fastapi.responses import JSONResponse

        from app.services.ssl_certs import read_progress

        return JSONResponse(read_progress())

    @app.get("/health")
    async def health():
        from fastapi.responses import JSONResponse

        # Public probe — CORS allows HTTPS health check from HTTP settings during SSL restart
        return JSONResponse(
            {"ok": True},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.get("/health/detail")
    async def health_detail(staff: dict = Depends(require_admin)):
        from app.runtime import BOOT_AT, BOOT_ID, PID
        from app.services.updates import local_version

        creds = load_web_admin()
        settings = get_settings()
        webhook_base = normalize_webhook_base_url(settings.webhook_url)
        webhook_path = normalize_webhook_path(settings.webhook_path)
        return {
            "ok": True,
            "web_panel": True,
            "admin_user_configured": bool(creds.get("username") and creds.get("password")),
            "setup_complete": is_setup_complete(),
            "version": local_version(),
            "boot_id": BOOT_ID,
            "boot_at": BOOT_AT,
            "pid": PID,
            "staff": staff.get("username"),
            "bot_update_mode": "webhook" if webhook_base else "polling",
            "webhook_url": webhook_base,
            "webhook_path": webhook_path,
            "webhook_full_url": (webhook_base + webhook_path) if webhook_base else "",
        }

    @app.get("/manifest.webmanifest")
    async def pwa_manifest(session: AsyncSession = Depends(get_db)):
        from fastapi.responses import JSONResponse

        from app.services.pwa import build_manifest, load_pwa_settings

        cfg = await load_pwa_settings(session)
        resp = JSONResponse(build_manifest(cfg), media_type="application/manifest+json")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/sw.js")
    async def pwa_service_worker():
        from fastapi.responses import Response

        from app.services.pwa import service_worker_js

        return Response(
            service_worker_js(),
            media_type="application/javascript; charset=utf-8",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/pwa/icon/{size}")
    async def pwa_icon(size: int, request: Request):
        from fastapi.responses import Response

        from app.services.pwa import icon_bytes

        if size not in (180, 192, 512):
            raise HTTPException(status_code=404)
        maskable = request.query_params.get("maskable") in {"1", "true", "yes"} and size == 512
        data = icon_bytes(size, maskable=maskable)
        if not data:
            raise HTTPException(status_code=404)
        return Response(
            data,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    # -------- Setup wizard --------
    async def _setup_page(
        request: Request,
        *,
        step: int = 0,
        err: str | None = None,
        ok: str | None = None,
        show_done: bool = False,
        pg_audit: dict | None = None,
    ):
        begin_setup()
        values = current_setup_values()
        from app.services.security_policy import is_placeholder_bot_token

        has_bot_token = bool((values.get("BOT_TOKEN") or "").strip()) and not is_placeholder_bot_token(
            values.get("BOT_TOKEN")
        )
        has_pg_password = bool((values.get("PG_PASSWORD") or "").strip())
        # Never echo live secrets back into the HTML source — a re-run of the
        # wizard (e.g. to change one unrelated field) must not require
        # re-typing the bot token / PG password, but it also must not leak
        # them in the page source. Leave both blank; the empty-field-keeps-
        # previous-value logic lives in setup_bot()/setup_other().
        display_values = dict(values)
        display_values["BOT_TOKEN"] = ""
        display_values["PG_PASSWORD"] = ""
        if pg_audit is None and (show_done or step >= 4):
            try:
                pg_audit = await setup_pg_access_audit()
            except Exception:
                pg_audit = None
        return render(
            request,
            "setup.html",
            {
                "values": display_values,
                "has_bot_token": has_bot_token,
                "has_pg_password": has_pg_password,
                "initial_step": step,
                "show_done": show_done,
                "flash_err": err or request.query_params.get("err"),
                "flash_ok": ok or request.query_params.get("ok"),
                "panel_url": wizard_panel_url_hint(values.get("WEB_PORT", "9000")),
                "finish_login_url": setup_finish_login_url(),
                "bot_username": (values.get("BOT_USERNAME") or "").lstrip("@"),
                "pg_audit": pg_audit,
            },
        )

    def _setup_wants_json(request: Request) -> bool:
        return (request.headers.get("x-requested-with") or "").strip() == "setup-probe"

    @app.get("/setup", response_class=HTMLResponse)
    async def setup_page(request: Request):
        if is_setup_complete():
            return RedirectResponse("/", status_code=303)
        step = 0
        try:
            step = int(request.query_params.get("step") or "0")
        except ValueError:
            step = 0
        show_done = step >= 4
        return await _setup_page(request, step=step if step < 4 else 4, show_done=show_done)

    @app.post("/setup/admin")
    async def setup_admin(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
    ):
        if is_setup_complete():
            return RedirectResponse("/", status_code=303)
        begin_setup()
        user = (username or "").strip() or "admin"
        p1 = password or ""
        p2 = password_confirm or ""
        if p1 != p2:
            return await _setup_page(request, step=1, err="رمز عبور و تکرار آن یکسان نیستند.")
        ok, msg = validate_password_strength(p1)
        if not ok:
            return await _setup_page(request, step=1, err=msg)
        try:
            save_web_admin(user, p1)
        except ValueError as e:
            return await _setup_page(request, step=1, err=str(e))
        ensure_web_secret()
        update_env_keys({"WEB_ADMIN_USER": user})
        return RedirectResponse("/setup?step=2", status_code=303)

    @app.post("/setup/bot")
    async def setup_bot(
        request: Request,
        bot_token: str = Form(...),
        bot_username: str = Form(""),
        admin_ids: str = Form(...),
    ):
        if is_setup_complete():
            return RedirectResponse("/", status_code=303)
        begin_setup()
        from app.services.security_policy import is_placeholder_bot_token

        token = (bot_token or "").strip()
        uname = (bot_username or "").strip().lstrip("@")
        ids_raw = (admin_ids or "").strip()
        if not token:
            # Wizard never re-displays the saved token (see _setup_page) — an
            # empty submit here means "keep the existing one", not "clear it".
            existing = (current_setup_values().get("BOT_TOKEN") or "").strip()
            if existing and not is_placeholder_bot_token(existing):
                token = existing
            else:
                return await _setup_page(request, step=2, err="توکن ربات الزامی است.")
        if is_placeholder_bot_token(token):
            return await _setup_page(request, step=2, err="توکن ربات نامعتبر است — یک توکن واقعی از BotFather وارد کنید.")
        if not uname:
            return await _setup_page(request, step=2, err="نام کاربری ربات الزامی است.")
        try:
            ids = parse_admin_ids(ids_raw)
        except ValueError:
            return await _setup_page(request, step=2, err="آیدی ادمین‌ها باید عدد باشد (با کاما جدا کنید).")
        if not ids:
            return await _setup_page(request, step=2, err="حداقل یک آیدی ادمین وارد کنید.")
        update_env_keys(
            {
                "BOT_TOKEN": token,
                "BOT_USERNAME": uname,
                "ADMIN_IDS": ",".join(str(i) for i in ids),
            }
        )
        ensure_web_secret()
        return RedirectResponse("/setup?step=3", status_code=303)

    @app.post("/setup/other")
    async def setup_other(
        request: Request,
        pg_base_url: str = Form(...),
        pg_username: str = Form(""),
        pg_password: str = Form(""),
        web_port: str = Form("9000"),
        public_base_url: str = Form(""),
        currency: str = Form("تومان"),
    ):
        async def fail(err: str):
            if _setup_wants_json(request):
                return JSONResponse({"ok": False, "error": err}, status_code=400)
            return await _setup_page(request, step=3, err=err)

        if is_setup_complete():
            if _setup_wants_json(request):
                return JSONResponse({"ok": False, "error": "راه‌اندازی قبلاً کامل شده است"}, status_code=400)
            return RedirectResponse("/", status_code=303)
        begin_setup()
        base = normalize_pg_base_url((pg_base_url or "").strip())
        if not base:
            return await fail("آدرس پاسارگارد الزامی است.")
        if not (pg_username or "").strip():
            return await fail("نام کاربری پاسارگارد الزامی است.")
        if not (pg_password or "").strip():
            # Wizard never re-displays the saved password (see _setup_page) —
            # an empty submit here means "keep the existing one".
            existing_pw = (current_setup_values().get("PG_PASSWORD") or "").strip()
            if existing_pw:
                pg_password = existing_pw
            else:
                return await fail("رمز پاسارگارد الزامی است.")
        port = (web_port or "9000").strip()
        try:
            port_n = int(port)
            if port_n < 1 or port_n > 65535:
                raise ValueError
        except ValueError:
            return await fail("پورت وب نامعتبر است.")
        # Hybrid: probe credentials + role before saving (fail closed on bad login).
        from app.services.pg_access import (
            clear_platform_pg_capability_cache,
            resolve_platform_pg_capabilities,
        )

        caps = await resolve_platform_pg_capabilities(
            username=(pg_username or "").strip(),
            password=(pg_password or "").strip(),
            base_url=base,
            use_cache=False,
        )
        if not caps.get("ok"):
            detail = caps.get("error") or "نامعتبر"
            return await fail(
                f"ورود به پاسارگارد ناموفق بود: {detail}. "
                "علت محتمل: آدرس/یوزر/رمز اشتباه. "
                "راه حل: همان اعتبارنامه ورود پنل پاسارگارد را وارد کنید."
            )
        ensure_web_secret()
        pub = (public_base_url or "").strip().rstrip("/")
        if pub.startswith("https://"):
            pub = "http://" + pub[len("https://") :]
        update_env_keys(
            {
                "PG_BASE_URL": base,
                "PG_USERNAME": (pg_username or "").strip(),
                "PG_PASSWORD": (pg_password or "").strip(),
                "WEB_PORT": str(port_n),
                "PUBLIC_BASE_URL": pub,
                "CURRENCY": (currency or "").strip() or "تومان",
            }
        )
        clear_platform_pg_capability_cache()
        if _setup_wants_json(request):
            return JSONResponse({"ok": True, "next": "/setup?step=4"})
        return RedirectResponse("/setup?step=4", status_code=303)

    @app.post("/setup/finish")
    async def setup_finish(request: Request):
        mark_setup_complete()
        ensure_web_secret()
        from app.services.service_control import schedule_panel_restart
        from app.services.setup_wizard import force_http_login_url

        schedule_panel_restart(delay_sec=2.5, reason="setup wizard finished")
        # Prefer an explicit next from the wizard page (matches the displayed
        # panel URL). Always coerce to http — SSL is not ready on first entry.
        next_url = ""
        try:
            form = await request.form()
            next_url = str(form.get("next") or "").strip()
        except Exception:
            next_url = ""
        return RedirectResponse(force_http_login_url(next_url), status_code=303)

    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        """Single entry URL: first-run → setup wizard, otherwise login/dashboard."""
        if not is_setup_complete():
            step = 0
            try:
                step = int(request.query_params.get("step") or "0")
            except ValueError:
                step = 0
            show_done = step >= 4
            return await _setup_page(request, step=step if step < 4 else 4, show_done=show_done)
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=303)
        if user.get("role") == "admin":
            return RedirectResponse("/home", status_code=303)
        if user.get("role") in {"pg_staff", "principal"}:
            # Legacy pg_staff / Level-1 Principal → PG home
            return RedirectResponse("/pg", status_code=303)
        # Reseller / sub-admin → web dashboard (bot + PG summaries)
        return RedirectResponse("/home", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        if not is_setup_complete():
            return RedirectResponse("/", status_code=303)
        err = request.query_params.get("err")
        # With an error (often after cookie clear), always show the form.
        if not err:
            sess = get_session_user(request)
            if sess:
                if sess.get("role") == "admin":
                    return _panel_redirect(request, "/home")
                if sess.get("role") in {"pg_staff", "principal"}:
                    return _panel_redirect(request, "/pg")
                # Reseller / sub-admin: web dashboard
                return _panel_redirect(request, "/home")
        page = render(
            request,
            "login.html",
            {
                "error": err,
                "username": "",
                "flash_ok": request.query_params.get("ok"),
            },
        )
        if err:
            # Belt-and-suspenders: drop any leftover session when showing an auth error.
            page.delete_cookie("session", path="/")
        return page

    @app.post("/login")
    async def login_submit(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        session: AsyncSession = Depends(get_db),
    ):
        if not is_setup_complete():
            return RedirectResponse("/", status_code=303)

        ip = _client_ip(request)
        typed_user = (username or "").strip()
        # Rate-limit by IP and by username+IP so XFF spoofing (when TRUST_PROXY=1) is harder.
        limit_keys = [ip, f"{ip}|{typed_user.lower()}"]
        if any(_login_blocked(k) for k in limit_keys):
            return render(
                request,
                "login.html",
                {
                    "error": "تعداد تلاش‌های ناموفق زیاد است. ۱۵ دقیقه دیگر دوباره تلاش کنید.",
                    "username": typed_user,
                },
                status_code=429,
            )

        role = None
        display = (username or "").strip()
        u = display
        p = password or ""
        # Autofill / password-managers sometimes omit fields — never return raw 422 JSON.
        if not typed_user or not p:
            return render(
                request,
                "login.html",
                {
                    "error": "نام کاربری و رمز دسترسی الزامی است.",
                    "username": typed_user,
                },
                status_code=400,
            )
        permissions: list[str] = []
        pg_permissions: list[str] = []
        pg_user_actions: dict = {}
        pg_access: dict = {}
        pg_writes: dict = {}
        pg_admin_username = None
        bot_user_id = None
        pg_role_id = None
        reseller_pv = ""
        pg_credentials_ready = False
        pg_staff_id = None

        if verify_web_admin(u, p):
            role = "admin"
            display = load_web_admin()["username"]
        else:
            # Reseller web credentials (self-serve wizard only — no referral-code login)
            from app.services.pg_staff_access import (
                PG_ACCESS_DENIED_MSG,
                access_by_web_username,
                enforce_pg_admin_web_gate,
                resolve_pg_role_id_for_admin,
                staff_has_stored_pg_password,
            )

            result = await session.execute(
                select(BotUser, ResellerProfile)
                .join(ResellerProfile, ResellerProfile.user_id == BotUser.id)
                .where(
                    ResellerProfile.web_username == u.strip().lower(),
                    BotUser.role == Role.RESELLER.value,
                )
            )
            row = result.first()
            if row:
                ru, profile = row
                if verify_password_hash(p, profile.web_password_hash):
                    if not profile.is_active:
                        for k in limit_keys:
                            _login_fail(k)
                        return render(
                            request,
                            "login.html",
                            {"error": PG_ACCESS_DENIED_MSG, "username": typed_user},
                            status_code=403,
                        )
                    if not setup_is_complete(profile):
                        for k in limit_keys:
                            _login_fail(k)
                        return render(
                            request,
                            "login.html",
                            {
                                "error": "راه‌اندازی پنل هنوز کامل نشده. از لینک تلگرام استفاده کنید.",
                                "username": typed_user,
                            },
                            status_code=400,
                        )
                    if profile.pg_admin_username:
                        allowed, deny_msg = await enforce_pg_admin_web_gate(
                            session, profile.pg_admin_username, revoke_if_missing=True
                        )
                        if not allowed:
                            for k in limit_keys:
                                _login_fail(k)
                            return render(
                                request,
                                "login.html",
                                {
                                    "error": deny_msg or PG_ACCESS_DENIED_MSG,
                                    "username": typed_user,
                                },
                                status_code=403,
                            )
                    from app.services.pg_access import (
                        map_pg_role_writes,
                        resolve_reseller_pg_features,
                        role_access_limits,
                        role_user_actions,
                    )

                    role = "reseller"
                    display = profile.web_username or ru.full_name or str(ru.telegram_id)
                    # Same ACL resolution as Bot (empty = deny; None = DEFAULT)
                    from app.services.authz import resolve_shop_permissions_from_profile

                    permissions = list(resolve_shop_permissions_from_profile(profile) or [])
                    bot_user_id = ru.id
                    pg_admin_username = profile.pg_admin_username
                    pg_role_id = int(profile.pg_role_id) if profile.pg_role_id else None
                    pg_client = None
                    if profile.pg_admin_username:
                        try:
                            from app.services.pasarguard import get_pg_for_reseller

                            pg_client = await get_pg_for_reseller(
                                session, int(profile.user_id)
                            )
                        except Exception:
                            pg_client = None
                        if pg_client is not None:
                            live_role = await resolve_pg_role_id_for_admin(
                                profile.pg_admin_username, client=pg_client
                            )
                            if live_role:
                                pg_role_id = int(live_role)
                                if profile.pg_role_id != pg_role_id:
                                    profile.pg_role_id = pg_role_id
                                    try:
                                        await session.commit()
                                    except Exception:
                                        await session.rollback()
                    if pg_client is not None:
                        pg_permissions, pg_role = await resolve_reseller_pg_features(
                            pg_role_id, client=pg_client
                        )
                    else:
                        pg_permissions, pg_role = [], None
                    pg_user_actions = role_user_actions(pg_role)
                    pg_access = role_access_limits(pg_role)
                    pg_writes = map_pg_role_writes(pg_role)
                    reseller_pv = (profile.web_password_hash or "")[:24]
            if not role:
                from app.services.pg_access import (
                    map_pg_role_writes,
                    resolve_reseller_pg_features,
                    role_access_limits,
                    role_user_actions,
                )

                staff_row = await access_by_web_username(session, u.strip().lower())
                if staff_row and verify_password_hash(p, staff_row.web_password_hash):
                    if not staff_row.is_active:
                        for k in limit_keys:
                            _login_fail(k)
                        return render(
                            request,
                            "login.html",
                            {"error": PG_ACCESS_DENIED_MSG, "username": typed_user},
                            status_code=403,
                        )
                    allowed, deny_msg = await enforce_pg_admin_web_gate(
                        session, staff_row.pg_username, revoke_if_missing=True
                    )
                    if not allowed:
                        for k in limit_keys:
                            _login_fail(k)
                        return render(
                            request,
                            "login.html",
                            {
                                "error": deny_msg or PG_ACCESS_DENIED_MSG,
                                "username": typed_user,
                            },
                            status_code=403,
                        )
                    # Re-load in case revoke removed the row
                    staff_row = await access_by_web_username(session, u.strip().lower())
                    if not staff_row or not staff_row.is_active:
                        for k in limit_keys:
                            _login_fail(k)
                        return render(
                            request,
                            "login.html",
                            {"error": PG_ACCESS_DENIED_MSG, "username": typed_user},
                            status_code=403,
                        )
                    role = "pg_staff"
                    display = staff_row.web_username
                    permissions = []
                    pg_admin_username = staff_row.pg_username
                    pg_credentials_ready = staff_has_stored_pg_password(staff_row)
                    pg_staff_id = int(staff_row.id)
                    # Prefer stored role; refresh live when reachable
                    pg_role_id = (
                        int(staff_row.pg_role_id) if staff_row.pg_role_id else None
                    )
                    pg_client = None
                    try:
                        from app.services.pasarguard import get_pg_for_staff

                        pg_client = await get_pg_for_staff(
                            session, staff_id=int(staff_row.id)
                        )
                    except Exception:
                        pg_client = None
                    if pg_client is not None:
                        live_role = await resolve_pg_role_id_for_admin(
                            staff_row.pg_username, client=pg_client
                        )
                        if live_role:
                            pg_role_id = live_role
                        pg_permissions, pg_role = await resolve_reseller_pg_features(
                            pg_role_id, client=pg_client
                        )
                    else:
                        pg_permissions, pg_role = [], None
                    pg_user_actions = role_user_actions(pg_role)
                    pg_access = role_access_limits(pg_role)
                    pg_writes = map_pg_role_writes(pg_role)
                    reseller_pv = (staff_row.web_password_hash or "")[:24]
                    if not pg_permissions:
                        for k in limit_keys:
                            _login_fail(k)
                        return render(
                            request,
                            "login.html",
                            {
                                "error": "نقش پاسارگارد این ادمین هیچ دسترسی قابل‌نمایشی در وب‌پنل ندارد.",
                                "username": typed_user,
                            },
                            status_code=400,
                        )

            if not role:
                # Phase 2B: Level-1 OrgPrincipal independent Web identity
                from app.services.principal_web_identity import (
                    PrincipalWebIdentityError,
                    authenticate_level1_web,
                    build_principal_session_payload,
                )
                from app.services.pg_access import (
                    map_pg_role_writes,
                    resolve_reseller_pg_features,
                    role_access_limits,
                    role_user_actions,
                )
                from app.services.pg_staff_access import resolve_pg_role_id_for_admin

                try:
                    auth = await authenticate_level1_web(
                        session, username=u, password=p
                    )
                except PrincipalWebIdentityError as exc:
                    for k in limit_keys:
                        _login_fail(k)
                    return render(
                        request,
                        "login.html",
                        {"error": exc.message, "username": typed_user},
                        status_code=403,
                    )
                if auth is not None:
                    pg_role_id = None
                    pg_permissions = []
                    pg_user_actions = {}
                    pg_access = {}
                    pg_writes = {}
                    pg_client = None
                    if auth.pg_username:
                        try:
                            from app.services.pasarguard import get_pg_for_principal

                            pg_client = await get_pg_for_principal(
                                session, principal_id=int(auth.principal.id)
                            )
                        except Exception:
                            pg_client = None
                        if pg_client is not None:
                            live_role = await resolve_pg_role_id_for_admin(
                                auth.pg_username, client=pg_client
                            )
                            if live_role:
                                pg_role_id = int(live_role)
                    if pg_role_id and pg_client is not None:
                        pg_permissions, pg_role = await resolve_reseller_pg_features(
                            pg_role_id, client=pg_client
                        )
                        pg_user_actions = role_user_actions(pg_role)
                        pg_access = role_access_limits(pg_role)
                        pg_writes = map_pg_role_writes(pg_role)
                    for k in limit_keys:
                        _login_success(k)
                    payload = build_principal_session_payload(
                        auth,
                        permissions=[],
                        pg_permissions=pg_permissions,
                        pg_user_actions=pg_user_actions,
                        pg_access=pg_access,
                        pg_writes=pg_writes,
                        pg_role_id=pg_role_id,
                    )
                    home = "/pg" if pg_permissions else "/home"
                    resp = _panel_redirect(request, home)
                    try:
                        cookie_val = get_signer().dumps(payload)
                    except Exception:
                        logging.getLogger(__name__).exception("session cookie sign failed")
                        return render(
                            request,
                            "login.html",
                            {
                                "error": "خطای داخلی نشست — دوباره تلاش کنید",
                                "username": typed_user,
                            },
                            status_code=500,
                        )
                    resp.set_cookie(
                        "session",
                        cookie_val,
                        httponly=True,
                        samesite="lax",
                        max_age=SESSION_MAX_AGE,
                        secure=_cookie_secure(request),
                        path="/",
                    )
                    try:
                        revoke_setup_gate()
                    except Exception:
                        logging.getLogger(__name__).exception("revoke_setup_gate failed")
                    resp.delete_cookie("setup_gate", path="/")
                    return resp

        if not role:
            for k in limit_keys:
                _login_fail(k)
            return render(
                request,
                "login.html",
                {
                    "error": "نام کاربری یا رمز عبور اشتباه است. اگر تازه نصب کرده‌اید: python scripts/set_web_password.py",
                    "username": typed_user,
                },
                status_code=400,
            )

        for k in limit_keys:
            _login_success(k)
        payload = {
            "role": role,
            "username": display,
            "permissions": permissions,
            "pg_permissions": pg_permissions,
        }
        if role == "admin":
            payload["sv"] = admin_session_version()
            payload["web_owner"] = True
        if bot_user_id is not None:
            payload["bot_user_id"] = bot_user_id
        if pg_admin_username:
            payload["pg_admin_username"] = pg_admin_username
        if role in {"reseller", "pg_staff"}:
            payload["pg_user_actions"] = pg_user_actions
            payload["pg_access"] = pg_access
            payload["pg_writes"] = pg_writes
            if pg_role_id:
                payload["pg_role_id"] = int(pg_role_id)
            if reseller_pv:
                payload["pv"] = reseller_pv
        if role == "pg_staff":
            payload["pg_credentials_ready"] = bool(pg_credentials_ready)
            if pg_staff_id is not None:
                payload["pg_staff_id"] = int(pg_staff_id)
        home = "/home" if role == "admin" else "/home"
        if role == "pg_staff":
            home = "/pg"
        elif role == "reseller":
            # Prefer web dashboard; fall back by shop/PG perms.
            home = "/home"
            if "dashboard" not in permissions and not pg_permissions:
                home = ""
                for path, key in (
                    ("/plans", "plans"),
                    ("/finance", "orders"),
                    ("/finance", "payments"),
                    ("/tickets", "tickets"),
                    ("/shop-settings", "shop_settings"),
                ):
                    if key in permissions:
                        home = path
                        break
            if not home:
                if "pg_users" in pg_permissions:
                    home = "/pg/users"
                elif "pg_overview" in pg_permissions:
                    home = "/pg"
                elif pg_permissions:
                    home = "/pg"
                else:
                    home = "/logout"
        resp = _panel_redirect(request, home)
        try:
            cookie_val = get_signer().dumps(payload)
        except Exception:
            logging.getLogger(__name__).exception("session cookie sign failed")
            return render(
                request,
                "login.html",
                {
                    "error": "ورود انجام شد ولی ذخیره نشست ناموفق بود. WEB_SECRET یا دسترسی نوشتن .env را بررسی کنید.",
                    "username": typed_user,
                },
                status_code=500,
            )
        resp.set_cookie(
            "session",
            cookie_val,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
            max_age=60 * 60 * 24 * 7,
            path="/",
        )
        try:
            revoke_setup_gate()
        except Exception:
            logging.getLogger(__name__).exception("revoke_setup_gate failed")
        resp.delete_cookie("setup_gate", path="/")
        return resp

    @app.post("/logout")
    async def logout_post():
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("session", path="/")
        return resp

    @app.get("/logout")
    async def logout():
        # GET kept for bookmark/back-compat; prefer POST from the panel UI.
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("session", path="/")
        return resp

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(
        request: Request,
        staff: dict = Depends(require_perm("dashboard")),
        session: AsyncSession = Depends(get_db),
    ):
        # Bot overview (/dashboard): platform admin sees bot boxes;
        # overall web home stays at /home. Resellers stay shop-scoped.
        from sqlalchemy import or_

        from app.services.shop_scope import empty_shop_stats, is_platform_admin, shop_owner_id

        rid = shop_owner_id(staff)
        stats = empty_shop_stats()
        recent_payments: list = []
        recent_orders: list = []
        bot_setup_needed = False
        host_gauges = None

        if is_platform_admin(staff):
            import asyncio

            from app.services.db_safe import rollback_quiet
            from app.services.home_overview import bot_panel_summary
            from app.services.host_gauges import local_host_gauges

            try:
                host_gauges = await asyncio.to_thread(local_host_gauges, wait_cpu=0.0)
            except Exception:
                from app.services.host_gauges import empty_host_gauges

                host_gauges = empty_host_gauges()

            try:
                summary = await bot_panel_summary(session)
            except Exception:
                await rollback_quiet(session)
                summary = {
                    "users": 0,
                    "orders": 0,
                    "services": 0,
                    "pending": 0,
                    "revenue": 0,
                    "tickets": 0,
                    "resellers": 0,
                }
            try:
                plans_count = (
                    await session.scalar(
                        select(func.count())
                        .select_from(Plan)
                        .where(Plan.is_active.is_(True), Plan.owner_reseller_id.is_(None))
                    )
                    or 0
                )
            except Exception:
                await rollback_quiet(session)
                plans_count = 0
            stats = {
                "users": summary["users"],
                "orders": summary["orders"],
                "pending": summary["pending"],
                "services": summary["services"],
                "revenue": summary["revenue"],
                "plans": int(plans_count),
                "tickets": summary["tickets"],
            }
            # Hard shop isolation: platform dashboard never lists tenant shop traffic
            try:
                recent_payments = list(
                    (
                        await session.execute(
                            select(Payment)
                            .outerjoin(Order, Order.id == Payment.order_id)
                            .where(
                                or_(
                                    Payment.is_wallet_topup.is_(True),
                                    Order.reseller_id.is_(None),
                                )
                            )
                            .order_by(Payment.id.desc())
                            .limit(6)
                        )
                    ).scalars().all()
                )
                recent_orders = list(
                    (
                        await session.execute(
                            select(Order)
                            .where(Order.reseller_id.is_(None))
                            .order_by(Order.id.desc())
                            .limit(6)
                        )
                    ).scalars().all()
                )
            except Exception:
                await rollback_quiet(session)
                recent_payments = []
                recent_orders = []
            return render(
                request,
                "dashboard.html",
                {
                    "staff": staff,
                    "stats": stats,
                    "recent_payments": recent_payments,
                    "recent_orders": recent_orders,
                    "bot_setup_needed": False,
                    "host_gauges": host_gauges,
                },
            )

        # Fail closed: never fall through to platform/admin aggregates.
        # Bot نمای کلی is bot-only (PG lives on /home and /pg).
        if rid:
            from app.db.models import ResellerProfile
            from app.services.resellers import bot_needs_setup

            profile = (
                await session.execute(
                    select(ResellerProfile).where(ResellerProfile.user_id == int(rid))
                )
            ).scalar_one_or_none()
            bot_setup_needed = bot_needs_setup(profile)
            # Until dedicated bot token is set, bot overview is setup-only.
            if bot_setup_needed:
                return render(
                    request,
                    "dashboard.html",
                    {
                        "staff": staff,
                        "stats": stats,
                        "recent_payments": [],
                        "recent_orders": [],
                        "bot_setup_needed": True,
                    },
                )

            from app.api.home_pages import _reseller_shop_stats

            perms = staff.get("permissions") or []
            stats = await _reseller_shop_stats(session, int(rid))
            if "payments" in perms:
                recent_payments = list(
                    (
                        await session.execute(
                            select(Payment)
                            .join(BotUser, BotUser.id == Payment.user_id)
                            .where(BotUser.reseller_id == rid)
                            .order_by(Payment.id.desc())
                            .limit(6)
                        )
                    ).scalars().all()
                )
            if "orders" in perms:
                recent_orders = list(
                    (
                        await session.execute(
                            select(Order)
                            .where(Order.reseller_id == rid)
                            .order_by(Order.id.desc())
                            .limit(6)
                        )
                    ).scalars().all()
                )

        return render(
            request,
            "dashboard.html",
            {
                "staff": staff,
                "stats": stats,
                "recent_payments": recent_payments,
                "recent_orders": recent_orders,
                "bot_setup_needed": bot_setup_needed,
                "host_gauges": host_gauges,
            },
        )

    @app.get("/dashboard/metrics")
    async def dashboard_host_metrics_json(staff: dict = Depends(require_admin)):
        """Local bot-server CPU/RAM for the bot overview gauges (platform admin only)."""
        import asyncio

        from fastapi.responses import JSONResponse

        from app.services.host_gauges import gauges_json, local_host_gauges

        host = await asyncio.to_thread(local_host_gauges, wait_cpu=0.0)
        if host.get("cpu_percent") is None:
            host = await asyncio.to_thread(local_host_gauges, wait_cpu=0.12)
        return JSONResponse(gauges_json(host))

    @app.get("/plans", response_class=HTMLResponse)
    async def plans_page(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import (
            catalog_owner_id,
            custom_range_limit_issue,
            list_catalog_plans,
            load_pg_plan_options,
            plan_limit_issue,
            staff_can_create_pg_template,
        )
        from app.services.pg_quota import limit_snapshot_cards, load_staff_limit_snapshot
        from app.services.shop_scope import is_platform_admin

        plans = await list_catalog_plans(session, staff, include_trial=True)
        trial = next((p for p in plans if p.is_trial), None)
        sale_plans = [p for p in plans if not p.is_trial]
        templates, groups, pg_error = await load_pg_plan_options(staff, session=session)
        rid = catalog_owner_id(staff)
        # Non-admin without shop id must never load platform (admin) settings.
        if not is_platform_admin(staff) and not rid:
            values = {}
        else:
            values = await get_all_settings(session, reseller_id=rid)
        limit_snapshot = await load_staff_limit_snapshot(staff, session=session)
        plan_limit_issues: dict[int, str] = {}
        for p in sale_plans:
            issue = await plan_limit_issue(staff, p, session=session)
            if issue:
                plan_limit_issues[int(p.id)] = issue
        trial_limit_issue = await plan_limit_issue(staff, trial, session=session) if trial else None
        try:
            custom_limit_msg = custom_range_limit_issue(
                min_gb=float(values.get("custom_plan_min_gb") or 1),
                max_gb=float(values.get("custom_plan_max_gb") or 500),
                min_days=int(float(values.get("custom_plan_min_days") or 1)),
                max_days=int(float(values.get("custom_plan_max_days") or 365)),
                snapshot=limit_snapshot,
            )
        except (TypeError, ValueError):
            custom_limit_msg = "تنظیمات پلن دلخواه نامعتبر است"
        trial_group_ids = set()
        if trial and trial.pg_group_ids:
            trial_group_ids = {x.strip() for x in trial.pg_group_ids.split(",") if x.strip()}
        custom_group_ids = {
            x.strip()
            for x in (values.get("custom_plan_group_ids") or "").split(",")
            if x.strip()
        }
        reseller_plans = []
        feature_perms = []
        pg_roles = []
        reseller_plans_err = None
        if is_platform_admin(staff):
            from app.services.resellers import FEATURE_PERMS, list_reseller_plans

            feature_perms = FEATURE_PERMS
            try:
                reseller_plans = await list_reseller_plans(session)
            except Exception:
                # Missing additive columns (e.g. billing_mode before migrate) must
                # not 500 the whole plans page — user catalog still loads.
                import logging

                logging.getLogger(__name__).exception("list_reseller_plans failed on /plans")
                reseller_plans = []
                reseller_plans_err = (
                    "بارگذاری پلن‌های نمایندگی ناموفق بود — پس از به‌روزرسانی، سرویس را یک‌بار ری‌استارت کنید."
                )
            try:
                from app.services.pasarguard import get_pg

                pg_roles = await get_pg().get_admin_roles()
            except Exception:
                pg_roles = []

        gift_codes: list = []
        try:
            from app.db.models import ChargeCode

            if is_platform_admin(staff) or rid:
                q = select(ChargeCode).order_by(ChargeCode.id.desc()).limit(200)
                if is_platform_admin(staff):
                    q = q.where(ChargeCode.reseller_id.is_(None))
                else:
                    q = q.where(ChargeCode.reseller_id == int(rid))
                gift_codes = list((await session.execute(q)).scalars().all())
        except Exception:
            import logging

            logging.getLogger(__name__).exception("gift codes load failed on /plans")
            gift_codes = []

        return render(
            request,
            "plans.html",
            {
                "staff": staff,
                "plans": sale_plans,
                "trial": trial,
                "trial_group_ids": trial_group_ids,
                "custom_group_ids": custom_group_ids,
                "values": values,
                "templates": templates,
                "groups": groups,
                "pg_error": pg_error,
                "can_create_template": staff_can_create_pg_template(staff),
                "is_platform_admin": is_platform_admin(staff),
                "reseller_plans": reseller_plans,
                "reseller_plans_err": reseller_plans_err,
                "feature_perms": feature_perms,
                "pg_roles": pg_roles,
                "gift_codes": gift_codes,
                "pg_limit_snapshot": limit_snapshot,
                "pg_limit_cards": limit_snapshot_cards(limit_snapshot),
                "plan_limit_issues": plan_limit_issues,
                "trial_limit_issue": trial_limit_issue,
                "custom_limit_issue": custom_limit_msg,
                "open_gifts": request.query_params.get("gifts") in {"1", "true", "yes"},
                "flash_err": request.query_params.get("err"),
                "flash_ok": request.query_params.get("ok"),
                "plan_style_options": __import__(
                    "app.services.button_styles", fromlist=["PLAN_BUTTON_STYLE_OPTIONS"]
                ).PLAN_BUTTON_STYLE_OPTIONS,
            },
        )

    @app.post("/plans")
    async def plans_create(
        request: Request,
        name: str = Form(...),
        price: int = Form(...),
        duration_days: int = Form(30),
        data_limit_gb: str = Form(""),
        pg_template_id: str = Form(""),
        description: str = Form(""),
        mode: str = Form("custom"),
        also_create_template: str = Form(""),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
        from app.services.plans_catalog import (
            groups_allowed_for_staff,
            parse_group_ids_from_form,
            require_catalog_owner_id,
            staff_can_create_pg_template,
            template_allowed_for_staff,
        )
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        gb = float(data_limit_gb) if str(data_limit_gb).strip() else None
        try:
            await assert_user_plan_within_limits(
                staff,
                data_limit=int(gb * (1024**3)) if gb is not None else None,
                duration_days=duration_days,
                label="پلن فروش",
                session=session,
            )
        except PgQuotaError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        tpl = None
        group_csv = None
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)

        if mode == "template":
            tpl = int(pg_template_id) if str(pg_template_id).strip() else None
            if not tpl:
                return RedirectResponse(
                    f"/plans?err={quote('تمپلیت پاسارگارد را انتخاب کنید')}",
                    status_code=303,
                )
            if not template_allowed_for_staff(staff, tpl):
                return RedirectResponse(
                    f"/plans?err={quote('به این تمپلیت دسترسی ندارید')}",
                    status_code=303,
                )
        else:
            ids = parse_group_ids_from_form(form)
            if not ids:
                return RedirectResponse(
                    f"/plans?err={quote('حداقل یک گروه پاسارگارد انتخاب کنید')}",
                    status_code=303,
                )
            if not groups_allowed_for_staff(staff, ids):
                return RedirectResponse(
                    f"/plans?err={quote('به یکی از گروه‌های انتخاب‌شده دسترسی ندارید')}",
                    status_code=303,
                )
            group_csv = ",".join(str(i) for i in ids)
            if also_create_template:
                if not staff_can_create_pg_template(staff):
                    return RedirectResponse(
                        f"/plans?err={quote('اجازه ساخت تمپلیت در پاسارگارد را ندارید')}",
                        status_code=303,
                    )
                try:
                    # Must use staff PG credentials — never owner token for non-admin
                    from app.api.pg_pages import _staff_pg

                    pg_client, _as_owner = await _staff_pg(session, staff)
                    created = await pg_client.create_user_template(
                        {
                            "name": name.strip(),
                            "group_ids": ids,
                            "expire_duration": duration_days * 86400,
                            "data_limit": int(gb * (1024**3)) if gb is not None else None,
                            "status": "active",
                        }
                    )
                    if isinstance(created, dict) and created.get("id"):
                        tpl = int(created["id"])
                except Exception as e:
                    return RedirectResponse(
                        f"/plans?err={quote(f'ساخت تمپلیت در پاسارگارد ناموفق: {e}')}",
                        status_code=303,
                    )

        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_plan_button_style_form

        uname_prefix, uname_suffix, uname_pattern = parse_naming_form(form)
        button_style = parse_plan_button_style_form(form)

        session.add(
            Plan(
                name=name.strip(),
                price=price,
                duration_days=duration_days,
                data_limit_gb=gb,
                pg_template_id=tpl,
                pg_group_ids=group_csv,
                pg_username_prefix=uname_prefix,
                pg_username_suffix=uname_suffix,
                pg_username_pattern=uname_pattern,
                button_style=button_style,
                owner_reseller_id=owner_id,
                description=description or None,
                is_active=True,
            )
        )
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={quote('پلن ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/trial")
    async def plans_trial_save(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
        from app.services.plans_catalog import (
            groups_allowed_for_staff,
            parse_group_ids_from_form,
            require_catalog_owner_id,
            template_allowed_for_staff,
        )
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        enabled = str(form.get("trial_enabled") or "") in {"1", "on", "true", "yes"}
        await set_setting(session, "trial_enabled", "1" if enabled else "0", reseller_id=owner_id)
        name = str(form.get("name") or "تست رایگان").strip() or "تست رایگان"
        try:
            days = max(1, int(str(form.get("duration_days") or "1")))
        except ValueError:
            days = 1
        gb_raw = str(form.get("data_limit_gb") or "").strip()
        gb = float(gb_raw) if gb_raw else None
        try:
            await assert_user_plan_within_limits(
                staff,
                data_limit=int(gb * (1024**3)) if gb is not None else None,
                duration_days=days,
                label="پلن تست",
                session=session,
            )
        except PgQuotaError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        mode = str(form.get("mode") or "custom")
        tpl = None
        group_csv = None
        if mode == "template":
            tpl_raw = str(form.get("pg_template_id") or "").strip()
            tpl = int(tpl_raw) if tpl_raw.isdigit() else None
            if enabled and not tpl:
                return RedirectResponse(
                    f"/plans?err={quote('برای پلن تست، تمپلیت را انتخاب کنید')}",
                    status_code=303,
                )
            if tpl and not template_allowed_for_staff(staff, tpl):
                return RedirectResponse(
                    f"/plans?err={quote('به این تمپلیت دسترسی ندارید')}",
                    status_code=303,
                )
        else:
            ids = parse_group_ids_from_form(form)
            if enabled and not ids:
                return RedirectResponse(
                    f"/plans?err={quote('برای پلن تست حداقل یک گروه انتخاب کنید')}",
                    status_code=303,
                )
            if ids and not groups_allowed_for_staff(staff, ids):
                return RedirectResponse(
                    f"/plans?err={quote('به یکی از گروه‌های انتخاب‌شده دسترسی ندارید')}",
                    status_code=303,
                )
            group_csv = ",".join(str(i) for i in ids) if ids else None

        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_plan_button_style_form

        uname_prefix, uname_suffix, uname_pattern = parse_naming_form(form)
        button_style = parse_plan_button_style_form(form)

        q = select(Plan).where(Plan.is_trial.is_(True))
        if owner_id:
            q = q.where(Plan.owner_reseller_id == owner_id)
        else:
            q = q.where(Plan.owner_reseller_id.is_(None))
        result = await session.execute(q)
        trial = result.scalar_one_or_none()
        if not trial:
            trial = Plan(
                name=name,
                price=0,
                duration_days=days,
                data_limit_gb=gb,
                pg_template_id=tpl,
                pg_group_ids=group_csv,
                pg_username_prefix=uname_prefix,
                pg_username_suffix=uname_suffix,
                pg_username_pattern=uname_pattern,
                button_style=button_style,
                owner_reseller_id=owner_id,
                is_trial=True,
                is_active=enabled,
                description="پلن تست رایگان",
            )
            session.add(trial)
        else:
            trial.name = name
            trial.price = 0
            trial.duration_days = days
            trial.data_limit_gb = gb
            trial.pg_template_id = tpl
            trial.pg_group_ids = group_csv
            trial.pg_username_prefix = uname_prefix
            trial.pg_username_suffix = uname_suffix
            trial.pg_username_pattern = uname_pattern
            trial.button_style = button_style
            trial.is_active = enabled
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={quote('تنظیمات پلن تست ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/custom")
    async def plans_custom_save(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.pg_quota import PgQuotaError, assert_custom_plan_range_within_limits
        from app.services.plans_catalog import (
            groups_allowed_for_staff,
            parse_group_ids_from_form,
            require_catalog_owner_id,
            template_allowed_for_staff,
        )
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        enabled = str(form.get("custom_plan_enabled") or "") in {"1", "on", "true", "yes"}
        try:
            await assert_custom_plan_range_within_limits(
                staff,
                min_gb=float(str(form.get("custom_plan_min_gb") or "1") or "1"),
                max_gb=float(str(form.get("custom_plan_max_gb") or "500") or "500"),
                min_days=max(1, int(float(str(form.get("custom_plan_min_days") or "1") or "1"))),
                max_days=max(1, int(float(str(form.get("custom_plan_max_days") or "365") or "365"))),
                session=session,
            )
        except (PgQuotaError, ValueError) as e:
            return RedirectResponse(
                f"/plans?err={quote(getattr(e, 'message', str(e)))}",
                status_code=303,
            )
        await set_setting(
            session, "custom_plan_enabled", "1" if enabled else "0", reseller_id=owner_id
        )
        for key in (
            "custom_plan_price_per_gb",
            "custom_plan_price_per_day",
            "custom_plan_min_gb",
            "custom_plan_max_gb",
            "custom_plan_min_days",
            "custom_plan_max_days",
        ):
            raw = str(form.get(key) or "").strip()
            if raw:
                await set_setting(session, key, raw, reseller_id=owner_id)
        mode = str(form.get("mode") or "custom")
        if mode == "template":
            tpl = str(form.get("custom_plan_template_id") or "").strip()
            if tpl and tpl.isdigit() and not template_allowed_for_staff(staff, int(tpl)):
                return RedirectResponse(
                    f"/plans?err={quote('به این تمپلیت دسترسی ندارید')}",
                    status_code=303,
                )
            await set_setting(session, "custom_plan_template_id", tpl, reseller_id=owner_id)
            await set_setting(session, "custom_plan_group_ids", "", reseller_id=owner_id)
        else:
            ids = parse_group_ids_from_form(form)
            if ids and not groups_allowed_for_staff(staff, ids):
                return RedirectResponse(
                    f"/plans?err={quote('به یکی از گروه‌های انتخاب‌شده دسترسی ندارید')}",
                    status_code=303,
                )
            await set_setting(
                session,
                "custom_plan_group_ids",
                ",".join(str(i) for i in ids),
                reseller_id=owner_id,
            )
            await set_setting(session, "custom_plan_template_id", "", reseller_id=owner_id)
        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_custom_plan_button_style_form

        c_prefix, c_suffix, c_pattern = parse_naming_form(
            form,
            prefix_key="custom_plan_username_prefix",
            suffix_key="custom_plan_username_suffix",
            pattern_key="custom_plan_username_pattern",
        )
        custom_btn_style = parse_custom_plan_button_style_form(form)
        await set_setting(
            session,
            "custom_plan_button_style",
            "" if custom_btn_style is None else custom_btn_style,
            reseller_id=owner_id,
        )
        await set_setting(
            session, "custom_plan_username_prefix", c_prefix or "", reseller_id=owner_id
        )
        await set_setting(
            session, "custom_plan_username_suffix", c_suffix or "", reseller_id=owner_id
        )
        await set_setting(
            session, "custom_plan_username_pattern", c_pattern or "", reseller_id=owner_id
        )
        return RedirectResponse(
            f"/plans?ok={quote('تنظیمات پلن دلخواه ذخیره شد')}",
            status_code=303,
        )

    @app.post("/plans/wholesale")
    async def plans_wholesale_save(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.orders import parse_wholesale_tiers
        from app.services.plans_catalog import require_catalog_owner_id
        from app.services.shop_scope import ShopScopeError

        form = await request.form()
        try:
            owner_id = require_catalog_owner_id(staff)
        except ShopScopeError as e:
            return RedirectResponse(f"/plans?err={quote(e.message)}", status_code=303)
        enabled = str(form.get("wholesale_enabled") or "") in {"1", "on", "true", "yes"}
        await set_setting(
            session, "wholesale_enabled", "1" if enabled else "0", reseller_id=owner_id
        )
        try:
            mn = max(1, int(float(str(form.get("wholesale_min_qty") or "5"))))
        except (TypeError, ValueError):
            mn = 5
        try:
            mx = max(mn, int(float(str(form.get("wholesale_max_qty") or "20"))))
        except (TypeError, ValueError):
            mx = max(mn, 20)
        await set_setting(session, "wholesale_min_qty", str(mn), reseller_id=owner_id)
        await set_setting(session, "wholesale_max_qty", str(mx), reseller_id=owner_id)
        btn = str(form.get("btn_wholesale") or "").strip() or "📦 فروش عمده"
        await set_setting(session, "btn_wholesale", btn, reseller_id=owner_id)
        tiers = parse_wholesale_tiers(str(form.get("wholesale_tiers") or ""))
        import json

        await set_setting(
            session,
            "wholesale_tiers",
            json.dumps(tiers, ensure_ascii=False),
            reseller_id=owner_id,
        )
        return RedirectResponse(
            f"/plans?ok={quote('تنظیمات فروش عمده ذخیره شد')}",
            status_code=303,
        )

    async def _plans_context(session: AsyncSession, request: Request, staff: dict, extra: dict | None = None):
        from app.services.plans_catalog import list_catalog_plans, load_pg_plan_options
        from app.services.pg_quota import limit_snapshot_cards, load_staff_limit_snapshot

        plans = await list_catalog_plans(session, staff, include_trial=True)
        templates, groups, pg_error = await load_pg_plan_options(staff, session=session)
        snap = await load_staff_limit_snapshot(staff, session=session)
        ctx = {
            "staff": staff,
            "plans": plans,
            "templates": templates,
            "groups": groups,
            "pg_error": pg_error,
            "pg_limit_snapshot": snap,
            "pg_limit_cards": limit_snapshot_cards(snap),
            "flash_err": request.query_params.get("err"),
            "flash_ok": request.query_params.get("ok"),
        }
        if extra:
            ctx.update(extra)
        return ctx

    @app.get("/plans/{plan_id}/edit", response_class=HTMLResponse)
    async def plans_edit_page(
        plan_id: int,
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import catalog_owner_id, get_owned_plan
        from app.services.shop_scope import is_platform_admin

        plan = await get_owned_plan(session, plan_id, staff)
        if not plan:
            return RedirectResponse("/plans?err=" + quote("پلن یافت نشد"), status_code=303)
        rid = catalog_owner_id(staff)
        if not is_platform_admin(staff) and not rid:
            values = {}
        else:
            values = await get_all_settings(session, reseller_id=rid)
        ctx = await _plans_context(session, request, staff, {"plan": plan, "values": values})
        from app.services.button_styles import PLAN_BUTTON_STYLE_OPTIONS

        ctx["plan_style_options"] = PLAN_BUTTON_STYLE_OPTIONS
        return render(request, "plan_edit.html", ctx)

    @app.post("/plans/{plan_id}/edit")
    async def plans_edit_save(
        plan_id: int,
        request: Request,
        name: str = Form(...),
        price: int = Form(...),
        duration_days: int = Form(30),
        data_limit_gb: str = Form(""),
        pg_template_id: str = Form(""),
        description: str = Form(""),
        mode: str = Form("custom"),
        sort_order: int = Form(0),
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
        from app.services.plans_catalog import (
            get_owned_plan,
            groups_allowed_for_staff,
            parse_group_ids_from_form,
            template_allowed_for_staff,
        )

        plan = await get_owned_plan(session, plan_id, staff)
        if not plan:
            return RedirectResponse("/plans?err=" + quote("پلن یافت نشد"), status_code=303)

        form = await request.form()
        gb = float(data_limit_gb) if str(data_limit_gb).strip() else None
        try:
            await assert_user_plan_within_limits(
                staff,
                data_limit=int(gb * (1024**3)) if gb is not None else None,
                duration_days=duration_days,
                label="ویرایش پلن",
                session=session,
            )
        except PgQuotaError as e:
            return RedirectResponse(
                "/plans/%d/edit?err=%s" % (plan_id, quote(e.message)),
                status_code=303,
            )
        tpl = None
        group_csv = None

        if mode == "template":
            tpl = int(pg_template_id) if str(pg_template_id).strip() else None
            if not tpl:
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote('تمپلیت پاسارگارد را انتخاب کنید')}",
                    status_code=303,
                )
            if not template_allowed_for_staff(staff, tpl):
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote('به این تمپلیت دسترسی ندارید')}",
                    status_code=303,
                )
        else:
            ids = parse_group_ids_from_form(form)
            if not ids:
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote('حداقل یک گروه پاسارگارد انتخاب کنید')}",
                    status_code=303,
                )
            if not groups_allowed_for_staff(staff, ids):
                return RedirectResponse(
                    f"/plans/{plan_id}/edit?err={quote('به یکی از گروه‌های انتخاب‌شده دسترسی ندارید')}",
                    status_code=303,
                )
            group_csv = ",".join(str(i) for i in ids)

        plan.name = name.strip()
        plan.price = price
        plan.duration_days = duration_days
        plan.data_limit_gb = gb
        plan.description = description or None
        plan.sort_order = sort_order
        plan.pg_template_id = tpl
        plan.pg_group_ids = group_csv
        from app.services.orders import parse_naming_form
        from app.services.button_styles import parse_plan_button_style_form

        uname_prefix, uname_suffix, uname_pattern = parse_naming_form(form)
        plan.pg_username_prefix = uname_prefix
        plan.pg_username_suffix = uname_suffix
        plan.pg_username_pattern = uname_pattern
        plan.button_style = parse_plan_button_style_form(form)
        await session.commit()
        return RedirectResponse(
            f"/plans?ok={quote('پلن به‌روزرسانی شد')}",
            status_code=303,
        )

    @app.post("/plans/{plan_id}/toggle")
    async def plans_toggle(
        plan_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import get_owned_plan

        plan = await get_owned_plan(session, plan_id, staff)
        if plan:
            plan.is_active = not plan.is_active
            await session.commit()
        return RedirectResponse("/plans", status_code=303)

    @app.post("/plans/{plan_id}/delete")
    async def plans_delete(
        plan_id: int,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.plans_catalog import get_owned_plan

        plan = await get_owned_plan(session, plan_id, staff)
        if plan and not plan.is_trial:
            await session.delete(plan)
            await session.commit()
        return RedirectResponse("/plans", status_code=303)


    async def _notify_order_user(session: AsyncSession, payment: Payment, order: Order | None) -> None:
        try:
            from app.services.payment_settlement import notify_after_settlement

            await notify_after_settlement(session, payment, order)
        except Exception:
            pass

    @app.post("/orders/{order_id}/approve")
    async def order_approve(
        order_id: int,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        order = await session.get(Order, order_id)
        if not order:
            return _redirect_msg("/finance?tab=orders", err="سفارش یافت نشد")
        # Hard shop isolation (match bot ordrev + web payments): platform admin
        # must never approve tenant shop orders.
        if order.reseller_id and staff.get("role") == "admin":
            return _redirect_msg(
                "/finance?tab=orders",
                err="این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل تأیید است",
            )
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, assert_order_in_scope

            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError as e:
                return _redirect_msg("/finance?tab=orders", err=e.message)
        if order.status == OrderStatus.DELIVERED.value:
            return _redirect_msg("/finance?tab=orders", ok="قبلاً تحویل شده")
        # Already provisioned (status may have been tampered) — never re-deliver/notify
        note = (order.note or "").strip()
        is_renew_or_app = note.startswith("renew:") or note.startswith("reseller_app:")
        if order.service_id and not is_renew_or_app:
            if order.status != OrderStatus.DELIVERED.value:
                order.status = OrderStatus.DELIVERED.value
                await session.commit()
            return _redirect_msg("/finance?tab=orders", ok="قبلاً تحویل شده")

        result = await session.execute(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.id.desc())
            .limit(1)
        )
        payment = result.scalar_one_or_none()
        try:
            if payment and payment.status == PaymentStatus.PENDING.value:
                # Approving a pending receipt requires payments perm (not only orders)
                if staff.get("role") != "admin":
                    perms = staff.get("permissions") or []
                    if "payments" not in perms:
                        return _redirect_msg(
                "/finance?tab=orders",
                            err="تأیید رسید نیاز به دسترسی «پرداخت‌ها» دارد",
                        )
                delivered = await approve_payment(session, payment, reviewer_tg=0)
                await _notify_order_user(session, payment, delivered or order)
            elif order.status == OrderStatus.PAID.value:
                had_service = bool(order.service_id)
                delivered = await deliver_order(session, order)
                if payment and not had_service:
                    await _notify_order_user(session, payment, delivered)
            elif payment and payment.status == PaymentStatus.APPROVED.value and order.status != OrderStatus.DELIVERED.value:
                had_service = bool(order.service_id)
                delivered = await deliver_order(session, order)
                if not had_service:
                    await _notify_order_user(session, payment, delivered)
            elif order.status in {
                OrderStatus.PENDING.value,
                OrderStatus.AWAITING_RECEIPT.value,
            }:
                # Pending without receipt: staff can still approve → deliver once
                delivered, payment = await manual_fulfill_unpaid_order(
                    session, order, note="web manual approve"
                )
                await _notify_order_user(session, payment, delivered)
            else:
                return _redirect_msg("/finance?tab=orders", err="این سفارش هنوز قابل تأیید نیست (رسید لازم است)")
        except Exception as e:
            return _redirect_msg("/finance?tab=orders", err=str(e))
        return _redirect_msg("/finance?tab=orders", ok="سفارش تأیید و تحویل شد")

    @app.post("/orders/{order_id}/reject")
    async def order_reject(
        order_id: int,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        order = await session.get(Order, order_id)
        if not order:
            return _redirect_msg("/finance?tab=orders", err="سفارش یافت نشد")
        if order.reseller_id and staff.get("role") == "admin":
            return _redirect_msg(
                "/finance?tab=orders",
                err="این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل رد است",
            )
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, assert_order_in_scope

            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError as e:
                return _redirect_msg("/finance?tab=orders", err=e.message)
        # Rejecting a pending receipt also requires payments perm for limited staff
        pending_pay = (
            await session.execute(
                select(Payment)
                .where(Payment.order_id == order_id, Payment.status == PaymentStatus.PENDING.value)
                .order_by(Payment.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if pending_pay and staff.get("role") != "admin":
            perms = staff.get("permissions") or []
            if "payments" not in perms:
                return _redirect_msg(
                "/finance?tab=orders",
                    err="رد رسید نیاز به دسترسی «پرداخت‌ها» دارد",
                )
        try:
            if pending_pay:
                await reject_payment(session, pending_pay, reviewer_tg=0, note="web order reject")
            else:
                await reject_order(session, order, note="web order reject")
        except Exception as e:
            return _redirect_msg("/finance?tab=orders", err=str(e))
        await notify_payer(
            session,
            order.user_id,
            title="❌ سفارش رد شد",
            body=f"سفارش #{order_id} رد شد. اگر اشتباهی رخ داده با پشتیبانی در تماس باشید.",
        )
        return _redirect_msg("/finance?tab=orders", ok="سفارش رد شد")

    @app.post("/orders/{order_id}/cancel")
    async def order_cancel(
        order_id: int,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        order = await session.get(Order, order_id)
        if not order:
            return _redirect_msg("/finance?tab=orders", err="سفارش یافت نشد")
        if order.reseller_id and staff.get("role") == "admin":
            return _redirect_msg(
                "/finance?tab=orders",
                err="این سفارش مربوط به نماینده است — فقط در ربات/پنل همان فروشگاه قابل لغو است",
            )
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, assert_order_in_scope

            try:
                assert_order_in_scope(staff, order)
            except ShopScopeError as e:
                return _redirect_msg("/finance?tab=orders", err=e.message)
        try:
            await cancel_order(session, order, note="web order cancel")
        except Exception as e:
            return _redirect_msg("/finance?tab=orders", err=str(e))
        await notify_payer(
            session,
            order.user_id,
            title="⏹ سفارش لغو شد",
            body=f"سفارش #{order_id} لغو شد.",
        )
        return _redirect_msg("/finance?tab=orders", ok="سفارش لغو شد")


    @app.get("/payments/{payment_id}/approve")
    async def payment_approve_get(payment_id: int):
        return _redirect_msg("/finance?tab=payments", err="برای تأیید از دکمه داخل صفحه پرداخت‌ها استفاده کنید")

    @app.post("/payments/{payment_id}/approve")
    async def payment_approve(
        payment_id: int,
        staff: dict = Depends(require_perm("payments")),
        session: AsyncSession = Depends(get_db),
    ):
        payment = await session.get(Payment, payment_id)
        if not payment:
            return _redirect_msg("/finance?tab=payments", err="پرداخت یافت نشد")
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, require_shop_owner_id

            # Wallet top-ups mint global balance — tenant reviewers cannot approve them.
            if payment.is_wallet_topup:
                return _redirect_msg("/finance?tab=payments", err="شارژ کیف پول فقط توسط مدیر اصلی تأیید می‌شود")
            try:
                rid = require_shop_owner_id(staff)
            except ShopScopeError as e:
                return _redirect_msg("/finance?tab=payments", err=e.message)
            if not payment.order_id:
                return _redirect_msg("/finance?tab=payments", err="دسترسی به این پرداخت ندارید")
            order_row = await session.get(Order, payment.order_id)
            if not order_row or order_row.reseller_id != rid:
                return _redirect_msg("/finance?tab=payments", err="دسترسی به این پرداخت ندارید")
        elif payment.order_id and not payment.is_wallet_topup:
            # Platform admin must not approve shop-tenant payments (match bot isolation)
            order_row = await session.get(Order, payment.order_id)
            if order_row and order_row.reseller_id is not None:
                return _redirect_msg(
                "/finance?tab=payments",
                    err="پرداخت‌های فروشگاه فقط توسط نماینده همان فروشگاه تأیید می‌شود",
                )
        if payment.status != PaymentStatus.PENDING.value:
            return _redirect_msg("/finance?tab=payments", err="این پرداخت قابل تأیید نیست")
        try:
            order = await approve_payment(session, payment, reviewer_tg=0)
        except Exception as e:
            return _redirect_msg("/finance?tab=payments", err=str(e))
        user = await session.get(BotUser, payment.user_id)
        if user:
            try:
                from app.services.delivery import send_delivery_to_user
                from app.services.notifications import notify_new_subscription, notify_wallet_topup_ok
                from app.services.reseller_bots import open_notify_bot_for_user

                bot, should_close = await open_notify_bot_for_user(session, user)
                try:
                    try:
                        await send_delivery_to_user(
                            bot, user.telegram_id, session, payment, order
                        )
                    except Exception as send_exc:
                        if order is not None:
                            from app.services.ux20 import note_delivery_send_failure

                            await note_delivery_send_failure(
                                session, order=order, payment=payment, error=str(send_exc)
                            )
                        raise
                    if payment.is_wallet_topup:
                        await notify_wallet_topup_ok(bot, session, payment, user.telegram_id)
                    elif order:
                        plan = await session.get(Plan, order.plan_id) if order.plan_id else None
                        await notify_new_subscription(
                            bot,
                            session,
                            order=order,
                            user_tg_id=user.telegram_id,
                            user_name=user.full_name or user.username,
                            plan_name=plan.name if plan else None,
                            needs_approval=False,
                        )
                finally:
                    if should_close:
                        await bot.session.close()
            except Exception:
                pass
        return _redirect_msg("/finance?tab=payments", ok="پرداخت تأیید شد")

    @app.post("/payments/{payment_id}/reject")
    async def payment_reject(
        payment_id: int,
        staff: dict = Depends(require_perm("payments")),
        session: AsyncSession = Depends(get_db),
    ):
        payment = await session.get(Payment, payment_id)
        if not payment:
            return _redirect_msg("/finance?tab=payments", err="پرداخت یافت نشد")
        if staff.get("role") != "admin":
            from app.services.shop_scope import ShopScopeError, require_shop_owner_id

            if payment.is_wallet_topup:
                return _redirect_msg("/finance?tab=payments", err="شارژ کیف پول فقط توسط مدیر اصلی رد می‌شود")
            try:
                rid = require_shop_owner_id(staff)
            except ShopScopeError as e:
                return _redirect_msg("/finance?tab=payments", err=e.message)
            if not payment.order_id:
                return _redirect_msg("/finance?tab=payments", err="دسترسی به این پرداخت ندارید")
            order_row = await session.get(Order, payment.order_id)
            if not order_row or order_row.reseller_id != rid:
                return _redirect_msg("/finance?tab=payments", err="دسترسی به این پرداخت ندارید")
        elif payment.order_id and not payment.is_wallet_topup:
            order_row = await session.get(Order, payment.order_id)
            if order_row and order_row.reseller_id is not None:
                return _redirect_msg(
                "/finance?tab=payments",
                    err="پرداخت‌های فروشگاه فقط توسط نماینده همان فروشگاه رد می‌شود",
                )
        try:
            await reject_payment(session, payment, reviewer_tg=0, note="web reject")
        except Exception as e:
            return _redirect_msg("/finance?tab=payments", err=str(e))
        body = "پرداخت شما رد شد. اگر اشتباهی رخ داده با پشتیبانی در تماس باشید."
        user = await session.get(BotUser, payment.user_id)
        if user:
            try:
                shop_rid = getattr(user, "reseller_id", None)
                ui = await get_all_settings(session, reseller_id=shop_rid)
                body = ui.get("payment_reject_text") or body
            except Exception:
                pass
        await notify_payer(
            session,
            payment.user_id,
            title="❌ پرداخت رد شد",
            body=body,
        )
        return _redirect_msg("/finance?tab=payments", ok="پرداخت رد شد")

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(
        request: Request,
        staff: dict = Depends(require_perm("dashboard")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.shop_scope import ShopScopeError, resolve_shop_scope_id
        from app.services.users_ops import (
            build_users_ops_page,
            parse_focus_uid,
            scoped_users_where,
            users_list_href,
        )

        try:
            scope = resolve_shop_scope_id(staff)
        except ShopScopeError:
            return RedirectResponse("/home", status_code=303)

        search_q = normalize_search_q(request.query_params.get("q"))
        filter_raw = request.query_params.get("filter")
        focus_uid = parse_focus_uid(request.query_params.get("uid"))
        fetch_limit = 500 if search_q else 200

        # Shop scope: Owner → platform users only; reseller → own customers.
        # Pure resellers (no shop services) stay on /resellers.
        result = await session.execute(
            select(BotUser)
            .where(scoped_users_where(scope))
            .order_by(BotUser.id.desc())
            .limit(fetch_limit)
        )
        users = list(result.scalars().all())
        if search_q:
            users = filter_by_search(
                users,
                search_q,
                lambda u: (
                    u.id,
                    u.telegram_id,
                    u.username,
                    u.full_name,
                    u.role,
                    u.wallet_balance,
                    "مسدود" if u.is_blocked else "فعال",
                ),
            )
        rows, counts, filter_key = await build_users_ops_page(
            session,
            users,
            filter_key=filter_raw,
            focus_uid=focus_uid,
        )
        # Deep-link uid outside this shop → ignore (no cross-tenant leak).
        if focus_uid and not any(int(r.user.id) == int(focus_uid) for r in rows):
            in_fetched = any(int(u.id) == int(focus_uid) for u in users)
            if not in_fetched:
                focus_uid = None
        return render(
            request,
            "users.html",
            {
                "staff": staff,
                "rows": rows,
                "users": [r.user for r in rows],
                "counts": counts,
                "filter": filter_key,
                "focus_uid": focus_uid,
                "filter_href": users_list_href,
                "q": search_q,
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
                "open_edit": request.query_params.get("edit"),
                "can_manage_users": is_explicit_owner_staff(staff),
                "can_user_ops": True,
            },
        )

    @app.post("/users/{user_id}/role")
    async def users_set_role(
        request: Request,
        user_id: int,
        role: str = Form(...),
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        user = await session.get(BotUser, user_id)
        if not user:
            return _redirect_msg("/users", err="کاربر یافت نشد")
        from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return _redirect_msg("/users", err=e.message)
        if role not in {Role.USER.value, Role.RESELLER.value, Role.ADMIN.value}:
            return _redirect_msg("/users", err="نقش نامعتبر")

        from app.services.notifications import actor_label_from_staff, notify_account_edit

        old_role = user.role
        actor = actor_label_from_staff(staff)

        if role == Role.RESELLER.value:
            from app.services.resellers import (
                format_credentials_message,
                get_reseller_panel_base_url,
                provision_reseller,
            )

            try:
                creds = await provision_reseller(
                    session,
                    user=user,
                    web_permissions=DEFAULT_FEATURE_PERMS,
                    bot_permissions=DEFAULT_FEATURE_PERMS,
                    create_pg_admin=True,
                    panel_base_url=await get_reseller_panel_base_url(session),
                )
            except Exception as e:
                return _redirect_msg("/users", err=str(e))
            try:
                from app.bot import create_bot

                bot = create_bot()
                try:
                    await bot.send_message(
                        user.telegram_id,
                        format_credentials_message(creds),
                        parse_mode="HTML",
                    )
                finally:
                    await bot.session.close()
            except Exception:
                pass
            # Admin mirror + short role notice (credentials already sent to subject)
            await notify_account_edit(
                session,
                user=user,
                event="role",
                reason=reason,
                old_role=old_role,
                new_role=role,
                actor=actor,
                notify_subject=False,
            )
        else:
            # Demoting a reseller must tear down profile / PG admin / shop bot
            if user.role == Role.RESELLER.value and role != Role.RESELLER.value:
                from app.services.resellers import notify_reseller_revoked, revoke_reseller

                try:
                    info = await revoke_reseller(
                        session,
                        user.id,
                        delete_pg_admin=True,
                        reason=reason,
                    )
                except ValueError:
                    user.role = role
                    await session.commit()
                    info = {"telegram_id": user.telegram_id}
                if role == Role.ADMIN.value:
                    user = await session.get(BotUser, user_id)
                    if user and user.role != Role.ADMIN.value:
                        user.role = Role.ADMIN.value
                        await session.commit()
                user = await session.get(BotUser, user_id)
                if user and role == Role.USER.value:
                    await notify_reseller_revoked(
                        int(info.get("telegram_id") or user.telegram_id),
                        reason,
                        session=session,
                        user=user,
                        actor=actor,
                    )
                elif user:
                    await notify_account_edit(
                        session,
                        user=user,
                        event="role",
                        reason=reason,
                        old_role=old_role,
                        new_role=role,
                        actor=actor,
                    )
            else:
                user.role = role
                await session.commit()
                await notify_account_edit(
                    session,
                    user=user,
                    event="role",
                    reason=reason,
                    old_role=old_role,
                    new_role=role,
                    actor=actor,
                )
        return RedirectResponse(
            f"/users?edit={int(user_id)}&ok={quote('نقش به‌روز شد')}&_={int(time.time())}",
            status_code=303,
        )

    @app.post("/users/{user_id}/block")
    async def users_toggle_block(
        request: Request,
        user_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import actor_label_from_staff, notify_account_edit
        from app.services.users import is_protected_admin

        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        user = await session.get(BotUser, user_id)
        if not user:
            return _redirect_msg("/users", err="کاربر یافت نشد")
        from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return _redirect_msg("/users", err=e.message)
        if is_protected_admin(user):
            return _redirect_msg("/users", err="مسدود کردن ادمین مجاز نیست")
        will_block = not user.is_blocked
        user.is_blocked = will_block
        await session.commit()
        await notify_account_edit(
            session,
            user=user,
            event="block" if will_block else "unblock",
            reason=reason or None,
            actor=actor_label_from_staff(staff),
        )
        return _redirect_msg("/users", ok="وضعیت مسدودی تغییر کرد")

    @app.post("/users/{user_id}/delete")
    async def users_delete(
        request: Request,
        user_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import actor_label_from_staff, notify_account_edit
        from app.services.users import delete_bot_user, friendly_user_delete_error

        form = await request.form()
        reason = str(form.get("reason") or "").strip()
        if len(reason) < 3:
            return _redirect_msg("/users", err="علت حذف کاربر الزامی است (حداقل ۳ کاراکتر)")

        actor_id = None
        try:
            actor_id = int(staff.get("user_id") or 0) or None
        except (TypeError, ValueError):
            actor_id = None
        # Prefer matching by telegram if panel session stores it
        if not actor_id and staff.get("telegram_id"):
            try:
                tg = int(staff["telegram_id"])
                actor = (
                    await session.execute(select(BotUser).where(BotUser.telegram_id == tg))
                ).scalar_one_or_none()
                actor_id = actor.id if actor else None
            except Exception:
                actor_id = None

        user = await session.get(BotUser, user_id)
        if not user:
            return _redirect_msg("/users", err="کاربر یافت نشد")
        from app.services.shop_scope import ShopScopeError, assert_bot_user_in_scope

        try:
            assert_bot_user_in_scope(staff, user)
        except ShopScopeError as e:
            return _redirect_msg("/users", err=e.message)
        await notify_account_edit(
            session,
            user=user,
            event="user_delete",
            reason=reason,
            actor=actor_label_from_staff(staff),
        )
        try:
            info = await delete_bot_user(
                session,
                user_id,
                actor_user_id=actor_id,
            )
        except ValueError as e:
            return _redirect_msg("/users", err=str(e))
        except Exception as e:
            return _redirect_msg("/users", err=friendly_user_delete_error(e))
        label = info.get("name") or info.get("telegram_id")
        return _redirect_msg("/users", ok=f"کاربر {label} حذف شد")

    @app.get("/menu-layout", response_class=HTMLResponse)
    async def menu_layout_page(staff: dict = Depends(require_admin)):
        return RedirectResponse("/settings?tab=menu", status_code=303)

    @app.post("/menu-layout")
    async def menu_layout_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        form = await request.form()
        await _save_menu_layout(session, form)
        return RedirectResponse("/settings?tab=menu&saved=1", status_code=303)

    @app.get("/update", response_class=HTMLResponse)
    async def update_page(
        request: Request,
        staff: dict = Depends(require_admin),
    ):
        q = "tab=update"
        if request.query_params.get("force") == "1":
            q += "&force=1"
        return RedirectResponse(f"/settings?{q}", status_code=303)

    @app.get("/update/status")
    async def update_status(staff: dict = Depends(require_admin)):
        from app.runtime import BOOT_ID, PID
        from app.services.panel_update import resolve_stale_update_status
        from app.services.updates import local_version

        st = resolve_stale_update_status()
        st["current_version"] = local_version()
        st["boot_id"] = BOOT_ID
        st["pid"] = PID
        return st

    @app.post("/update/clear")
    async def update_clear(staff: dict = Depends(require_admin)):
        """Admin escape hatch: unlock stuck update UI."""
        from app.services.panel_update import clear_idle_status, resolve_stale_update_status

        st = resolve_stale_update_status()
        if st.get("state") == "running":
            # Still try to clear zombies
            from app.services.panel_update import _thread_alive

            if _thread_alive():
                return {"ok": False, "error": "عملیات در حال اجراست", "status": st}
        return {"ok": True, "status": clear_idle_status()}

    @app.post("/update/start")
    async def update_start(
        request: Request,
        staff: dict = Depends(require_admin),
    ):
        from app.services.panel_update import clear_idle_status, start_update
        from app.services.updates import check_github_update, clear_update_cache, is_newer

        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        clear_update_cache()
        info = await check_github_update(force=True)
        target = (body or {}).get("target") or info.get("remote_version")
        force = bool((body or {}).get("force"))
        # Retry after error may force; otherwise require a real newer remote.
        if not info.get("update_available") and not force:
            from app.services.panel_update import read_status

            st = read_status()
            if st.get("state") != "error" and not st.get("restart_required"):
                return {"ok": False, "error": "نسخه جدیدی برای آپدیت نیست", "info": info}
        if target and info.get("remote_version") and not force:
            # Prefer the freshly checked remote over a stale client target.
            if is_newer(info.get("remote_version"), target):
                target = info.get("remote_version")
        # Clear leftover awaiting before starting a forced retry.
        if force:
            clear_idle_status()
        return start_update(target_version=target)

    @app.post("/update/rollback")
    async def update_rollback(
        request: Request,
        staff: dict = Depends(require_admin),
    ):
        from app.services.panel_update import start_rollback, start_rollback_to_version
        from app.services.updates import fetch_recent_versions

        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        version = str((body or {}).get("version") or "").strip()
        if version:
            recent = await fetch_recent_versions(limit=3, force=True)
            allowed = [str(x.get("version") or "") for x in recent]
            return start_rollback_to_version(version, allowed=allowed)
        snapshot_id = str((body or {}).get("snapshot_id") or "").strip()
        if not snapshot_id:
            return {"ok": False, "error": "نسخه بازگشت مشخص نشده"}
        return start_rollback(snapshot_id)

    @app.get("/notifications", response_class=HTMLResponse)
    async def notifications_page(staff: dict = Depends(require_admin)):
        return RedirectResponse("/settings?tab=notifications", status_code=303)

    @app.post("/notifications")
    async def notifications_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import save_notify_prefs

        form = await request.form()
        await save_notify_prefs(session, dict(form))
        return RedirectResponse("/settings?tab=notifications&saved=1", status_code=303)

    @app.post("/supports/save")
    async def supports_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from urllib.parse import quote

        from app.services.support_contacts import contacts_from_form_json, save_support_contacts

        form = await request.form()
        raw = form.get("support_contacts")
        if raw is not None:
            contacts, err = contacts_from_form_json(str(raw))
            if err:
                return RedirectResponse(
                    f"/tickets?supports=1&err={quote(err)}",
                    status_code=303,
                )
            await save_support_contacts(session, contacts or [])
            return RedirectResponse("/tickets?supports=1&saved=1", status_code=303)

        # Legacy single-row upsert (bot / older clients)
        from app.services.button_styles import parse_item_button_style_form
        from app.services.support_contacts import upsert_support_contact

        contact_id = str(form.get("id") or "").strip() or None
        title = str(form.get("title") or "").strip()
        telegram = str(form.get("telegram") or "").strip()
        try:
            sort = int(str(form.get("sort") or "0").strip() or "0")
        except ValueError:
            sort = 0
        enabled = str(form.get("enabled") or "") in {"1", "on", "true", "yes"}
        if contact_id and "enabled" not in form:
            enabled = False
        button_style = parse_item_button_style_form(form)
        _, err = await upsert_support_contact(
            session,
            contact_id=contact_id,
            title=title,
            telegram=telegram,
            sort=sort,
            enabled=enabled,
            button_style=button_style,
        )
        if err:
            return RedirectResponse(
                f"/tickets?supports=1&err={quote(err)}",
                status_code=303,
            )
        return RedirectResponse("/tickets?supports=1&saved=1", status_code=303)

    @app.post("/supports/delete")
    async def supports_delete(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.support_contacts import delete_support_contact

        form = await request.form()
        contact_id = str(form.get("id") or "").strip()
        if contact_id:
            await delete_support_contact(session, contact_id)
        return RedirectResponse("/tickets?supports=1&saved=1", status_code=303)

    def _menu_tab_context(values: dict) -> dict:
        from app.bot.keyboards import DEFAULT_MENU_ORDER

        order_raw = values.get("menu_order") or ",".join(DEFAULT_MENU_ORDER)
        order = [p.strip() for p in order_raw.split(",") if p.strip()]
        order = [k for k in order if k in DEFAULT_MENU_ORDER]
        if "shop" not in order:
            order.insert(0, "shop")
        catalog_meta = {
            "shop": {"label": "خرید سرویس", "required": True},
            "services": {"label": "سرویس‌های من", "required": False},
            "wallet": {"label": "کیف پول", "required": False},
            "support": {"label": "پشتیبانی", "required": False},
            "loyalty": {"label": "باشگاه مشتریان", "required": False},
            "reseller_apply": {"label": "درخواست نمایندگی", "required": False},
            "miniapp": {"label": "مینی‌اپ", "required": False},
        }
        # Migrate legacy referral → loyalty for DnD UI
        if "referral" in order and "loyalty" not in order:
            order = ["loyalty" if k == "referral" else k for k in order]
        order = [k for k in order if k != "referral"]
        items = []
        for key in order:
            meta = catalog_meta.get(key)
            if not meta:
                continue
            items.append(
                {
                    "key": key,
                    "label": meta["label"],
                    "btn": values.get(f"btn_{key}", meta["label"]),
                    "required": meta["required"],
                }
            )
        pool = []
        for key in DEFAULT_MENU_ORDER:
            if key not in order and key != "shop":
                meta = catalog_meta.get(key)
                if not meta:
                    continue
                pool.append(
                    {
                        "key": key,
                        "label": meta["label"],
                        "btn": values.get(f"btn_{key}", meta["label"]),
                        "required": False,
                    }
                )
        return {"items": items, "pool": pool, "order_csv": ",".join(order)}

    async def _save_menu_layout(session: AsyncSession, form, *, reseller_id: int | None = None) -> None:
        from app.bot.keyboards import DEFAULT_MENU_ORDER, sync_show_flags_for_order

        order = [p.strip() for p in str(form.get("menu_order") or "").split(",") if p.strip()]
        order = [k for k in order if k in DEFAULT_MENU_ORDER]
        if reseller_id is not None:
            order = [k for k in order if k != "reseller_apply"]
        if "shop" not in order:
            order.insert(0, "shop")
        layout = str(form.get("menu_layout") or "compact").strip()
        payload = {"menu_order": ",".join(order)}
        if layout in {"classic", "compact"}:
            payload["menu_layout"] = layout
        payload.update(sync_show_flags_for_order(order))
        if reseller_id is not None:
            payload["show_reseller_apply"] = "0"
        await set_settings_bulk(session, payload, reseller_id=reseller_id)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.notifications import NOTIFY_PREFS, get_notify_prefs
        from app.services.updates import clear_update_cache, local_version

        tab = (request.query_params.get("tab") or "welcome").strip()
        valid = {t[0] for t in SETTINGS_TABS} | PANEL_SETTINGS_KEYS
        if tab in SETTINGS_DOMAIN_REDIRECTS:
            dest = SETTINGS_DOMAIN_REDIRECTS[tab]
            from urllib.parse import urlencode
            extra = {}
            for k in ("ok", "err", "saved", "msg"):
                if request.query_params.get(k):
                    extra[k] = request.query_params.get(k)
            if extra:
                sep = "&" if "?" in dest else "?"
                dest = dest + sep + urlencode(extra)
            return RedirectResponse(dest, status_code=303)
        if tab == "users":
            return RedirectResponse("/settings?tab=naming", status_code=303)
        if tab == "security":
            return RedirectResponse("/security", status_code=303)
        if tab not in valid:
            tab = "welcome"

        values = await get_all_settings(session)
        tab_groups = TAB_SETTING_GROUPS.get(tab, [])
        groups = {name: SETTING_GROUPS[name] for name in tab_groups if name in SETTING_GROUPS}
        if tab == "terms":
            from app.services.rich_text import prepare_settings_values_for_web

            values = prepare_settings_values_for_web(values)

        if tab == "colors":
            from app.services.payment_destinations import enrich_payment_settings

            values = enrich_payment_settings(values)

        # Bot settings tabs only in horizontal nav; panel tabs use sidebar.
        page_tabs = SETTINGS_TABS if tab not in PANEL_SETTINGS_KEYS else []

        ctx: dict = {
            "staff": staff,
            "values": values,
            "groups": groups,
            "tab_groups": tab_groups,
            "tabs": page_tabs,
            "tab": tab,
            "is_panel_settings": tab in PANEL_SETTINGS_KEYS,
            "panel_tab_label": dict(PANEL_SETTINGS_TABS).get(tab),
            "saved": request.query_params.get("saved") == "1",
            "saved_msg": request.query_params.get("msg") or "",
        }
        # Single flash above page title (base.html) — avoid below-title duplicates
        if request.query_params.get("saved") == "1":
            ctx["flash_ok"] = request.query_params.get("msg") or "ذخیره شد."
        elif request.query_params.get("ok"):
            ctx["flash_ok"] = request.query_params.get("ok")
        if request.query_params.get("err"):
            ctx["flash_err"] = request.query_params.get("err")

        if tab == "menu":
            ctx.update(_menu_tab_context(values))
        elif tab == "terms":
            from app.services.button_styles import STYLE_OPTIONS

            ctx["style_options"] = STYLE_OPTIONS
        elif tab == "colors":
            from app.services.button_styles import (
                STYLE_OPTIONS,
                build_dynamic_color_summary,
                colors_page_grouped_sections,
            )
            from app.services.payment_destinations import (
                enrich_payment_settings,
                parse_payment_cards,
                parse_payment_crypto_wallets,
                parse_payment_gateways,
            )
            from app.services.support_contacts import parse_support_contacts

            pay_ui = enrich_payment_settings(values)
            ctx["colors_page_sections"] = colors_page_grouped_sections()
            ctx["style_options"] = STYLE_OPTIONS
            ctx["dynamic_color_rows"] = build_dynamic_color_summary(
                values,
                support_contacts=parse_support_contacts(values.get("support_contacts")),
                payment_cards=parse_payment_cards(pay_ui.get("payment_cards")),
                payment_gateways=parse_payment_gateways(pay_ui.get("payment_gateways")),
                payment_crypto_wallets=parse_payment_crypto_wallets(
                    pay_ui.get("payment_crypto_wallets")
                ),
            )
        elif tab == "notifications":
            prefs = await get_notify_prefs(session)
            ctx["notify_prefs"] = prefs
            ctx["notify_items"] = NOTIFY_PREFS
        elif tab == "daily_report":
            from app.services.daily_report import (
                ACTOR_OWNER,
                DEFAULT_REPORT_TEMPLATE,
                metric_groups_for_ui,
                parse_metric_keys,
            )

            actor = ACTOR_OWNER
            ctx["daily_report_actor"] = actor
            ctx["daily_report_metric_groups"] = metric_groups_for_ui(actor)
            ctx["daily_report_enabled_metrics"] = parse_metric_keys(
                values.get("admin_daily_report_metrics"), actor=actor
            )
            if not (values.get("admin_daily_report_template") or "").strip():
                values["admin_daily_report_template"] = DEFAULT_REPORT_TEMPLATE
        elif tab == "update":
            from app.services.panel_update import clear_idle_status, update_page_context
            from app.services.updates import clear_update_cache

            # Always re-check GitHub on the update tab (CDN + in-process cache
            # otherwise hide a just-published release for several minutes).
            clear_update_cache()
            # Success flash after confirmed restart — wipe progress UI
            if request.query_params.get("ok"):
                clear_idle_status()
            ctx.update(await update_page_context(force_check=True))
        elif tab == "pwa":
            from app.services.pwa import load_pwa_settings

            ctx["pwa"] = await load_pwa_settings(session)
        elif tab == "backup":
            from app.services.backup import (
                clear_idle_restore_status,
                list_backups,
                resolve_stale_restore_status,
                sqlite_db_path,
            )

            if request.query_params.get("ok"):
                clear_idle_restore_status()
            ctx["backups"] = list_backups()
            ctx["restore_status"] = resolve_stale_restore_status()
            ctx["local_version"] = local_version()
            ctx["db_path"] = str(sqlite_db_path())
            # values already loaded above — do not re-import get_all_settings
            # (that would shadow the module-level name and 500 every settings tab).
            names = TAB_SETTING_GROUPS.get("backup") or []
            ctx["backup_schedule_groups"] = {
                name: SETTING_GROUPS[name] for name in names if name in SETTING_GROUPS
            }
        elif tab == "bot":
            from app.services.setup_wizard import current_setup_values

            env_values = current_setup_values()
            ctx["env_values"] = env_values
            token = env_values.get("BOT_TOKEN") or ""
            ctx["bot_status"] = await _bot_token_status(token)
            ctx["bot_token_masked"] = (
                ("••••" + token[-6:]) if len(token) > 8 else ("••••" if token else "")
            )
            wh_base = env_values.get("WEBHOOK_URL") or ""
            wh_path = env_values.get("WEBHOOK_PATH") or "/telegram/webhook"
            ctx["bot_update_mode"] = env_values.get("BOT_UPDATE_MODE") or (
                "webhook" if wh_base else "polling"
            )
            ctx["webhook_full_url"] = (wh_base + wh_path) if wh_base else ""
        elif tab == "links":
            from app.services.ux20 import build_magic_links_context

            ctx.update(await build_magic_links_context(session, staff))
        elif tab == "ssl":
            from urllib.parse import urlparse

            from app.services.setup_wizard import current_setup_values
            from app.services.ssl_certs import cert_status, normalize_domain

            st = cert_status()
            if not st.get("domain"):
                pub = (current_setup_values().get("PUBLIC_BASE_URL") or "").strip()
                if pub:
                    host = urlparse(pub if "://" in pub else f"https://{pub}").hostname or ""
                    st["domain"] = normalize_domain(host)
            ctx["ssl"] = st
        elif tab == "appearance":
            from app.services.bot_appearance import load_appearance_context
            from app.services.setup_wizard import current_setup_values

            env_values = current_setup_values()
            token = (env_values.get("BOT_TOKEN") or "").strip()
            uname = (env_values.get("BOT_USERNAME") or "").strip()
            ctx.update(
                await load_appearance_context(
                    session, token=token, fallback_username=uname
                )
            )

        return render(request, "settings.html", ctx)

    @app.post("/settings/cancel-pending-orders")
    async def settings_cancel_pending_orders(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        """Manual cancel of stale pending/unapproved orders (platform shop)."""
        from app.services.orders import cancel_stale_pending_for_settings

        try:
            n = await cancel_stale_pending_for_settings(
                session, reseller_id=None, force=True
            )
        except Exception as e:
            dest = _settings_next(request, "/finance?tab=orders&settings=payment")
            return _redirect_msg(dest, err=f"لغو سفارش‌ها ناموفق: {e}")
        dest = _settings_next(request, "/finance?tab=orders&settings=payment")
        return _redirect_msg(
            dest,
            ok=f"{n} سفارش معلق/تأییدنشده لغو شد" if n else "سفارش معلقی برای لغو نبود",
        )

    async def _bot_token_status(token: str) -> dict:
        token = (token or "").strip()
        if not token:
            return {"ok": False, "error": "توکن تنظیم نشده"}
        try:
            import httpx

            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(f"https://api.telegram.org/bot{token}/getMe")
                data = resp.json()
            if data.get("ok") and isinstance(data.get("result"), dict):
                me = data["result"]
                return {
                    "ok": True,
                    "username": me.get("username"),
                    "id": me.get("id"),
                    "name": me.get("first_name"),
                }
            return {"ok": False, "error": data.get("description") or "توکن نامعتبر"}
        except Exception as exc:
            return {"ok": False, "error": f"عدم اتصال به تلگرام: {exc}"}

    @app.post("/settings")
    async def settings_save(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        import uuid

        from starlette.datastructures import UploadFile

        from app.services.users import IMAGE_KEYS, SECRET_KEYS, TOGGLE_KEYS, keys_for_tab, should_keep_secret_value

        tab = (request.query_params.get("tab") or "welcome").strip()
        form = await request.form()

        if tab == "ssl":
            from app.services.ssl_certs import (
                disable_https,
                enable_https,
                is_valid_domain,
                normalize_domain,
                read_meta,
                start_issue_job,
                write_meta,
            )

            action = str(form.get("action") or "issue").strip()
            domain = normalize_domain(str(form.get("domain") or ""))
            email = str(form.get("acme_email") or "").strip()

            if action == "enable":
                result = enable_https(restart=True)
                if not result.get("ok"):
                    return RedirectResponse(
                        "/settings?tab=ssl&err=" + quote(str(result.get("error") or "خطا")[:400]),
                        status_code=303,
                    )
                return RedirectResponse("/settings?tab=ssl", status_code=303)

            if action == "disable":
                result = disable_https(restart=True)
                if not result.get("ok"):
                    return RedirectResponse(
                        "/settings?tab=ssl&err=" + quote(str(result.get("error") or "خطا")[:400]),
                        status_code=303,
                    )
                return RedirectResponse(
                    "/settings?tab=ssl&ok=" + quote("HTTPS غیرفعال شد — ری‌استارت…"),
                    status_code=303,
                )

            if not is_valid_domain(domain):
                return RedirectResponse(
                    "/settings?tab=ssl&err=" + quote("دامنه نامعتبر است"),
                    status_code=303,
                )
            if not email or "@" not in email:
                return RedirectResponse(
                    "/settings?tab=ssl&err=" + quote("ایمیل معتبر لازم است"),
                    status_code=303,
                )
            meta = read_meta()
            meta.update({"domain": domain, "panel_domain": domain, "miniapp_domain": domain, "email": email})
            write_meta(meta)
            force = action == "renew"
            result = start_issue_job(domain=domain, email=email, force=force)
            if not result.get("ok"):
                return RedirectResponse(
                    "/settings?tab=ssl&err=" + quote(str(result.get("error") or "خطا")[:400]),
                    status_code=303,
                )
            return RedirectResponse("/settings?tab=ssl&working=1", status_code=303)

        if tab == "bot":
            from fastapi.responses import JSONResponse

            from app.runtime import BOOT_ID
            from app.services.pasarguard import reset_pg
            from app.services.service_control import schedule_panel_restart
            from app.services.setup_wizard import current_setup_values, parse_admin_ids

            ajax = str(form.get("ajax") or "").strip() == "1"
            preview = str(form.get("preview") or "").strip() == "1"
            confirmed = str(form.get("confirm_restart") or "").strip() == "1"

            def _bot_err(msg: str, *, code: int = 400):
                if ajax or preview:
                    return JSONResponse(
                        {"ok": False, "error": msg, "needs_confirm": False},
                        status_code=code,
                    )
                return RedirectResponse(
                    "/settings?tab=bot&err=" + quote(msg),
                    status_code=303,
                )

            token = str(form.get("BOT_TOKEN") or "").strip()
            uname = str(form.get("BOT_USERNAME") or "").strip().lstrip("@")
            ids_raw = str(form.get("ADMIN_IDS") or "").strip()
            pg_base = normalize_pg_base_url(str(form.get("PG_BASE_URL") or "").strip())
            pg_user = str(form.get("PG_USERNAME") or "").strip()
            pg_pass = str(form.get("PG_PASSWORD") or "").strip()
            web_port = str(form.get("WEB_PORT") or "9000").strip()
            public_base = str(form.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
            try:
                from app.services.ssl_certs import https_is_active

                if public_base.startswith("https://") and not https_is_active():
                    public_base = "http://" + public_base[len("https://") :]
            except Exception:
                pass
            currency = str(form.get("CURRENCY") or "").strip() or "تومان"
            update_mode_raw = str(form.get("BOT_UPDATE_MODE") or "").strip().lower()
            webhook_url_raw = str(form.get("WEBHOOK_URL") or "").strip()
            webhook_path_raw = str(form.get("WEBHOOK_PATH") or "").strip()
            current = current_setup_values()
            if not token:
                token = (current.get("BOT_TOKEN") or "").strip()
            if not pg_pass:
                pg_pass = (current.get("PG_PASSWORD") or "").strip()
            if not token or not uname or not ids_raw or not pg_base or not pg_user or not pg_pass:
                return _bot_err("همه فیلدهای الزامی را پر کنید")
            try:
                ids = parse_admin_ids(ids_raw)
            except ValueError:
                return _bot_err("آیدی ادمین‌ها نامعتبر است")
            if not ids:
                return _bot_err("حداقل یک آیدی ادمین لازم است")
            try:
                port_n = int(web_port)
                if port_n < 1 or port_n > 65535:
                    raise ValueError
            except ValueError:
                return _bot_err("پورت نامعتبر است")
            try:
                update_mode, webhook_base, webhook_path = resolve_bot_update_mode(
                    mode=update_mode_raw or current.get("BOT_UPDATE_MODE"),
                    webhook_url=webhook_url_raw,
                    webhook_path=webhook_path_raw or current.get("WEBHOOK_PATH"),
                    public_base_url=public_base,
                )
            except ValueError as exc:
                return _bot_err(str(exc))

            admin_ids_norm = ",".join(str(i) for i in ids)
            payload = {
                "BOT_TOKEN": token,
                "BOT_USERNAME": uname,
                "ADMIN_IDS": admin_ids_norm,
                "PG_BASE_URL": pg_base,
                "PG_USERNAME": pg_user,
                "PG_PASSWORD": pg_pass,
                "WEB_PORT": str(port_n),
                "PUBLIC_BASE_URL": public_base,
                "CURRENCY": currency,
                "WEBHOOK_URL": webhook_base,
                "WEBHOOK_PATH": webhook_path,
            }
            try:
                current_admin_ids = ",".join(
                    str(i) for i in parse_admin_ids(current.get("ADMIN_IDS") or "")
                )
            except ValueError:
                current_admin_ids = str(current.get("ADMIN_IDS") or "").strip()
            current_norm = {
                "BOT_TOKEN": str(current.get("BOT_TOKEN") or "").strip(),
                "BOT_USERNAME": str(current.get("BOT_USERNAME") or "").strip().lstrip("@"),
                "ADMIN_IDS": current_admin_ids,
                "PG_BASE_URL": normalize_pg_base_url(str(current.get("PG_BASE_URL") or "").strip()),
                "PG_USERNAME": str(current.get("PG_USERNAME") or "").strip(),
                "PG_PASSWORD": str(current.get("PG_PASSWORD") or "").strip(),
                "WEB_PORT": str(current.get("WEB_PORT") or "9000").strip(),
                "PUBLIC_BASE_URL": str(current.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
                "CURRENCY": str(current.get("CURRENCY") or "تومان").strip() or "تومان",
                "WEBHOOK_URL": normalize_webhook_base_url(current.get("WEBHOOK_URL")),
                "WEBHOOK_PATH": normalize_webhook_path(
                    current.get("WEBHOOK_PATH") or "/telegram/webhook"
                ),
            }
            needs_restart = any(
                str(payload.get(k) or "").strip() != str(current_norm.get(k) or "").strip()
                for k in payload
            )

            if preview:
                return JSONResponse(
                    {
                        "ok": True,
                        "needs_restart": needs_restart,
                        "bot_update_mode": update_mode,
                        "webhook_full_url": (webhook_base + webhook_path) if webhook_base else "",
                    }
                )

            if needs_restart and ajax and not confirmed:
                return JSONResponse(
                    {
                        "ok": False,
                        "needs_confirm": True,
                        "needs_restart": True,
                        "error": "برای اعمال این تغییرات ری‌استارت سرویس لازم است",
                        "bot_update_mode": update_mode,
                        "webhook_full_url": (webhook_base + webhook_path) if webhook_base else "",
                    }
                )

            if not needs_restart:
                if ajax:
                    return JSONResponse(
                        {
                            "ok": True,
                            "restarting": False,
                            "changed": False,
                            "message": "تغییری برای ذخیره نبود",
                            "bot_update_mode": update_mode,
                            "webhook_full_url": (webhook_base + webhook_path) if webhook_base else "",
                        }
                    )
                return RedirectResponse("/settings?tab=bot&saved=1", status_code=303)

            update_env_keys(payload)
            ensure_web_secret()
            get_settings.cache_clear()
            reset_pg()
            from app.services.pg_access import clear_platform_pg_capability_cache

            clear_platform_pg_capability_cache()
            pre_boot = BOOT_ID
            schedule_panel_restart(delay_sec=2.5, reason="bot settings saved")
            if ajax:
                return JSONResponse(
                    {
                        "ok": True,
                        "restarting": True,
                        "changed": True,
                        "pre_boot_id": pre_boot,
                        "bot_update_mode": update_mode,
                        "webhook_full_url": (webhook_base + webhook_path) if webhook_base else "",
                        "message": "تنظیمات ذخیره شد — در حال ری‌استارت",
                    }
                )
            return RedirectResponse(
                "/settings?tab=bot&restarting=1",
                status_code=303,
            )

        if tab == "pwa":
            from app.services.pwa import save_pwa_from_form

            ok, msg = await save_pwa_from_form(session, form)
            if not ok:
                return RedirectResponse(
                    "/settings?tab=pwa&err=" + quote(msg),
                    status_code=303,
                )
            return RedirectResponse(
                "/settings?tab=pwa&saved=1&msg=" + quote(msg),
                status_code=303,
            )

        if tab == "appearance":
            from app.services.bot_appearance import save_appearance_from_form
            from app.services.setup_wizard import current_setup_values

            token = (current_setup_values().get("BOT_TOKEN") or "").strip()
            ok, msg = await save_appearance_from_form(
                session, form, token=token, upload_prefix="main"
            )
            if not ok:
                return RedirectResponse(
                    "/settings?tab=appearance&err=" + quote(msg),
                    status_code=303,
                )
            return RedirectResponse(
                "/settings?tab=appearance&saved=1&msg=" + quote(msg),
                status_code=303,
            )

        if tab == "menu" or form.get("menu_layout_save"):
            await _save_menu_layout(session, form)
            return RedirectResponse("/settings?tab=menu&saved=1", status_code=303)

        known = keys_for_tab(tab)
        if tab == "menu":
            known = known | {"menu_order"}

        payload: dict[str, str] = {}
        for key in TOGGLE_KEYS:
            if key in known:
                payload[key] = "1" if form.get(f"s_{key}") else "0"
        for key in known:
            if key in TOGGLE_KEYS or key in IMAGE_KEYS:
                continue
            raw = form.get(f"s_{key}")
            if raw is not None and not isinstance(raw, UploadFile):
                val = str(raw)
                if key in SECRET_KEYS and should_keep_secret_value(val):
                    continue
                if key.startswith("btn_style_"):
                    from app.services.button_styles import normalize_style

                    val = normalize_style(val)
                if key in ("user_alert_low_traffic_pct", "user_alert_low_time_pct"):
                    from app.services.users import clamp_alert_percent

                    val = clamp_alert_percent(val)
                if key == "force_join_channel":
                    from app.services.users import normalize_force_join_channel_value

                    val = normalize_force_join_channel_value(val)
                payload[key] = val
        if tab == "daily_report":
            from app.services.daily_report import (
                ACTOR_OWNER,
                metrics_for_actor,
                serialize_metric_keys,
            )

            chosen = [
                m.key
                for m in metrics_for_actor(ACTOR_OWNER)
                if form.get(f"m_{m.key}")
            ]
            payload["admin_daily_report_metrics"] = serialize_metric_keys(
                chosen, actor=ACTOR_OWNER
            )
        uploads = DATA_DIR / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        max_image_bytes = 5 * 1024 * 1024
        for key in IMAGE_KEYS:
            if key not in known:
                continue
            if form.get(f"s_{key}_clear"):
                payload[key] = ""
                continue
            upload = form.get(f"s_{key}")
            if isinstance(upload, UploadFile) and upload.filename:
                name = upload.filename.lower()
                ext = Path(name).suffix
                if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    continue
                dest_name = f"{key}_{uuid.uuid4().hex[:10]}{ext}"
                dest = uploads / dest_name
                content = await upload.read(max_image_bytes + 1)
                if len(content) > max_image_bytes:
                    continue
                if content:
                    dest.write_bytes(content)
                    payload[key] = f"uploads/{dest_name}"
        if tab == "terms" and payload:
            from app.services.rich_text import merge_rich_settings_on_save

            existing = await get_all_settings(session)
            payload = merge_rich_settings_on_save(existing, payload)
        if payload:
            await set_settings_bulk(session, payload)
        next_url = safe_internal_next(request.query_params.get("next"), "")
        if next_url:
            sep = "&" if "?" in next_url else "?"
            return RedirectResponse(f"{next_url}{sep}saved=1", status_code=303)
        return RedirectResponse(f"/settings?tab={tab}&saved=1", status_code=303)

    @app.get("/broadcast", response_class=HTMLResponse)
    async def broadcast_page(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.broadcast import AUDIENCE_LABELS, list_broadcast_history

        history = await list_broadcast_history(session, limit=40)
        return render(
            request,
            "broadcast.html",
            {
                "staff": staff,
                "history": history,
                "audience_labels": AUDIENCE_LABELS,
                "flash_ok": request.query_params.get("ok"),
                "flash_err": request.query_params.get("err"),
            },
        )

    @app.post("/broadcast")
    async def broadcast_send(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
        text: str = Form(...),
        audience: str = Form("all"),
    ):
        from app.bot import create_bot
        from app.services.broadcast import send_broadcast

        audience = (audience or "all").strip()
        if audience not in {"all", "users", "resellers", "admins"}:
            audience = "all"
        bot = create_bot()
        try:
            result = await send_broadcast(
                bot,
                session,
                text=text,
                audience=audience,
                created_by=str(staff.get("username") or "admin"),
            )
        except ValueError as e:
            return _redirect_msg("/broadcast", err=str(e))
        except Exception as e:
            return _redirect_msg("/broadcast", err=f"خطا در ارسال: {e}")
        finally:
            await bot.session.close()
        msg = f"ارسال شد: {result['ok']} موفق از {result['total']} (ناموفق: {result['fail']})"
        return _redirect_msg("/broadcast", ok=msg)

    @app.post("/broadcast/history/clear")
    async def broadcast_history_clear(
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.broadcast import clear_broadcast_history

        n = await clear_broadcast_history(session)
        return _redirect_msg("/broadcast", ok=f"{n} رکورد تاریخچه حذف شد")

    @app.post("/broadcast/history/{log_id}/delete")
    async def broadcast_history_delete(
        log_id: int,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.broadcast import delete_broadcast_log

        ok = await delete_broadcast_log(session, log_id)
        if not ok:
            return _redirect_msg("/broadcast", err="رکورد یافت نشد")
        return _redirect_msg("/broadcast", ok="رکورد حذف شد")

    return app
