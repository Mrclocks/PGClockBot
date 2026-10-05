from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings, pg_api_base_candidates
from app.services.security_policy import UnsafePgUrlError, assert_safe_pg_base_url

logger = logging.getLogger(__name__)


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


def is_pg_permission_denied(exc: Exception) -> bool:
    """True when PasarGuard refused a resource (not a transport outage).

    Limited roles often 403 on ``/api/admins``, hosts, nodes, or templates
    even after a valid token — callers must not paint that as «قطع اتصال».
    """
    return isinstance(exc, PasarGuardError) and exc.status_code in (403, 404, 405)


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
                    loc = ".".join(str(x) for x in (item.get("loc") or []) if x != "body")
                    msg = item.get("msg") or item.get("message") or ""
                    parts.append(f"{loc}: {msg}".strip(": "))
                else:
                    parts.append(str(item))
            joined = "؛ ".join(p for p in parts if p)
            if joined:
                return joined[:400]
        if isinstance(val, dict):
            inner = _pg_error_detail(val)
            if inner:
                return inner
    return None


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
    ) -> None:
        self.settings = get_settings()
        # Use settings value as-is (already normalized with path preserved)
        self.base_url = (self.settings.pg_base_url or "").rstrip("/")
        if self.base_url:
            try:
                self.base_url = assert_safe_pg_base_url(self.base_url)
            except UnsafePgUrlError:
                # Keep empty so ensure_token fails closed instead of dialing SSRF targets
                logger.error("Refusing unsafe PG_BASE_URL=%s", self.settings.pg_base_url)
                self.base_url = ""
        # Optional per-admin credentials (reseller shop) — never fall back to owner silently
        self._login_username = (username or "").strip() or None
        self._login_password = (password or "").replace("\r", "").strip() or None
        if access_token is not None:
            self._token: str | None = access_token or None
        elif self._login_username:
            self._token = None
        else:
            self._token = (self.settings.pg_access_token or None) or None
        if self._token == "":
            self._token = None
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=30.0,
            # Never follow redirects with Bearer tokens (SSRF / credential forwarding)
            follow_redirects=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _rebind_base(self, base: str) -> None:
        base = (base or "").rstrip("/")
        if not base or base == self.base_url:
            return
        try:
            base = assert_safe_pg_base_url(base)
        except UnsafePgUrlError as exc:
            raise PasarGuardError(str(exc)) from exc
        logger.warning(
            "PasarGuard API root adjusted %s → %s (path was dashboard UI, not API)",
            self.base_url,
            base,
        )
        old = self._client
        self.base_url = base
        self._client = httpx.AsyncClient(
            base_url=base,
            timeout=30.0,
            follow_redirects=False,
        )
        try:
            await old.aclose()
        except Exception:
            pass

    async def _ensure_api_base(self) -> None:
        """If PG_BASE_URL includes a dashboard path, switch to the real API root."""
        if getattr(self, "_api_base_resolved", False):
            return
        candidates = pg_api_base_candidates(self.settings.pg_base_url or self.base_url)
        if len(candidates) <= 1:
            self._api_base_resolved = True
            return
        for base in candidates:
            try:
                async with httpx.AsyncClient(
                    base_url=base, timeout=15.0, follow_redirects=False
                ) as probe:
                    # Only accept a candidate that identifies as PasarGuard.
                    # Do NOT treat bare /api/system 401/403 as proof — that
                    # pattern matches many unrelated internal services (SSRF).
                    r = await probe.get("/openapi.json")
                    if r.status_code == 200 and "PasarGuard" in (r.text or ""):
                        await self._rebind_base(base)
                        break
            except Exception:
                continue
        self._api_base_resolved = True

    async def ensure_token(self) -> str:
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
            # Never follow redirects on credential POSTs (prevents auth-forwarding SSRF)
            async with httpx.AsyncClient(
                base_url=base, timeout=30.0, follow_redirects=False
            ) as probe:
                for data in attempts:
                    last = await probe.post("/api/admin/token", data=data)
                    if last.status_code < 400:
                        await self._rebind_base(base)
                        self._api_base_resolved = True
                        payload = last.json()
                        token = payload.get("access_token")
                        if not token:
                            raise PasarGuardError(
                                "PasarGuard login response missing access_token",
                                body=payload,
                            )
                        self._token = token
                        # Drop plaintext password from memory once a Bearer token exists
                        # (cached clients must not retain PG passwords).
                        self._login_password = None
                        logger.info("PasarGuard login OK · base=%s", self.base_url)
                        return self._token
                    if last.status_code in (401, 403):
                        detail = (last.text or "")[:300]
                        from app.services.redact import redact

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

    async def _headers(self) -> dict[str, str]:
        token = await self.ensure_token()
        return {"Authorization": f"Bearer {token}"}

    def _read_cache_ident(self) -> str:
        """Cache partition: never share Owner reads with an L1/L2 client."""
        user = (getattr(self, "_login_username", None) or "").strip()
        base = (self.base_url or "").rstrip("/")
        if user:
            return f"u:{user}@{base}"
        return f"owner@{base}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        **kwargs: Any,
    ) -> Any:
        from app.services.pg_read_cache import cache_get, cache_put, invalidate_ident

        method_u = (method or "GET").upper()
        ident = self._read_cache_ident()
        if method_u != "GET":
            invalidate_ident(ident)
        else:
            cached = cache_get(ident, path, kwargs.get("params"))
            if cached is not None:
                return cached
        headers = kwargs.pop("headers", {})
        if auth:
            headers.update(await self._headers())
        else:
            await self._ensure_api_base()
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401 and auth:
            self._token = None
            headers.update(await self._headers())
            resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code >= 400:
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
        if method_u == "GET":
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
                return data
            inner = data.get("admin")
            if isinstance(inner, dict) and (
                inner.get("username") or inner.get("id") is not None or inner.get("role")
            ):
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
        """
        username = (username or "").strip()
        if not username:
            return "missing", None
        from urllib.parse import quote

        enc = quote(username, safe="")
        saw_transport_error = False

        def _not_found(exc: Exception) -> bool:
            return isinstance(exc, PasarGuardError) and exc.status_code == 404

        def _username_matches(payload: dict | None) -> bool:
            if not isinstance(payload, dict):
                return False
            got = str(payload.get("username") or "").strip()
            return bool(got) and got.lower() == username.lower()

        # Own account first: no admins.read required. Never treat the token
        # holder's record as a *different* admin being looked up.
        try:
            current = await self.get_current_admin()
            if _username_matches(current):
                return "ok", current
        except Exception as exc:
            # 403/404/405: endpoint hidden or forbidden on this role/version.
            # Keep looking; do not flip a later confirmed-404 into unreachable.
            status = getattr(exc, "status_code", None)
            if not _not_found(exc) and status not in (403, 405):
                saw_transport_error = True

        for path in (
            f"/api/admin/by-username/{enc}",
            f"/api/admin/{enc}",
        ):
            try:
                data = await self.request("GET", path)
                if isinstance(data, dict) and (
                    str(data.get("username") or "").strip().lower() == username.lower()
                    or data.get("id") is not None
                ):
                    # Strict identity when username is present
                    got = str(data.get("username") or "").strip()
                    if got and got.lower() != username.lower():
                        continue
                    return "ok", data
            except Exception as exc:
                if not _not_found(exc):
                    saw_transport_error = True
        try:
            data = await self.request("GET", "/api/admins", params={"username": username, "limit": 20})
            admins = as_list(data, "admins")
            for a in admins:
                if str(a.get("username") or "").lower() == username.lower():
                    return "ok", a
            if len(admins) == 1:
                return "ok", admins[0]
        except Exception as exc:
            if not _not_found(exc):
                saw_transport_error = True
        try:
            for a in await self.get_admins():
                if str(a.get("username") or "").lower() == username.lower():
                    return "ok", a
        except Exception as exc:
            if not _not_found(exc):
                saw_transport_error = True
        return ("unreachable" if saw_transport_error else "missing"), None

    async def get_admin_roles(self) -> list[dict]:
        """Pasarguard admin roles (owner-defined access levels)."""
        try:
            data = await self.request("GET", "/api/admin-roles/simple")
            roles = as_list(data, "roles", "items")
            if roles:
                return roles
        except Exception:
            pass
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

    async def subscription_info(self, token: str) -> dict:
        return await self.request("GET", f"/sub/{token}/info", auth=False)

    async def subscription_usage(self, token: str) -> Any:
        return await self.request("GET", f"/sub/{token}/usage", auth=False)


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


def get_pg() -> PasarGuardClient:
    """Platform owner PasarGuard client (env credentials)."""
    global _pg
    if _pg is None:
        _pg = PasarGuardClient()
    return _pg


def reset_pg() -> None:
    """Drop cached clients (after PG_BASE_URL / credentials change)."""
    global _pg, _pg_reseller_cache, _pg_staff_cache, _pg_principal_cache
    _pg = None
    _pg_reseller_cache = {}
    _pg_staff_cache = {}
    _pg_principal_cache = {}


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
        _pg_principal_cache.pop(stale_key, None)


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
        _pg_reseller_cache.pop(stale_key, None)


async def get_pg_for_reseller(session, reseller_user_id: int) -> PasarGuardClient:
    """PasarGuard client authenticated as the shop's PG admin — never owner token.

    Requires ``pg_admin_username`` + stored encrypted password on the profile.
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
    if cached is not None and cached._token:
        return cached

    from app.services.org_principals import get_principal_by_reseller_profile

    linked = await get_principal_by_reseller_profile(session, int(profile.id))
    if (
        linked is not None
        and str(linked.status) == "active"
        and int(getattr(linked, "depth", -1) or -1) in (1, 2)
        and (linked.pg_password_enc or "").strip()
        and (linked.pg_username or "").strip()
    ):
        return await get_pg_for_principal(session, principal_id=int(linked.id))

    password = decrypt_secret(profile.pg_admin_password_enc)
    if not password:
        raise PasarGuardError(
            "رمز پاسارگارد نماینده ذخیره نشده. "
            "علت محتمل: راه‌اندازی ناقص یا کلید رمزنگاری تغییر کرده. "
            "راه حل: ادمین اصلی از بخش نمایندگان رمز را بازنشانی کند یا دسترسی را دوباره تنظیم کند."
        )
    client = PasarGuardClient(
        username=profile.pg_admin_username,
        password=password,
    )
    await client.ensure_token()
    # Drop any stale entry for this reseller under a different (old) admin
    # username so it can never be resurrected/reused.
    for stale_key in [k for k in _pg_reseller_cache if k[0] == rid and k != cache_key]:
        _pg_reseller_cache.pop(stale_key, None)
    _pg_reseller_cache[cache_key] = client
    return client


