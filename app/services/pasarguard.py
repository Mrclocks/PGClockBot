from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.config import get_settings, pg_api_base_candidates

logger = logging.getLogger(__name__)


class PasarGuardError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body

    def user_message(self, *, fallback: str | None = None) -> str:
        """Short Persian-friendly message extracted from API error body."""
        detail = _pg_error_detail(self.body)
        if detail:
            return detail[:400]
        base = str(self.args[0] if self.args else "") or (fallback or "خطای پاسارگارد")
        if self.status_code:
            return f"{base}"
        return base


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
    if note:
        payload["note"] = note
    return payload


def build_user_modify_payload(
    *,
    username: str | None = None,
    group_ids: list[int] | None = None,
    data_limit: int | None = None,
    expire_ts: int | None = None,
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
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _rebind_base(self, base: str) -> None:
        base = (base or "").rstrip("/")
        if not base or base == self.base_url:
            return
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
            follow_redirects=True,
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
                    base_url=base, timeout=15.0, follow_redirects=True
                ) as probe:
                    # Live API: openapi 200, or /api/system 401 without token
                    r = await probe.get("/openapi.json")
                    if r.status_code == 200 and "PasarGuard" in (r.text or ""):
                        await self._rebind_base(base)
                        break
                    r2 = await probe.get("/api/system")
                    if r2.status_code in (200, 401, 403):
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

    async def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = True,
        **kwargs: Any,
    ) -> Any:
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
            return resp.json()
        return resp.text

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
        """Authenticated admin's own profile — works for limited (non-sudo) admins.

        ``GET /api/admins`` is sudo-only; limited staff must use ``GET /api/admin``.
        """
        try:
            data = await self.request("GET", "/api/admin")
            return data if isinstance(data, dict) and data else None
        except Exception:
            return None

    async def get_admin(self, username: str) -> dict | None:
        """Fetch one admin (with usage metrics) by username.

        When this client is logged in as ``username``, prefer ``/api/admin`` so
        limited resellers/pg_staff never depend on the sudo-only admins list.
        """
        username = (username or "").strip()
        if not username:
            return None
        login = (self._login_username or "").strip()
        if login and login.lower() == username.lower():
            cur = await self.get_current_admin()
            if cur and str(cur.get("username") or "").lower() == username.lower():
                return cur
            if cur:
                return cur
        try:
            data = await self.request("GET", "/api/admins", params={"username": username, "limit": 20})
            admins = as_list(data, "admins")
            for a in admins:
                if str(a.get("username") or "").lower() == username.lower():
                    return a
            if len(admins) == 1:
                return admins[0]
        except Exception:
            pass
        try:
            for a in await self.get_admins():
                if str(a.get("username") or "").lower() == username.lower():
                    return a
        except Exception:
            pass
        # Last resort: if we are that admin, current profile still helps.
        if login and login.lower() == username.lower():
            return await self.get_current_admin()
        return None

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
        return await self.request("PUT", f"/api/admin/{username}", json=payload)

    async def delete_admin(self, username: str) -> Any:
        return await self.request("DELETE", f"/api/admin/{username}")

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

    async def reconnect_node(self, node_id: int) -> Any:
        return await self.request("POST", f"/api/node/{node_id}/reconnect")

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
_pg_reseller_cache: dict[int, PasarGuardClient] = {}
_pg_reseller_cache_ts: dict[int, float] = {}
_pg_staff_cache: dict[str, PasarGuardClient] = {}
_pg_staff_cache_ts: dict[str, float] = {}
_RESELLER_CLIENT_TTL_SEC = 3600.0
_STAFF_CLIENT_TTL_SEC = 3600.0


def get_pg() -> PasarGuardClient:
    """Platform owner PasarGuard client (env credentials)."""
    global _pg
    if _pg is None:
        _pg = PasarGuardClient()
    return _pg


def reset_pg() -> None:
    """Drop cached clients (after PG_BASE_URL / credentials change)."""
    global _pg, _pg_reseller_cache, _pg_reseller_cache_ts, _pg_staff_cache, _pg_staff_cache_ts
    _pg = None
    _pg_reseller_cache = {}
    _pg_reseller_cache_ts = {}
    _pg_staff_cache = {}
    _pg_staff_cache_ts = {}


def invalidate_reseller_pg_client(reseller_user_id: int | None = None) -> None:
    """Drop one (or all) cached reseller PG clients after password/credential rotation."""
    global _pg_reseller_cache, _pg_reseller_cache_ts
    if reseller_user_id is None:
        _pg_reseller_cache = {}
        _pg_reseller_cache_ts = {}
        return
    rid = int(reseller_user_id)
    _pg_reseller_cache.pop(rid, None)
    _pg_reseller_cache_ts.pop(rid, None)


