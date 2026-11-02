from __future__ import annotations

import logging
import json
from collections.abc import Awaitable, Callable
import asyncio
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote

import httpx

from app.config import get_settings, normalize_pg_subscription_path, pg_api_base_candidates
from app.services.redact import redact
from app.services.pg_read_resilience import DNS_CACHE_TTL_SEC, panel_read_request, reset_read_circuits, validate_panel_base
from app.services.security_policy import UnsafePgUrlError, assert_safe_pg_base_url

logger = logging.getLogger(__name__)


async def _mutation_request(
    client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Keep mutation timeouts and unknown outcomes independent of web reads."""
    return await client.request(method, url, **kwargs)


# Read/write keep the old 30 s ceiling so slow panel writes do not turn into
# unknown outcomes; a dead link now fails at connect instead of after 30 s.
PG_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0, pool=10.0)
PG_PROBE_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
# PasarGuard's uvicorn drops idle keep-alive sockets after 5 s; expiring ours
# first avoids "server disconnected" on the next request.
PG_HTTP_LIMITS = httpx.Limits(max_connections=32, max_keepalive_connections=8, keepalive_expiry=4.0)
# Transport retries only repeat failed connects (nothing was sent), so they
# are safe for mutations as well as reads.
PG_CONNECT_RETRIES = 2
RETIRED_CLIENT_GRACE_SEC = 120.0
MAX_VALIDATED_TARGETS = 16

_retiring_clients: set[asyncio.Task[None]] = set()
_validated_targets: dict[str, tuple[str, float]] = {}
# Configured PG_BASE_URL -> API root that identified itself as PasarGuard.
_discovered_api_bases: dict[str, str] = {}


def _remember_api_base(configured: str, api_base: str) -> None:
    if len(_discovered_api_bases) >= MAX_VALIDATED_TARGETS:
        _discovered_api_bases.clear()
    _discovered_api_bases[configured] = api_base


def new_panel_http_client(base_url: str, *, timeout: httpx.Timeout = PG_HTTP_TIMEOUT) -> httpx.AsyncClient:
    """HTTP client with the shared PasarGuard transport policy."""
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=timeout,
        # Never follow redirects with Bearer tokens (SSRF / credential forwarding)
        follow_redirects=False,
        transport=httpx.AsyncHTTPTransport(retries=PG_CONNECT_RETRIES, limits=PG_HTTP_LIMITS),
    )


def is_pg_transport_failure(exc: BaseException) -> bool:
    """True when the panel could not be reached, as opposed to answering with an error."""
    return isinstance(exc, (httpx.TransportError, TimeoutError))


async def _close_after_grace(close: Callable[[], Awaitable[None]]) -> None:
    await asyncio.sleep(RETIRED_CLIENT_GRACE_SEC)
    try:
        await close()
    except (httpx.HTTPError, OSError, RuntimeError):
        logger.debug("closing a retired PasarGuard client failed", exc_info=True)


def _schedule_close(close: Callable[[], Awaitable[None]]) -> None:
    """Close later, once requests already running on the client had time to end."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_close_after_grace(close))
    _retiring_clients.add(task)
    task.add_done_callback(_retiring_clients.discard)


def _retire_client(client: object) -> None:
    """Release the sockets of a client dropped from a cache."""
    if isinstance(client, PasarGuardClient):
        _schedule_close(client.close)


def _retire_http_client(client: httpx.AsyncClient) -> None:
    _schedule_close(client.aclose)


async def _close_unshared_client(client: PasarGuardClient) -> None:
    try:
        await client.close()
    except (httpx.HTTPError, OSError, RuntimeError):
        logger.debug("closing an unused PasarGuard client failed", exc_info=True)


@dataclass(frozen=True, slots=True)
class WireGuardResponse:
    """What the panel answered for a subscription's WireGuard files."""

    status: int | None
    data: bytes | None


class PasarGuardError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def user_message(self, *, fallback: str | None = None) -> str:
        """Short Persian-friendly message extracted from API error body."""
        from app.services.credential_policy import friendly_pg_error

        detail = _pg_error_detail(self.body)
        base = detail or str(self.args[0] if self.args else "") or (fallback or "خطای پاسارگارد")
        return friendly_pg_error(base, status_code=self.status_code)[:500]


def is_pg_not_found(exc: BaseException) -> bool:
    """True when PasarGuard reports the target resource is missing (HTTP 404).

    Used for idempotent DELETE: a clean 404 means the desired end-state
    (resource gone) is already true — not a transport/outage failure.
    """
    return isinstance(exc, PasarGuardError) and exc.status_code == 404


def is_pg_permission_denied(exc: Exception) -> bool:
    """True when PasarGuard refused a resource (not a transport outage).

    Limited roles often 403 on ``/api/admins``, hosts, nodes, or templates
    even after a valid token — callers must not paint that as «قطع اتصال».
    """
    return isinstance(exc, PasarGuardError) and exc.status_code in (403, 404, 405)


ADMIN_DIRECTORY_PAGE_SIZE = 100
ADMIN_DIRECTORY_MAX_PAGES = 20


class AdminDirectoryIncomplete(Exception):
    """The admin list could not be read to its end, so absence is unproven."""


def _is_admin_named(payload: Any, username: str) -> bool:
    """True only for an admin record carrying exactly this username."""
    if not isinstance(payload, dict):
        return False
    got = str(payload.get("username") or "").strip()
    return bool(got) and got.lower() == username.strip().lower()


def _pg_error_detail(body: Any) -> str | None:
    if body is None:
        return None
    text = body if isinstance(body, str) else None
    data = None
    if isinstance(body, dict):
        data = body
    elif isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
    if text and data is None:
        try:
            import json

            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
            else:
                return text.strip()[:400] or None
        except Exception:
            return text.strip()[:400] or None
    if not isinstance(data, dict):
        return None
    for key in ("detail", "message", "error", "msg"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, list):
            parts = []
            for item in val:
                if isinstance(item, dict):
                    loc_parts = [
                        str(x)
                        for x in (item.get("loc") or [])
                        if x not in {"body", "query", "path"}
                    ]
                    loc = ".".join(loc_parts)
                    msg = item.get("msg") or item.get("message") or item.get("type") or ""
                    parts.append(f"{loc}: {msg}".strip(": ").strip())
                else:
                    parts.append(str(item))
            joined = "؛ ".join(p for p in parts if p)
            if joined:
                return joined[:400]
        if isinstance(val, dict):
            inner = _pg_error_detail(val) or _field_errors_detail(val)
            if inner:
                return inner
    return None


def _field_errors_detail(fields: dict[str, Any]) -> str | None:
    """Join a ``{field: message | [messages]}`` mapping into ``field: message`` parts."""
    parts: list[str] = []
    for name, msgs in fields.items():
        items = msgs if isinstance(msgs, list) else [msgs]
        text = " / ".join(str(m).strip() for m in items if isinstance(m, (str, int, float)) and str(m).strip())
        if text:
            parts.append(f"{name}: {text}")
    return "؛ ".join(parts)[:400] or None


def build_user_create_payload(
    *,
    username: str,
    group_ids: list[int],
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    note: str | None = None,
    status: str = "active",
) -> dict[str, Any]:
    """Payload compatible with current PasarGuard UserCreate schema."""
    from datetime import datetime, timezone

    payload: dict[str, Any] = {
        "username": username,
        "status": status,
        "group_ids": group_ids,
        "proxy_settings": {},
    }
    if data_limit is not None:
        payload["data_limit"] = int(data_limit)
    if expire_ts is not None:
        if expire_ts <= 0:
            payload["expire"] = 0
        else:
            # Prefer ISO datetime (newer PG); timestamp still accepted by validators.
            payload["expire"] = (
                datetime.fromtimestamp(int(expire_ts), tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
    if hwid_limit is not None:
        # 0 / negative → omit (unlimited); positive → device cap
        if int(hwid_limit) > 0:
            payload["hwid_limit"] = int(hwid_limit)
    if note:
        payload["note"] = note
    return payload


def build_user_modify_payload(
    *,
    username: str | None = None,
    group_ids: list[int] | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
    hwid_limit: int | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    from datetime import datetime, timezone

    payload: dict[str, Any] = {}
    if username is not None:
        payload["username"] = username
    if group_ids is not None:
        payload["group_ids"] = group_ids
    if data_limit is not None:
        payload["data_limit"] = int(data_limit)
    if expire_ts is not None:
        if expire_ts <= 0:
            payload["expire"] = 0
        else:
            payload["expire"] = (
                datetime.fromtimestamp(int(expire_ts), tz=timezone.utc)
                .replace(microsecond=0)
                .isoformat()
                .replace("+00:00", "Z")
            )
    if hwid_limit is not None:
        payload["hwid_limit"] = int(hwid_limit) if int(hwid_limit) > 0 else None
    if status:
        payload["status"] = status
    return payload


class PasarGuardClient:
    def __init__(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        access_token: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.settings = get_settings()
        # Recreated/rotated credentials must never reuse another client's ACL reads.
        self._cache_partition = secrets.token_hex(16)
        # Use settings value as-is (already normalized with path preserved)
        self.base_url = (self.settings.pg_base_url or "").rstrip("/")
        if self.base_url:
            try:
                self.base_url = assert_safe_pg_base_url(self.base_url, resolve_dns=False)
            except UnsafePgUrlError:
                # Keep empty so ensure_token fails closed instead of dialing SSRF targets
                logger.error("Refusing unsafe PasarGuard base URL")
                self.base_url = ""
        # Optional per-admin credentials (reseller shop) — never fall back to owner silently
        self._login_username = (username or "").strip() or None
        self._login_password = (password or "").replace("\r", "").strip() or None
        # Admin API key (X-Api-Key). Mutually exclusive with Bearer/password on this client.
        # One auth method per client — never send both Authorization Bearer and X-Api-Key.
        explicit_api_key = (api_key or "").replace("\r", "").strip() or None
        # Env/owner client (no per-admin overrides): prefer PG_API_KEY over password login.
        env_api_key = None
        if (
            explicit_api_key is None
            and username is None
            and password is None
            and access_token is None
        ):
            env_api_key = (self.settings.pg_api_key or "").replace("\r", "").strip() or None
        self._api_key = explicit_api_key or env_api_key
        if self._api_key:
            self._token: str | None = None
            self._login_password = None
        elif access_token is not None:
            self._token = access_token or None
        elif self._login_username:
            self._token = None
        else:
            self._token = (self.settings.pg_access_token or None) or None
        if self._token == "":
            self._token = None
        self._client = new_panel_http_client(self.base_url)

    @property
    def uses_api_key(self) -> bool:
        return bool(self._api_key)

    @property
    def is_auth_ready(self) -> bool:
        """True when this client can authorize requests (API key or Bearer token)."""
        return bool(self._api_key) or bool(self._token)

    async def close(self) -> None:
        await self._client.aclose()

    def _http(self) -> httpx.AsyncClient:
        """The pooled client, reopened if a cache retired it while a caller still held this object."""
        if getattr(self._client, "is_closed", False) is True:
            self._client = new_panel_http_client(self.base_url)
        return self._client

    async def _ensure_safe_target(self, *, protected: bool = True) -> None:
        if not self.base_url:
            return
        now = time.monotonic()
        if getattr(self, "_validated_base", None) == self.base_url and getattr(self, "_validated_until", 0) > now:
            return
        # Shared by base URL so each new shop client does not repeat the DNS check.
        known = _validated_targets.get(self.base_url)
        if known is None or known[1] <= now:
            validated = await validate_panel_base(self.base_url, protected=protected)
            if len(_validated_targets) >= MAX_VALIDATED_TARGETS:
                _validated_targets.clear()
            known = (validated, time.monotonic() + DNS_CACHE_TTL_SEC)
            _validated_targets[self.base_url] = known
        self._validated_base, self._validated_until = known

    async def _rebind_base(self, base: str) -> None:
        base = (base or "").rstrip("/")
        if not base or base == self.base_url:
            return
        try:
            base = await validate_panel_base(base)
        except UnsafePgUrlError as exc:
            raise PasarGuardError(str(exc)) from exc
        logger.warning(
            "PasarGuard API root adjusted %s → %s (path was dashboard UI, not API)",
            self.base_url,
            base,
        )
        old = self._client
        self.base_url = base
        self._client = new_panel_http_client(base)
        # Concurrent requests may still be using the old client.
        _retire_http_client(old)

    async def _ensure_api_base(self) -> None:
        """If PG_BASE_URL includes a dashboard path, switch to the real API root."""
        await self._ensure_safe_target()
        if getattr(self, "_api_base_resolved", False):
            return
        configured = self.settings.pg_base_url or self.base_url
        candidates = pg_api_base_candidates(configured)
        if len(candidates) <= 1:
            self._api_base_resolved = True
            return
        # Another client already found the API root: skip downloading openapi.json again.
        known = _discovered_api_bases.get(configured)
        if known in candidates:
            await self._rebind_base(known)
            self._api_base_resolved = True
            return
        for base in candidates:
            try:
                async with new_panel_http_client(base, timeout=PG_PROBE_TIMEOUT) as probe:
                    # Only accept a candidate that identifies as PasarGuard.
                    # Do NOT treat bare /api/system 401/403 as proof — that
                    # pattern matches many unrelated internal services (SSRF).
                    r = await panel_read_request(probe, "GET", "/openapi.json")
                    if r.status_code == 200 and "PasarGuard" in (r.text or ""):
                        await self._rebind_base(base)
                        self._api_base_resolved = True
                        _remember_api_base(configured, base)
                        return
            except Exception:
                continue

    async def ensure_token(self) -> str:
        if self._api_key:
            await self._ensure_api_base()
            # Sentinel so callers that treat ensure_token as "auth ready" stay truthful.
            # Real requests use X-Api-Key via _headers — never Bearer with this client.
            return "__apikey__"
        if self._token:
            return self._token
        await self._ensure_api_base()
        if self._login_username:
            username = self._login_username
            password = self._login_password or ""
        else:
            username = (self.settings.pg_username or "").strip()
            password = (self.settings.pg_password or "").replace("\r", "").strip()
        if not username or not password:
            raise PasarGuardError(
                "اعتبارنامه پاسارگارد ناقص است"
                if self._login_username
                else "PG_USERNAME / PG_PASSWORD missing in .env"
            )

        attempts = [
            {"grant_type": "password", "username": username, "password": password},
            {"username": username, "password": password},
        ]
        candidates = pg_api_base_candidates(self.settings.pg_base_url or self.base_url)
        # Prefer already-resolved base first
        if self.base_url:
            candidates = [self.base_url] + [c for c in candidates if c != self.base_url]

        last: httpx.Response | None = None
        last_base = candidates[0] if candidates else self.base_url
        for base in candidates:
            last_base = base
            response, token = await self._login_at(base, attempts)
            if response is not None:
                last = response
            if token:
                return token

        assert last is not None
        detail = (last.text or "")[:300]
        hint = ""
        if last.status_code == 405 and len(candidates) > 1:
            hint = (
                " Hint: PG_BASE_URL includes a dashboard path; API is usually at the domain root "
                f"(try {candidates[-1]})."
            )
        raise PasarGuardError(
            f"PasarGuard login failed ({last.status_code}) at "
            f"{last_base}/api/admin/token — check PG_BASE_URL / user / password.{hint} {detail}",
            last.status_code,
            last.text,
        )

    async def _login_at(self, base: str, attempts: list[dict[str, str]]) -> tuple[httpx.Response | None, str | None]:
        """Try the password grants at one API root; reuse the open connection when possible."""
        reuse = base == self.base_url
        # Never follow redirects on credential POSTs (prevents auth-forwarding SSRF)
        probe = self._http() if reuse else new_panel_http_client(base)
        last: httpx.Response | None = None
        try:
            for data in attempts:
                last = await panel_read_request(probe, "POST", "/api/admin/token", data=data)
                if last.status_code < 400:
                    return last, await self._accept_login(base, last)
                if last.status_code in (401, 403):
                    detail = (last.text or "")[:300]
                    raise PasarGuardError(
                        redact(
                            f"PasarGuard login failed ({last.status_code}) at "
                            f"{base}/api/admin/token — check PG_USERNAME / PG_PASSWORD. {detail}"
                        ),
                        last.status_code,
                        redact(last.text),
                    )
                if last.status_code not in (404, 405):
                    break
        finally:
            if not reuse:
                await probe.aclose()
        return last, None

    async def _accept_login(self, base: str, response: httpx.Response) -> str:
        await self._rebind_base(base)
        self._api_base_resolved = True
        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise PasarGuardError("PasarGuard login response missing access_token", body=payload)
        self._token = token
        # Drop plaintext password from memory once a Bearer token exists
        # (cached clients must not retain PG passwords).
        self._login_password = None
        logger.info("PasarGuard login OK · base=%s", self.base_url)
        return token

    async def _headers(self) -> dict[str, str]:
        if self._api_key:
            await self._ensure_api_base()
            return {"X-Api-Key": self._api_key}
        token = await self.ensure_token()
        return {"Authorization": f"Bearer {token}"}

    def _read_cache_ident(self) -> str:
        """Cache partition: never share Owner reads with an L1/L2 client."""
        user = (getattr(self, "_login_username", None) or "").strip()
        base = (self.base_url or "").rstrip("/")
        if user:
            return f"u:{user}@{base}:{self._cache_partition}"
        return f"owner@{base}:{self._cache_partition}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        use_cache: bool = True,
        resolve_api_base: bool = True,
        **kwargs: Any,
    ) -> Any:
        from app.services.pg_read_cache import cache_get, cache_put, invalidate_ident

        method_u = (method or "GET").upper()
        await self._ensure_safe_target(protected=method_u == "GET")
        ident = self._read_cache_ident()
        if method_u != "GET":
            invalidate_ident(ident)
        elif use_cache:
            cached = cache_get(ident, path, kwargs.get("params"))
            if cached is not None:
                return cached
        headers = kwargs.pop("headers", {})
        if auth:
            headers.update(await self._headers())
        elif resolve_api_base:
            await self._ensure_api_base()
        send_request = panel_read_request if method_u == "GET" else _mutation_request
        resp = await send_request(self._http(), method, path, headers=headers, **kwargs)
        if resp.status_code == 401 and auth:
            # Bearer: clear JWT and re-login once. API key: same key cannot recover —
            # skip retry (would only double the same unauthorized request).
            if not self._api_key:
                self._token = None
                headers.update(await self._headers())
                resp = await send_request(self._http(), method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
            if resp.status_code == 422:
                logger.warning(
                    "PasarGuard rejected %s %s: %s", method, path, redact(resp.text, limit=600)
                )
            raise PasarGuardError(
                f"{method} {path} failed ({resp.status_code})",
                resp.status_code,
                resp.text,
            )
        if resp.status_code == 204 or not resp.content:
            return None
        content_type = resp.headers.get("content-type", "")
        if "application/json" in content_type:
            payload = resp.json()
        else:
            payload = resp.text
        if method_u == "GET" and use_cache:
            cache_put(ident, path, kwargs.get("params"), payload)
        return payload

    async def get_users(self, **params: Any) -> dict:
        return await self.request("GET", "/api/users", params=params)

    async def get_user_by_username(self, username: str) -> dict:
        return await self.request("GET", f"/api/user/by-username/{username}")

    async def get_user_by_id(self, user_id: int) -> dict:
        return await self.request("GET", f"/api/user/by-id/{user_id}")

    async def create_user(self, payload: dict) -> dict:
        return await self.request("POST", "/api/user", json=payload)

    async def create_user_from_template(self, payload: dict) -> dict:
        return await self.request("POST", "/api/user/from_template", json=payload)

    async def modify_user_by_id(self, user_id: int, payload: dict) -> dict:
        return await self.request("PUT", f"/api/user/by-id/{user_id}", json=payload)

    async def modify_user_with_template(self, user_id: int, payload: dict) -> dict:
        return await self.request(
            "PUT", f"/api/user/from_template/by-id/{user_id}", json=payload
        )

    async def delete_user_by_id(self, user_id: int) -> None:
        await self.request("DELETE", f"/api/user/by-id/{user_id}")

    async def reset_user_by_id(self, user_id: int) -> dict:
        return await self.request("POST", f"/api/user/by-id/{user_id}/reset")

    async def revoke_sub_by_id(self, user_id: int) -> dict:
        return await self.request("POST", f"/api/user/by-id/{user_id}/revoke_sub")

    async def set_disabled_by_id(self, user_id: int, disabled: bool) -> dict:
        return await self.request(
            "PUT",
            f"/api/user/by-id/{user_id}/disabled",
            json={"disabled": disabled},
        )

    async def set_owner_by_id(self, user_id: int, admin_username: str) -> dict:
        return await self.request(
            "PUT",
            f"/api/user/by-id/{user_id}/set_owner",
            params={"admin_username": admin_username},
        )

    async def get_user_usage(self, user_id: int) -> Any:
        return await self.request("GET", f"/api/user/by-id/{user_id}/usage")

    async def get_user_templates(self) -> Any:
        return await self.request("GET", "/api/user_templates")

    async def get_user_templates_simple(self) -> list[dict]:
        data = await self.request("GET", "/api/user_templates/simple")
        return as_list(data, "templates")

    async def create_user_template(self, payload: dict) -> dict:
        return await self.request("POST", "/api/user_template", json=payload)

    async def get_user_template(self, template_id: int) -> dict:
        return await self.request("GET", f"/api/user_template/{template_id}")

    async def delete_user_template(self, template_id: int) -> None:
        await self.request("DELETE", f"/api/user_template/{template_id}")

    async def modify_user_template(self, template_id: int, payload: dict) -> dict:
        return await self.request("PUT", f"/api/user_template/{template_id}", json=payload)

    async def get_groups(self) -> Any:
        return await self.request("GET", "/api/groups")

    async def get_groups_simple(self) -> list[dict]:
        data = await self.request("GET", "/api/groups/simple")
        return as_list(data, "groups")

    async def create_group(self, payload: dict) -> dict:
        return await self.request("POST", "/api/group", json=payload)

    async def get_group(self, group_id: int) -> dict:
        return await self.request("GET", f"/api/group/{group_id}")

    async def modify_group(self, group_id: int, payload: dict) -> dict:
        return await self.request("PUT", f"/api/group/{group_id}", json=payload)

    async def delete_group(self, group_id: int) -> None:
        await self.request("DELETE", f"/api/group/{group_id}")

    async def set_group_disabled(self, group_id: int, disabled: bool) -> Any:
        path = "/api/groups/bulk/disable" if disabled else "/api/groups/bulk/enable"
        return await self.request("POST", path, json={"ids": [group_id]})

    async def get_inbounds(self) -> list:
        data = await self.request("GET", "/api/inbounds")
        return as_any_list(data, "inbounds")

    async def get_inbounds_details(self) -> Any:
        return await self.request("GET", "/api/inbounds/details")

    async def get_hosts(self) -> list[dict]:
        data = await self.request("GET", "/api/hosts")
        return as_list(data, "hosts")

    async def get_host(self, host_id: int) -> dict:
        return await self.request("GET", f"/api/host/{host_id}")

    async def create_host(self, payload: dict) -> dict:
        return await self.request("POST", "/api/host/", json=payload)

    async def modify_host(self, host_id: int, payload: dict) -> dict:
        return await self.request("PUT", f"/api/host/{host_id}", json=payload)

    async def delete_host(self, host_id: int) -> None:
        await self.request("DELETE", f"/api/host/{host_id}")

    async def set_host_disabled(self, host_id: int, disabled: bool) -> Any:
        path = "/api/hosts/bulk/disable" if disabled else "/api/hosts/bulk/enable"
        return await self.request("POST", path, json={"ids": [host_id]})

    async def get_admins(self) -> list[dict]:
        data = await self.request("GET", "/api/admins")
        return as_list(data, "admins")

    async def get_admins_simple(self) -> list[dict]:
        data = await self.request("GET", "/api/admins/simple")
        return as_list(data, "admins")

    async def get_current_admin(self) -> dict | None:
        """The authenticated admin's own account (``GET /api/admin``).

        PasarGuard's panel uses this for the signed-in user. It does **not**
        require ``admins.read``, so a role that can only create users still
        receives its own ``AdminDetails`` (nested ``role`` / limits).

        Returns the admin dict, or ``None`` if the payload is empty. HTTP
        errors propagate as ``PasarGuardError`` so callers can tell a missing
        route (404) from a real outage.
        """
        data = await self.request("GET", "/api/admin")
        if isinstance(data, dict):
            if data.get("username") or data.get("id") is not None or data.get("role"):
                self._api_base_resolved = True
                return data
            inner = data.get("admin")
            if isinstance(inner, dict) and (
                inner.get("username") or inner.get("id") is not None or inner.get("role")
            ):
                self._api_base_resolved = True
                return inner
        return None

    async def get_admin(self, username: str) -> dict | None:
        """Fetch one admin (with usage metrics) by username.

        Prefer the current-admin endpoint when the requested name is *this*
        token, then direct by-username paths. Limited roles often cannot list
        ``/api/admins`` even when they can read their own account.
        """
        _gate, admin = await self.get_admin_gate(username)
        return admin

    async def get_admin_gate(self, username: str) -> tuple[str, dict | None]:
        """Like ``get_admin`` but distinguishes *confirmed missing* (a clean
        404 from every lookup path) from *unreachable* (any network/timeout/
        5xx error along the way).

        Callers (e.g. the pg_staff web-access gate) must never treat a
        temporary PasarGuard outage as proof the admin was deleted — doing so
        would permanently revoke a legitimate staff member's web access just
        because PasarGuard was briefly down. Returns ``(gate, admin)`` where
        ``gate`` is one of ``"ok"``, ``"missing"``, ``"unreachable"``.

        Only an exact (case-insensitive) username match is ever returned:
        billing and access decisions are made on this record, so a similar
        name returned by a substring search must never stand in for it.
        """
        username = (username or "").strip()
        if not username:
            return "missing", None
        saw_unreachable = False

        # Own account first: no admins.read required. Never treat the token
        # holder's record as a *different* admin being looked up.
        try:
            current = await self.get_current_admin()
            if _is_admin_named(current, username):
                return "ok", current
        except Exception as exc:
            # 403/404/405: endpoint hidden or forbidden on this role/version.
            # Keep looking; do not flip a later confirmed-404 into unreachable.
            if getattr(exc, "status_code", None) not in (403, 404, 405):
                saw_unreachable = True

        enc = quote(username, safe="")
        for path in (f"/api/admin/by-username/{enc}", f"/api/admin/{enc}"):
            try:
                data = await self.request("GET", path)
            except Exception as exc:
                saw_unreachable = saw_unreachable or not is_pg_not_found(exc)
                continue
            if _is_admin_named(data, username):
                return "ok", data

        try:
            found = await self._find_admin_in_directory(username)
        except Exception as exc:
            saw_unreachable = saw_unreachable or not is_pg_not_found(exc)
            found = None
        if found is not None:
            return "ok", found
        return ("unreachable" if saw_unreachable else "missing"), None

    async def _find_admin_in_directory(self, username: str) -> dict | None:
        """Exact match from ``/api/admins``; ``None`` only after a complete scan.

        PasarGuard filters ``username`` as a substring, so the wanted admin can
        sit behind similar names on a later page. A scan that cannot reach the
        end raises ``AdminDirectoryIncomplete`` instead of claiming absence.
        """
        try:
            return await self._scan_admin_directory({"username": username}, username)
        except PasarGuardError as exc:
            if exc.status_code not in (400, 422):
                raise
        # Panel rejected the filter parameter: scan the plain list instead.
        return await self._scan_admin_directory({}, username)

    async def _scan_admin_directory(self, filters: dict[str, str], username: str) -> dict | None:
        seen: set[str] = set()
        for page in range(ADMIN_DIRECTORY_MAX_PAGES):
            params = {**filters, "offset": page * ADMIN_DIRECTORY_PAGE_SIZE, "limit": ADMIN_DIRECTORY_PAGE_SIZE}
            admins = as_list(await self.request("GET", "/api/admins", params=params), "admins")
            match = next((admin for admin in admins if _is_admin_named(admin, username)), None)
            if match is not None:
                return match
            if len(admins) < ADMIN_DIRECTORY_PAGE_SIZE:
                return None
            names = {str(admin.get("username") or "").strip().lower() for admin in admins}
            if len(admins) > ADMIN_DIRECTORY_PAGE_SIZE or names <= seen:
                # The panel ignored offset/limit; later pages would repeat this one.
                raise AdminDirectoryIncomplete(username)
            seen |= names
        raise AdminDirectoryIncomplete(username)

    async def get_admin_roles(self) -> list[dict]:
        """Pasarguard admin roles (owner-defined access levels)."""
        try:
            data = await self.request("GET", "/api/admin-roles/simple")
            roles = as_list(data, "roles", "items")
            if roles:
                return roles
        except Exception:
            logger.debug("get_admin_roles: ignored failure", exc_info=True)
        try:
            data = await self.request("GET", "/api/admin-roles")
            return as_list(data, "roles", "items")
        except Exception:
            return []

    async def get_admin_role(self, role_id: int) -> dict:
        """Full role including permissions / limits / access."""
        data = await self.request("GET", f"/api/admin-role/{int(role_id)}")
        return data if isinstance(data, dict) else {}

    async def create_admin(self, payload: dict) -> dict:
        return await self.request("POST", "/api/admin", json=payload)

    async def modify_admin(self, username: str, payload: dict) -> dict:
        """Update admin — prefer by-id (case-safe); username path is case-sensitive in PG."""
        uname = (username or "").strip()
        if not uname:
            raise PasarGuardError(
                "نام ادمین نامعتبر است. "
                "علت محتمل: نام خالی. "
                "راه حل: نام ادمین پاسارگارد را دوباره انتخاب کنید.",
                400,
            )
        admin = await self.get_admin(uname)
        if not admin:
            raise PasarGuardError(
                f"ادمین «{uname}» در پاسارگارد یافت نشد. "
                "علت محتمل: نام اشتباه یا حذف‌شده. "
                "راه حل: لیست ادمین‌ها را رفرش کنید و دوباره تلاش کنید.",
                404,
            )
        admin_id = admin.get("id")
        exact = str(admin.get("username") or uname).strip()
        errors: list[str] = []
        if admin_id is not None:
            try:
                return await self.request(
                    "PUT", f"/api/admin/by-id/{int(admin_id)}", json=payload
                )
            except PasarGuardError as e:
                if e.status_code not in (404, 405):
                    raise
                errors.append(str(e))
        for path in (
            f"/api/admin/by-username/{exact}",
            f"/api/admin/{exact}",
        ):
            try:
                return await self.request("PUT", path, json=payload)
            except PasarGuardError as e:
                if e.status_code not in (404, 405):
                    raise
                errors.append(str(e))
        detail = errors[-1] if errors else "مسیر به‌روزرسانی ادمین در دسترس نیست"
        raise PasarGuardError(
            f"به‌روزرسانی ادمین پاسارگارد ناموفق بود: {detail}. "
            "علت محتمل: نسخه پنل پاسارگارد یا دسترسی نقش. "
            "راه حل: نقش ادمین اصلی و نسخه پاسارگارد را بررسی کنید.",
            404,
        )

    async def delete_admin(self, username: str) -> Any:
        """Delete admin — prefer by-id (case-safe)."""
        uname = (username or "").strip()
        if not uname:
            raise PasarGuardError("نام ادمین نامعتبر است", 400)
        admin = await self.get_admin(uname)
        if not admin:
            raise PasarGuardError(f"ادمین «{uname}» در پاسارگارد یافت نشد", 404)
        admin_id = admin.get("id")
        exact = str(admin.get("username") or uname).strip()
        if admin_id is not None:
            try:
                return await self.request("DELETE", f"/api/admin/by-id/{int(admin_id)}")
            except PasarGuardError as e:
                if e.status_code not in (404, 405):
                    raise
        for path in (
            f"/api/admin/by-username/{exact}",
            f"/api/admin/{exact}",
        ):
            try:
                return await self.request("DELETE", path)
            except PasarGuardError as e:
                if e.status_code not in (404, 405):
                    raise
        raise PasarGuardError(
            f"حذف ادمین «{exact}» ناموفق بود. "
            "علت محتمل: ادمین مالک یا دسترسی ناکافی. "
            "راه حل: از پنل اصلی پاسارگارد وضعیت را بررسی کنید.",
            404,
        )

    async def get_system_stats(self) -> dict:
        data = await self.request("GET", "/api/system")
        return data if isinstance(data, dict) else {"raw": data}

    async def get_nodes(self) -> list[dict]:
        data = await self.request("GET", "/api/nodes")
        return as_list(data, "nodes")

    async def get_nodes_simple(self) -> list[dict]:
        data = await self.request("GET", "/api/nodes/simple")
        return as_list(data, "nodes")

    async def get_nodes_realtime(self) -> Any:
        return await self.request("GET", "/api/nodes/realtime_stats")

    async def get_node(self, node_id: int) -> dict:
        data = await self.request("GET", f"/api/node/{int(node_id)}")
        return data if isinstance(data, dict) else {}

    async def create_node(self, payload: dict) -> dict:
        return await self.request("POST", "/api/node", json=payload)

    async def modify_node(self, node_id: int, payload: dict) -> dict:
        return await self.request("PUT", f"/api/node/{int(node_id)}", json=payload)

    async def delete_node(self, node_id: int) -> Any:
        return await self.request("DELETE", f"/api/node/{int(node_id)}")

    async def reset_node(self, node_id: int) -> Any:
        return await self.request("POST", f"/api/node/{int(node_id)}/reset")

    async def sync_node(self, node_id: int) -> Any:
        return await self.request("PUT", f"/api/node/{int(node_id)}/sync")

    async def reconnect_node(self, node_id: int) -> Any:
        return await self.request("POST", f"/api/node/{int(node_id)}/reconnect")

    async def reconnect_all_nodes(self) -> Any:
        return await self.request("POST", "/api/nodes/reconnect")

    def _subscription_endpoint(self, token: str, suffix: str, subscription_url: str | None) -> str:
        from urllib.parse import quote, urlsplit, urlunsplit

        # Use a path from the stored link when available; never dial its host.
        # Customer-supplied links cannot redirect a panel client to another origin.
        configured = normalize_pg_subscription_path(getattr(self.settings, "pg_subscription_path", "/sub"))
        prefix = subscription_path_from_url(subscription_url) or configured
        origin = urlsplit(self.base_url)
        if origin.scheme not in {"http", "https"} or not origin.netloc:
            raise PasarGuardError("آدرس پاسارگارد تنظیم نشده است")
        if not token or any(char in token for char in "/?#\\"):
            raise PasarGuardError("توکن اشتراک نامعتبر است")
        path = f"{prefix}/{quote(token, safe='')}/{suffix}"
        return urlunsplit((origin.scheme, origin.netloc, path, "", ""))

    async def subscription_info(self, token: str, *, subscription_url: str | None = None) -> dict:
        # Public route is independent of a dashboard/API base path. Fresh reads
        # also make the user's refresh action bypass the short PG GET cache.
        return await self.request(
            "GET", self._subscription_endpoint(token, "info", subscription_url),
            auth=False, use_cache=False, resolve_api_base=False,
            headers={"Accept": "application/json"},
        )

    async def subscription_usage(self, token: str, *, subscription_url: str | None = None) -> Any:
        return await self.request(
            "GET", self._subscription_endpoint(token, "usage", subscription_url),
            auth=False, use_cache=False, resolve_api_base=False,
        )

    async def subscription_xray(self, token: str, *, subscription_url: str | None = None) -> list[dict[str, Any]]:
        """Read bounded native routes without caching credentials or raw error bodies."""
        path = self._subscription_endpoint(token, "xray", subscription_url)
        await self._ensure_safe_target()
        payload = bytearray()
        try:
            async with self._http().stream("GET", path, headers={"Accept": "application/json"}) as response:
                if response.status_code != 200:
                    raise PasarGuardError("Monitoring subscription unavailable", response.status_code)
                async for chunk in response.aiter_bytes(chunk_size=16384):
                    payload.extend(chunk)
                    if len(payload) > 2 * 1024 * 1024:
                        raise PasarGuardError("Monitoring subscription exceeds capacity")
            result = await asyncio.to_thread(json.loads, payload)
        except (httpx.HTTPError, ValueError, RecursionError):
            raise PasarGuardError("Monitoring subscription unavailable") from None
        if not isinstance(result, list) or len(result) > 1000 or any(not isinstance(item, dict) for item in result):
            raise PasarGuardError("Monitoring subscription format unavailable")
        return result

    async def subscription_wireguard_bytes(
        self, token: str, *, subscription_url: str | None = None
    ) -> WireGuardResponse:
        """Fetch the WireGuard payload (``.zip`` of ``.conf`` files, or one plain ``.conf``).

        ``data`` is ``None`` when the panel offered nothing usable; ``status`` tells why
        (``None`` = transport error, 406 = WireGuard format switched off in the panel).
        Raw bytes are used because ``request()`` would corrupt a zip through ``resp.text``.
        """
        path = self._subscription_endpoint(token, "wireguard", subscription_url)
        try:
            await self._ensure_safe_target()
            resp = await panel_read_request(
                self._http(),
                "GET",
                path,
                headers={
                    "Accept": "application/zip, text/plain;q=0.9, */*;q=0.1",
                    "User-Agent": "PGClockBot-WireGuard/1.0",
                },
            )
        except httpx.HTTPError:
            logger.warning("wireguard fetch transport error token=…%s", (token or "")[-6:])
            return WireGuardResponse(status=None, data=None)
        if resp.status_code >= 400 and resp.status_code != 404:
            logger.warning(
                "wireguard fetch failed status=%s token=…%s",
                resp.status_code,
                (token or "")[-6:],
            )
        data = resp.content or b""
        head = data.lstrip()[:64].lower()
        if (
            resp.status_code >= 300
            or len(data) < 16
            or head.startswith(b"<!doctype")
            or head.startswith(b"<html")
        ):
            return WireGuardResponse(status=resp.status_code, data=None)
        return WireGuardResponse(status=resp.status_code, data=data)


def as_any_list(data: Any, *keys: str) -> list:
    if data is None:
        return []
    if isinstance(data, list):
        return list(data)
    if isinstance(data, dict):
        for key in keys:
            if key in data and isinstance(data[key], list):
                return list(data[key])
        for key in ("items", "data", "results"):
            if key in data and isinstance(data[key], list):
                return list(data[key])
    return []


def as_list(data: Any, *keys: str) -> list[dict]:
    return [x for x in as_any_list(data, *keys) if isinstance(x, dict)]


_pg: Optional[PasarGuardClient] = None
_pg_reseller_cache: dict[tuple[int, str], PasarGuardClient] = {}
# staff cache: (staff_row_id, pg_username) — never username alone (sibling isolation)
_pg_staff_cache: dict[tuple[int, str], PasarGuardClient] = {}
# Level-1 OrgPrincipal cache: (principal_id, pg_username) — never share with Owner
_pg_principal_cache: dict[tuple[int, str], PasarGuardClient] = {}


def _cached_client_ready(client: Any) -> bool:
    """True when a cached client can authorize (API key or Bearer).

    Accepts real ``PasarGuardClient`` and test doubles that only expose ``_token``.
    """
    if client is None:
        return False
    if hasattr(client, "is_auth_ready"):
        return bool(client.is_auth_ready)
    return bool(getattr(client, "_token", None) or getattr(client, "_api_key", None))


def get_pg() -> PasarGuardClient:
    """Platform owner PasarGuard client (env credentials)."""
    global _pg
    if _pg is None:
        _pg = PasarGuardClient()
    return _pg


def reset_pg() -> None:
    """Drop cached clients (after PG_BASE_URL / credentials change)."""
    global _pg, _pg_reseller_cache, _pg_staff_cache, _pg_principal_cache
    for client in (_pg, *_pg_reseller_cache.values(), *_pg_staff_cache.values(), *_pg_principal_cache.values()):
        _retire_client(client)
    _validated_targets.clear()
    _discovered_api_bases.clear()
    _pg = None
    _pg_reseller_cache = {}
    _pg_staff_cache = {}
    _pg_principal_cache = {}
    reset_read_circuits()


def invalidate_pg_principal_cache(principal_id: int | None) -> None:
    """Drop cached PG clients for one OrgPrincipal (e.g. after disable).

    Does not touch Owner env client or sibling Principal caches.
    """
    if principal_id is None:
        return
    try:
        pid = int(principal_id)
    except (TypeError, ValueError):
        return
    if pid <= 0:
        return
    for stale_key in [k for k in _pg_principal_cache if k[0] == pid]:
        _retire_client(_pg_principal_cache.pop(stale_key, None))


def invalidate_pg_reseller_cache(reseller_user_id: int | None) -> None:
    """Drop cached PG clients for one shop (e.g. after password rotate).

    Does not touch Owner env client or other shops.
    """
    if reseller_user_id is None:
        return
    try:
        rid = int(reseller_user_id)
    except (TypeError, ValueError):
        return
    if rid <= 0:
        return
    for stale_key in [k for k in _pg_reseller_cache if k[0] == rid]:
        _retire_client(_pg_reseller_cache.pop(stale_key, None))


def invalidate_pg_staff_cache(
    *,
    staff_id: int | None = None,
    pg_username: str | None = None,
) -> None:
    """Drop cached PG clients for one pg_staff row (e.g. after API key rotate)."""
    if staff_id is not None:
        try:
            sid = int(staff_id)
        except (TypeError, ValueError):
            sid = 0
        if sid > 0:
            for stale_key in [k for k in _pg_staff_cache if k[0] == sid]:
                _retire_client(_pg_staff_cache.pop(stale_key, None))
            return
    uname = (pg_username or "").strip().lower()
    if not uname:
        return
    for stale_key in [k for k in _pg_staff_cache if k[1] == uname]:
        _retire_client(_pg_staff_cache.pop(stale_key, None))


async def _authenticate_and_cache(
    cache: dict[tuple[int, str], PasarGuardClient],
    cache_key: tuple[int, str],
    client: PasarGuardClient,
) -> PasarGuardClient:
    """Log the new client in, then make it the only cached client for its owner row.

    Entries of the same row under another PasarGuard username are dropped so a
    re-linked shop or staff member can never act under the previous identity.
    """
    try:
        await client.ensure_token()
    except BaseException:
        # A client that never authenticated is not shared yet; free its sockets now.
        await _close_unshared_client(client)
        raise
    for stale_key in [key for key in cache if key[0] == cache_key[0] and key != cache_key]:
        _retire_client(cache.pop(stale_key, None))
    previous = cache.get(cache_key)
    if previous is not None and previous is not client:
        _retire_client(previous)
    cache[cache_key] = client
    return client


async def get_pg_for_reseller(session, reseller_user_id: int) -> PasarGuardClient:
    """PasarGuard client authenticated as the shop's PG admin — never owner token.

    Requires ``pg_admin_username`` plus a stored encrypted API key or password.
    Priority: API key (X-Api-Key) → password (Bearer via /api/admin/token).
    """
    from sqlalchemy import select

    from app.db.models import ResellerProfile
    from app.services.secret_box import decrypt_secret

    rid = int(reseller_user_id)

    profile = (
        await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == rid))
    ).scalar_one_or_none()
    if not profile or not profile.pg_admin_username:
        raise PasarGuardError(
            "ادمین پاسارگارد برای این نماینده تعریف نشده — عملیات فروشگاه ممکن نیست"
        )

    # Cache key includes the PG admin username: if an operator re-links this
    # reseller to a different PasarGuard admin, the old cached client (still
    # authenticated as the previous admin) must never be reused — it would
    # silently keep acting under the wrong PasarGuard identity/permissions.
    uname = str(profile.pg_admin_username).strip().lower()
    cache_key = (rid, uname)
    cached = _pg_reseller_cache.get(cache_key)
    if _cached_client_ready(cached):
        return cached

    from app.services.org_principals import get_principal_by_reseller_profile

    linked = await get_principal_by_reseller_profile(session, int(profile.id))
    if (
        linked is not None
        and str(linked.status) == "active"
        and int(getattr(linked, "depth", -1) or -1) in (1, 2)
        and (
            (getattr(linked, "pg_api_key_enc", None) or "").strip()
            or (linked.pg_password_enc or "").strip()
        )
        and (linked.pg_username or "").strip()
    ):
        return await get_pg_for_principal(session, principal_id=int(linked.id))

    api_key = decrypt_secret(getattr(profile, "pg_api_key_enc", None))
    if api_key:
        client = PasarGuardClient(
            username=profile.pg_admin_username,
            api_key=api_key,
        )
        api_key = ""
        return await _authenticate_and_cache(_pg_reseller_cache, cache_key, client)

    password = decrypt_secret(profile.pg_admin_password_enc)
    if not password:
        raise PasarGuardError(
            "اعتبارنامه پاسارگارد نماینده ذخیره نشده (کلید API یا رمز). "
            "علت محتمل: راه‌اندازی ناقص یا کلید رمزنگاری تغییر کرده. "
            "راه حل: ادمین اصلی از بخش نمایندگان کلید API یا رمز را ثبت/بازنشانی کند."
        )
    client = PasarGuardClient(
        username=profile.pg_admin_username,
        password=password,
    )
    password = ""
    return await _authenticate_and_cache(_pg_reseller_cache, cache_key, client)


async def get_pg_for_staff(
    session, *, pg_username: str | None = None, staff_id: int | None = None
) -> PasarGuardClient:
    """PasarGuard client for a pg_staff row — never owner token (Phase C5 / 1F).

    Requires encrypted API key or password on ``PgStaffAccess``.
    Priority: API key → password.
    Cache key is ``(staff_id, username)`` so sibling principals never share a
    client even if usernames collide after re-grant.
    """
    from sqlalchemy import select

    from app.db.models import PgStaffAccess
    from app.services.secret_box import decrypt_secret

    row: PgStaffAccess | None = None
    if staff_id is not None:
        row = await session.get(PgStaffAccess, int(staff_id))
    elif pg_username:
        from sqlalchemy import func

        uname = str(pg_username).strip().lower()
        row = (
            await session.execute(
                select(PgStaffAccess).where(
                    func.lower(PgStaffAccess.pg_username) == uname
                )
            )
        ).scalar_one_or_none()
    if not row or not row.is_active:
        raise PasarGuardError(
            "دسترسی ادمین پاسارگارد فعال نیست. "
            "علت محتمل: حساب غیرفعال شده یا ردیف دسترسی حذف شده. "
            "راه حل: ادمین اصلی از «ادمین‌ها» وضعیت را بررسی و در صورت نیاز دوباره اعطا کند."
        )
    uname = (row.pg_username or "").strip().lower()
    sid = int(row.id)
    cache_key = (sid, uname)
    cached = _pg_staff_cache.get(cache_key)
    if _cached_client_ready(cached):
        return cached
    api_key = decrypt_secret(getattr(row, "pg_api_key_enc", None))
    if api_key:
        client = PasarGuardClient(username=row.pg_username, api_key=api_key)
        api_key = ""
        return await _authenticate_and_cache(_pg_staff_cache, cache_key, client)
    password = decrypt_secret(row.pg_admin_password_enc)
    if not password:
        raise PasarGuardError(
            "اعتبارنامه پاسارگارد برای این حساب ذخیره نشده (کلید API یا رمز). "
            "علت محتمل: اعطای ناقص یا کلید رمزنگاری تغییر کرده. "
            "راه حل: ادمین اصلی از «ادمین‌ها» کلید API یا رمز ادمین فرعی را ثبت کند."
        )
    client = PasarGuardClient(username=row.pg_username, password=password)
    password = ""
    return await _authenticate_and_cache(_pg_staff_cache, cache_key, client)


async def get_pg_for_principal(
    session,
    *,
    principal_id: int | None = None,
    pg_username: str | None = None,
) -> PasarGuardClient:
    """PasarGuard client for a Level-1 or Level-2 OrgPrincipal — never Owner env token.

    Requires ``OrgPrincipal.pg_username`` plus ``pg_api_key_enc`` or ``pg_password_enc``
    on *this* row. Priority: API key → password.
    Selector is ``principal_id`` only. ``pg_username`` is ignored and must not
    choose a sibling/parent row.

    Cache key is ``(principal_id, username)`` so parent/siblings never share clients.

    Level-2 uses its own stored credential only. Missing identity, inactive parent,
    or decrypt failure → fail closed. Never falls back to Owner env credentials,
    parent Level-1 credentials, or sibling caches.
    """
    from app.db.models import OrgPrincipal
    from app.services.secret_box import decrypt_secret

    _ = pg_username  # not a selector — principal_id is required
    row: OrgPrincipal | None = None
    if principal_id is not None:
        try:
            pid = int(principal_id)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0:
            row = await session.get(OrgPrincipal, pid)
    if row is None or str(row.status) != "active":
        raise PasarGuardError(
            "Principal فعال با هویت پاسارگارد یافت نشد. "
            "علت محتمل: Principal حذف/غیرفعال شده. "
            "راه حل: با ادمین اصلی تماس بگیرید."
        )
    try:
        depth = int(row.depth)
    except (TypeError, ValueError):
        depth = -1
    if depth not in (1, 2):
        raise PasarGuardError(
            "فقط Principal سطح ۱ یا ۲ می‌تواند کلاینت اختصاصی پاسارگارد داشته باشد"
        )
    if depth == 2:
        parent_id = getattr(row, "parent_id", None)
        if parent_id is None:
            raise PasarGuardError(
                "والد Principal سطح ۲ نامعتبر است — دسترسی رد شد"
            )
        parent = await session.get(OrgPrincipal, int(parent_id))
        if (
            parent is None
            or str(parent.status) != "active"
            or int(getattr(parent, "depth", -1) or -1) != 1
        ):
            raise PasarGuardError(
                "والد Principal سطح ۲ غیرفعال است — دسترسی رد شد"
            )
    uname = (row.pg_username or "").strip()
    if not uname:
        raise PasarGuardError(
            "هویت پاسارگارد برای این Principal تعریف نشده — دسترسی رد شد"
        )
    pid = int(row.id)
    cache_key = (pid, uname.lower())
    cached = _pg_principal_cache.get(cache_key)
    if _cached_client_ready(cached):
        return cached
    api_key = decrypt_secret(getattr(row, "pg_api_key_enc", None))
    if api_key:
        client = PasarGuardClient(username=uname, api_key=api_key)
        api_key = ""
        return await _authenticate_and_cache(_pg_principal_cache, cache_key, client)
    password = decrypt_secret(row.pg_password_enc)
    if not password:
        raise PasarGuardError(
            "اعتبارنامه پاسارگارد Principal ذخیره نشده (کلید API یا رمز). "
            "علت محتمل: provisioning ناقص یا کلید رمزنگاری تغییر کرده. "
            "راه حل: ادمین اصلی کلید API یا رمز Principal را ثبت کند."
        )
    client = PasarGuardClient(username=uname, password=password)
    # Local plaintext reference must not outlive token acquisition.
    password = ""
    return await _authenticate_and_cache(_pg_principal_cache, cache_key, client)


def public_pg_api_base() -> str:
    """Best-known API root (may include a dashboard path). Prefer ``public_pg_sub_origin`` for /sub links."""
    client = _pg
    if client and client.base_url:
        return client.base_url.rstrip("/")
    cands = pg_api_base_candidates(get_settings().pg_base_url or "")
    return (cands[0] if cands else "").rstrip("/")


def public_pg_sub_origin() -> str:
    """Scheme + host (+ port) of the PasarGuard panel — no dashboard/UI path.

    When PG has no dedicated subscription host configured it often returns a
    path-only ``/sub/…`` URL. Public links must still be absolute; use this
    origin (not the panel path) as the fallback base.
    """
    from urllib.parse import urlparse, urlunparse

    from app.config import normalize_pg_base_url

    raw = ""
    client = _pg
    if client and getattr(client, "base_url", None):
        raw = str(client.base_url or "")
    if not raw:
        raw = get_settings().pg_base_url or ""
    base = normalize_pg_base_url(raw)
    if not base:
        return ""
    parsed = urlparse(base)
    if not parsed.scheme or not parsed.netloc:
        return base.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _subscription_link_parts(subscription_url: str | None) -> tuple[str, str] | None:
    from urllib.parse import unquote, urlsplit

    if not subscription_url:
        return None
    raw = subscription_url.strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in {"", "http", "https"} or (not parsed.scheme and not raw.startswith("/")):
        return None
    path = parsed.path.rstrip("/")
    if path.rsplit("/", 1)[-1] in {"info", "usage", "raw", "apps"}:
        path = path.rsplit("/", 1)[0]
    prefix, _, raw_token = path.rpartition("/")
    if not prefix or not raw_token:
        return None
    try:
        prefix = normalize_pg_subscription_path(prefix)
    except ValueError:
        return None
    token = unquote(raw_token)
    if not token or any(char.isspace() or char in "/?#\\" for char in token):
        return None
    return prefix, token


def subscription_path_from_url(subscription_url: str | None) -> str | None:
    parts = _subscription_link_parts(subscription_url)
    return parts[0] if parts else None


def extract_sub_token(subscription_url: str | None) -> str | None:
    """Read a service token from default or custom subscription paths."""
    parts = _subscription_link_parts(subscription_url)
    return parts[1] if parts else None


def _sanitize_subscription_url(raw: str) -> str | None:
    """Reject executable browser schemes; allow http(s) and common VPN URI schemes."""
    url = (raw or "").strip()
    if not url:
        return None
    low = url.lower()
    if low.startswith(("javascript:", "data:", "vbscript:", "file:")):
        return None
    # Relative /sub/ paths and absolute http(s) / known schemes
    if low.startswith(("/", "http://", "https://", "vless://", "vmess://", "trojan://", "ss://", "ssr://", "hy2://", "hysteria2://", "tuic://", "wireguard://")):
        return url
    # Bare host-ish tokens are rejected
    if "://" in low:
        return None
    return url


def absolutize_subscription_url(raw: str | None) -> str | None:
    """Sanitize and turn path-only ``/sub/…`` into an absolute URL via PG panel origin."""
    cleaned = _sanitize_subscription_url(raw or "")
    if not cleaned:
        return None
    if cleaned.startswith("/"):
        origin = public_pg_sub_origin()
        if not origin:
            # Incomplete without a host — do not hand out path-only links
            return None
        return f"{origin}{cleaned}"
    return cleaned


def user_subscription_url(user: dict | None) -> str | None:
    """Best-effort absolute subscription URL from a PG user payload."""
    if not isinstance(user, dict):
        return None
    for key in (
        "subscription_url",
        "subscription",
        "sub_url",
        "link",
        "subscribe_url",
        "sub_link",
    ):
        raw = user.get(key)
        if isinstance(raw, str) and raw.strip():
            cleaned = absolutize_subscription_url(raw)
            if cleaned:
                return cleaned
        if isinstance(raw, dict):
            for k in ("url", "subscription_url", "link", "href"):
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    cleaned = absolutize_subscription_url(v)
                    if cleaned:
                        return cleaned
    links = user.get("links")
    if isinstance(links, dict):
        for k in ("subscription", "subscription_url", "url", "sub"):
            v = links.get(k)
            if isinstance(v, str) and v.strip():
                cleaned = absolutize_subscription_url(v)
                if cleaned:
                    return cleaned
    if isinstance(links, list):
        for item in links:
            if isinstance(item, str) and (extract_sub_token(item) or item.startswith("http")):
                cleaned = absolutize_subscription_url(item)
                if cleaned:
                    return cleaned
            if isinstance(item, dict):
                for k in ("url", "link", "href"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        cleaned = absolutize_subscription_url(v)
                        if cleaned:
                            return cleaned
    token = (
        user.get("subscription_token")
        or user.get("token")
        or user.get("sub_id")
        or user.get("subscription_id")
    )
    if isinstance(token, str) and token.strip():
        origin = public_pg_sub_origin()
        if origin:
            return f"{origin}{get_settings().pg_subscription_path}/{token.strip()}"
    return None


def user_group_ids(user: dict | None) -> list[int]:
    if not isinstance(user, dict):
        return []
    raw = user.get("group_ids")
    if isinstance(raw, list):
        out = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    groups = user.get("groups")
    if isinstance(groups, list):
        out = []
        for g in groups:
            if isinstance(g, dict) and g.get("id") is not None:
                try:
                    out.append(int(g["id"]))
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    out.append(int(g))
                except (TypeError, ValueError):
                    continue
        return out
    return []


def parse_group_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    out: list[int] = []
    for part in str(raw).replace(" ", "").split(","):
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out