async def get_pg_for_staff(
    session, *, pg_username: str | None = None, staff_id: int | None = None
) -> PasarGuardClient:
    """PasarGuard client for a pg_staff row — never owner token (Phase C5 / 1F).

    Requires ``PgStaffAccess.pg_admin_password_enc``.
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
    if cached is not None and cached._token:
        return cached
    password = decrypt_secret(row.pg_admin_password_enc)
    if not password:
        raise PasarGuardError(
            "رمز پاسارگارد برای این حساب ذخیره نشده. "
            "علت محتمل: اعطای ناقص یا کلید رمزنگاری تغییر کرده. "
            "راه حل: ادمین اصلی از «ادمین‌ها» دسترسی ادمین فرعی را با رمز جدید ویرایش کند."
        )
    client = PasarGuardClient(username=row.pg_username, password=password)
    await client.ensure_token()
    # Drop stale keys for this staff row (username rename) and legacy
    # username-only keys if any remain from older builds.
    for stale_key in [k for k in _pg_staff_cache if k[0] == sid and k != cache_key]:
        _pg_staff_cache.pop(stale_key, None)
    _pg_staff_cache[cache_key] = client
    return client


async def get_pg_for_principal(
    session,
    *,
    principal_id: int | None = None,
    pg_username: str | None = None,
) -> PasarGuardClient:
    """PasarGuard client for a Level-1 or Level-2 OrgPrincipal — never Owner env token.

    Requires ``OrgPrincipal.pg_username`` + ``pg_password_enc`` on *this* row.
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
    if cached is not None and cached._token:
        return cached
    password = decrypt_secret(row.pg_password_enc)
    if not password:
        raise PasarGuardError(
            "رمز پاسارگارد Principal ذخیره نشده. "
            "علت محتمل: provisioning ناقص یا کلید رمزنگاری تغییر کرده. "
            "راه حل: ادمین اصلی Principal را دوباره provision کند."
        )
    client = PasarGuardClient(username=uname, password=password)
    # Local plaintext reference must not outlive token acquisition.
    password = ""
    await client.ensure_token()
    for stale_key in [k for k in _pg_principal_cache if k[0] == pid and k != cache_key]:
        _pg_principal_cache.pop(stale_key, None)
    _pg_principal_cache[cache_key] = client
    return client