def invalidate_staff_pg_client(pg_username: str | None = None) -> None:
    """Drop one (or all) cached pg_staff PG clients after password/credential rotation."""
    global _pg_staff_cache, _pg_staff_cache_ts
    if pg_username is None:
        _pg_staff_cache = {}
        _pg_staff_cache_ts = {}
        return
    key = str(pg_username or "").strip().lower()
    if key:
        _pg_staff_cache.pop(key, None)
        _pg_staff_cache_ts.pop(key, None)


async def get_pg_for_reseller(session, reseller_user_id: int) -> PasarGuardClient:
    """PasarGuard client authenticated as the shop's PG admin — never owner token.

    Requires ``pg_admin_username`` + stored encrypted password on the profile.
    """
    import time

    from sqlalchemy import select

    from app.db.models import ResellerProfile
    from app.services.secret_box import decrypt_secret

    rid = int(reseller_user_id)
    cached = _pg_reseller_cache.get(rid)
    ts = _pg_reseller_cache_ts.get(rid, 0.0)
    if (
        cached is not None
        and cached._token
        and (time.monotonic() - ts) < _RESELLER_CLIENT_TTL_SEC
    ):
        return cached

    profile = (
        await session.execute(select(ResellerProfile).where(ResellerProfile.user_id == rid))
    ).scalar_one_or_none()
    if not profile or not profile.pg_admin_username:
        raise PasarGuardError(
            "ادمین پاسارگارد برای این نماینده تعریف نشده — عملیات فروشگاه ممکن نیست"
        )
    password = decrypt_secret(profile.pg_admin_password_enc)
    if not password:
        raise PasarGuardError(
            "رمز پاسارگارد نماینده ذخیره نشده — نمایندگی را دوباره provision کنید "
            "یا رمز را از پنل ادمین بازنشانی کنید"
        )
    client = PasarGuardClient(
        username=profile.pg_admin_username,
        password=password,
    )
    await client.ensure_token()
    _pg_reseller_cache[rid] = client
    _pg_reseller_cache_ts[rid] = time.monotonic()
    return client


async def get_pg_for_staff_admin(session, pg_username: str) -> PasarGuardClient:
    """PasarGuard client authenticated as a linked ``pg_staff`` admin — never owner."""
    import time

    from app.services.pg_staff_access import access_by_pg_username
    from app.services.secret_box import decrypt_secret

    uname = str(pg_username or "").strip()
    if not uname:
        raise PasarGuardError("ادمین پاسارگارد برای این حساب تنظیم نشده است")
    key = uname.lower()
    cached = _pg_staff_cache.get(key)
    ts = _pg_staff_cache_ts.get(key, 0.0)
    if (
        cached is not None
        and cached._token
        and (time.monotonic() - ts) < _STAFF_CLIENT_TTL_SEC
    ):
        return cached

    row = await access_by_pg_username(session, uname)
    if not row or not row.is_active:
        raise PasarGuardError("دسترسی وب ادمین پاسارگارد فعال نیست")
    password = decrypt_secret(row.pg_password_enc)
    if not password:
        raise PasarGuardError(
            "رمز پاسارگارد این ادمین ذخیره نشده — دسترسی وب را دوباره با رمز تنظیم کنید"
        )
    client = PasarGuardClient(username=str(row.pg_username).strip(), password=password)
    await client.ensure_token()
    _pg_staff_cache[key] = client
    _pg_staff_cache_ts[key] = time.monotonic()
    return client


async def get_pg_for_staff(session, staff: dict) -> tuple[PasarGuardClient, bool]:
    """Return ``(client, as_owner)`` for the authenticated web/bot staff member.

    - Platform admin → owner client (``as_owner=True``)
    - Reseller with shop scope → shop PG admin client (never owner)
    - ``pg_staff`` → linked PG admin client from stored credentials (never owner)
    """
    from app.services.shop_scope import is_platform_admin, shop_owner_id

    if is_platform_admin(staff):
        return get_pg(), True
    rid = shop_owner_id(staff)
    if rid:
        return await get_pg_for_reseller(session, int(rid)), False
    if staff.get("role") == "pg_staff":
        uname = str(staff.get("pg_admin_username") or "").strip()
        if not uname:
            raise PasarGuardError("ادمین پاسارگارد برای این حساب تنظیم نشده است")
        return await get_pg_for_staff_admin(session, uname), False
    raise PasarGuardError("محدوده فروشگاه مشخص نیست — عملیات پاسارگارد مجاز نیست")


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