def public_pg_api_base() -> str:
    """Best-known API root for building public /sub links."""
    client = _pg
    if client and client.base_url:
        return client.base_url.rstrip("/")
    cands = pg_api_base_candidates(get_settings().pg_base_url or "")
    return (cands[0] if cands else "").rstrip("/")


def extract_sub_token(subscription_url: str | None) -> str | None:
    if not subscription_url:
        return None
    url = subscription_url.rstrip("/")
    parts = url.split("/sub/")
    if len(parts) < 2:
        return None
    token = parts[-1].split("?")[0].strip("/")
    return token or None


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


def user_subscription_url(user: dict | None) -> str | None:
    """Best-effort subscription URL from a PG user payload."""
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
            cleaned = _sanitize_subscription_url(raw)
            if cleaned:
                return cleaned
        if isinstance(raw, dict):
            for k in ("url", "subscription_url", "link", "href"):
                v = raw.get(k)
                if isinstance(v, str) and v.strip():
                    cleaned = _sanitize_subscription_url(v)
                    if cleaned:
                        return cleaned
    links = user.get("links")
    if isinstance(links, dict):
        for k in ("subscription", "subscription_url", "url", "sub"):
            v = links.get(k)
            if isinstance(v, str) and v.strip():
                cleaned = _sanitize_subscription_url(v)
                if cleaned:
                    return cleaned
    if isinstance(links, list):
        for item in links:
            if isinstance(item, str) and ("/sub/" in item or item.startswith("http")):
                cleaned = _sanitize_subscription_url(item)
                if cleaned:
                    return cleaned
            if isinstance(item, dict):
                for k in ("url", "link", "href"):
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        cleaned = _sanitize_subscription_url(v)
                        if cleaned:
                            return cleaned
    token = (
        user.get("subscription_token")
        or user.get("token")
        or user.get("sub_id")
        or user.get("subscription_id")
    )
    if isinstance(token, str) and token.strip():
        base = public_pg_api_base()
        if base:
            return f"{base}/sub/{token.strip()}"
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
