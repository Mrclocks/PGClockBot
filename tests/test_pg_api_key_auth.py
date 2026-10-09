"""PasarGuard admin API key auth (X-Api-Key) with password fallback."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import Base
import app.db.models  # noqa: F401
from app.db.models import BotUser, OrgPrincipal, PgStaffAccess, ResellerProfile, Role
from app.services.org_principals import create_principal, ensure_owner_principal
from app.services.pasarguard import (
    PasarGuardClient,
    PasarGuardError,
    get_pg_for_principal,
    get_pg_for_reseller,
    get_pg_for_staff,
    invalidate_pg_principal_cache,
    invalidate_pg_reseller_cache,
    invalidate_pg_staff_cache,
    _cached_client_ready,
)
from app.services.pg_staff_access import (
    staff_has_pg_auth_secret,
    staff_has_stored_pg_api_key,
    staff_has_stored_pg_password,
    staff_remediation_flags,
    store_staff_pg_api_key,
)
from app.services.principal_web_identity import (
    PrincipalWebIdentityError,
    _assert_pg_identity_ready,
    session_contains_plaintext_secret,
)
from app.services.secret_box import decrypt_secret, encrypt_secret


_API_KEY = "pg_key_aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PASSWORD = "SecretA12!@xx"


class PasarGuardClientApiKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_headers_use_x_api_key_not_bearer(self) -> None:
        client = PasarGuardClient(username="shop_a", api_key=_API_KEY)
        self.assertTrue(client.uses_api_key)
        self.assertTrue(client.is_auth_ready)
        self.assertIsNone(client._login_password)
        with patch.object(client, "_ensure_api_base", new=AsyncMock()):
            headers = await client._headers()
        self.assertEqual(headers, {"X-Api-Key": _API_KEY})
        self.assertNotIn("Authorization", headers)

    async def test_api_key_ignores_password_arg(self) -> None:
        client = PasarGuardClient(
            username="shop_a",
            password=_PASSWORD,
            api_key=_API_KEY,
        )
        self.assertIsNone(client._login_password)
        self.assertEqual(client._api_key, _API_KEY)

    async def test_ensure_token_skips_login_for_api_key(self) -> None:
        client = PasarGuardClient(username="shop_a", api_key=_API_KEY)
        with patch.object(client, "_ensure_api_base", new=AsyncMock()) as ensure:
            with patch("httpx.AsyncClient") as http_cls:
                token = await client.ensure_token()
        self.assertEqual(token, "__apikey__")
        ensure.assert_awaited()
        http_cls.assert_not_called()

    async def test_401_does_not_retry_when_using_api_key(self) -> None:
        client = PasarGuardClient(username="shop_a", api_key=_API_KEY)
        resp = MagicMock()
        resp.status_code = 401
        resp.text = "unauthorized"
        resp.content = b"unauthorized"
        resp.headers = {"content-type": "text/plain"}
        client._client = MagicMock()
        client._client.request = AsyncMock(return_value=resp)
        with patch.object(client, "_ensure_api_base", new=AsyncMock()):
            with self.assertRaises(PasarGuardError) as ctx:
                await client.request("GET", "/api/admin")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(client._client.request.await_count, 1)

    async def test_cached_client_ready_for_api_key_without_token(self) -> None:
        client = PasarGuardClient(username="shop_a", api_key=_API_KEY)
        self.assertTrue(_cached_client_ready(client))
        self.assertFalse(bool(client._token))
        # Legacy double that only exposes _token
        self.assertTrue(_cached_client_ready(SimpleNamespace(_token="jwt")))
        self.assertFalse(_cached_client_ready(SimpleNamespace(_token=None, _api_key=None)))


class GetPgApiKeyPriorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )
        invalidate_pg_principal_cache(None)
        import app.services.pasarguard as pg_mod

        pg_mod._pg_principal_cache.clear()
        pg_mod._pg_reseller_cache.clear()
        pg_mod._pg_staff_cache.clear()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_principal_prefers_api_key_over_password(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_api",
                pg_password_enc=encrypt_secret(_PASSWORD),
            )
            p.pg_api_key_enc = encrypt_secret(_API_KEY)
            await session.commit()

            captured: dict = {}

            class _FakeClient:
                def __init__(self, *, username=None, password=None, access_token=None, api_key=None):
                    captured["username"] = username
                    captured["password"] = password
                    captured["api_key"] = api_key
                    self._token = None
                    self._api_key = api_key
                    self.is_auth_ready = True

                async def ensure_token(self):
                    return "__apikey__" if self._api_key else "t"

            with patch("app.services.pasarguard.PasarGuardClient", _FakeClient):
                client = await get_pg_for_principal(session, principal_id=int(p.id))
            self.assertEqual(captured.get("api_key"), _API_KEY)
            self.assertIsNone(captured.get("password"))
            self.assertTrue(client.is_auth_ready)

    async def test_principal_password_fallback_when_no_api_key(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_pwd",
                pg_password_enc=encrypt_secret(_PASSWORD),
            )
            await session.commit()

            captured: dict = {}

            class _FakeClient:
                def __init__(self, *, username=None, password=None, access_token=None, api_key=None):
                    captured["password"] = password
                    captured["api_key"] = api_key
                    self._token = "t"
                    self._api_key = api_key
                    self.is_auth_ready = True

                async def ensure_token(self):
                    return "t"

            with patch("app.services.pasarguard.PasarGuardClient", _FakeClient):
                await get_pg_for_principal(session, principal_id=int(p.id))
            self.assertEqual(captured.get("password"), _PASSWORD)
            self.assertIsNone(captured.get("api_key"))

    async def test_reseller_uses_api_key_on_profile(self) -> None:
        async with self.Session() as session:
            user = BotUser(
                telegram_id=900001,
                role=Role.RESELLER.value,
                referral_code="ref_api_key_test",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="shop_api",
                pg_api_key_enc=encrypt_secret(_API_KEY),
            )
            session.add(profile)
            await session.commit()

            captured: dict = {}

            class _FakeClient:
                def __init__(self, *, username=None, password=None, access_token=None, api_key=None):
                    captured["api_key"] = api_key
                    captured["password"] = password
                    self._api_key = api_key
                    self._token = None
                    self.is_auth_ready = True

                async def ensure_token(self):
                    return "__apikey__"

            with patch("app.services.pasarguard.PasarGuardClient", _FakeClient):
                await get_pg_for_reseller(session, int(user.id))
            self.assertEqual(captured.get("api_key"), _API_KEY)
            self.assertIsNone(captured.get("password"))

    async def test_reseller_cache_reuse_with_api_key_client(self) -> None:
        """Regression: API-key clients have no _token; cache must still hit."""
        async with self.Session() as session:
            user = BotUser(
                telegram_id=900002,
                role=Role.RESELLER.value,
                referral_code="ref_api_cache",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="shop_cache",
                pg_api_key_enc=encrypt_secret(_API_KEY),
            )
            session.add(profile)
            await session.commit()

            builds = {"n": 0}

            class _FakeClient:
                def __init__(self, *, username=None, password=None, access_token=None, api_key=None):
                    builds["n"] += 1
                    self._api_key = api_key
                    self._token = None
                    self.is_auth_ready = True

                async def ensure_token(self):
                    return "__apikey__"

            with patch("app.services.pasarguard.PasarGuardClient", _FakeClient):
                c1 = await get_pg_for_reseller(session, int(user.id))
                c2 = await get_pg_for_reseller(session, int(user.id))
            self.assertIs(c1, c2)
            self.assertEqual(builds["n"], 1)

    async def test_staff_api_key_then_password(self) -> None:
        async with self.Session() as session:
            row = PgStaffAccess(
                pg_username="staff_api",
                web_username="staff_api",
                web_password_hash="hash",
                pg_api_key_enc=encrypt_secret(_API_KEY),
                pg_admin_password_enc=encrypt_secret(_PASSWORD),
                is_active=True,
            )
            session.add(row)
            await session.commit()

            captured: dict = {}

            class _FakeClient:
                def __init__(self, *, username=None, password=None, access_token=None, api_key=None):
                    captured["api_key"] = api_key
                    captured["password"] = password
                    self._api_key = api_key
                    self.is_auth_ready = True

                async def ensure_token(self):
                    return "__apikey__"

            with patch("app.services.pasarguard.PasarGuardClient", _FakeClient):
                await get_pg_for_staff(session, staff_id=int(row.id))
            self.assertEqual(captured.get("api_key"), _API_KEY)
            self.assertIsNone(captured.get("password"))

    async def test_inactive_staff_fail_closed(self) -> None:
        async with self.Session() as session:
            row = PgStaffAccess(
                pg_username="staff_off",
                web_username="staff_off",
                web_password_hash="hash",
                pg_api_key_enc=encrypt_secret(_API_KEY),
                is_active=False,
            )
            session.add(row)
            await session.commit()
            with self.assertRaises(PasarGuardError) as ctx:
                await get_pg_for_staff(session, staff_id=int(row.id))
            self.assertIn("فعال نیست", str(ctx.exception))

    async def test_missing_both_credentials_fail_closed(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="l1_empty",
                pg_password_enc=None,
            )
            await session.commit()
            with self.assertRaises(PasarGuardError) as ctx:
                await get_pg_for_principal(session, principal_id=int(p.id))
            self.assertIn("کلید API یا رمز", str(ctx.exception))

    async def test_reseller_delegates_to_principal_when_api_key_on_principal(self) -> None:
        async with self.Session() as session:
            user = BotUser(
                telegram_id=900003,
                role=Role.RESELLER.value,
                referral_code="ref_api_link",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="shop_linked",
            )
            session.add(profile)
            await session.flush()
            owner = await ensure_owner_principal(session)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="shop_linked",
                reseller_profile_id=int(profile.id),
            )
            p.pg_api_key_enc = encrypt_secret(_API_KEY)
            await session.commit()

            called: dict = {}

            async def _fake_principal(session, *, principal_id=None, pg_username=None):
                called["principal_id"] = principal_id
                client = MagicMock()
                client.is_auth_ready = True
                client._token = None
                client._api_key = _API_KEY
                return client

            with patch(
                "app.services.pasarguard.get_pg_for_principal",
                new=_fake_principal,
            ):
                await get_pg_for_reseller(session, int(user.id))
            self.assertEqual(called.get("principal_id"), int(p.id))


class StaffApiKeyHelpersTests(unittest.TestCase):
    def test_remediation_flags_api_key_counts_as_ready(self) -> None:
        enc = encrypt_secret(_API_KEY)
        row = SimpleNamespace(
            pg_admin_password_enc=None,
            pg_api_key_enc=enc,
            web_username="a",
            pg_username="a",
            is_active=True,
        )
        self.assertTrue(staff_has_stored_pg_api_key(row))
        self.assertFalse(staff_has_stored_pg_password(row))
        self.assertTrue(staff_has_pg_auth_secret(row))
        flags = staff_remediation_flags(row)  # type: ignore[arg-type]
        self.assertTrue(flags["credentials_ready"])
        self.assertTrue(flags["api_key_ready"])
        self.assertFalse(flags["password_ready"])

    def test_store_and_clear_api_key(self) -> None:
        row = SimpleNamespace(id=7, pg_api_key_enc=None)
        err = store_staff_pg_api_key(row, api_key=_API_KEY)  # type: ignore[arg-type]
        self.assertIsNone(err)
        self.assertTrue(decrypt_secret(row.pg_api_key_enc) == _API_KEY)
        err = store_staff_pg_api_key(row, clear=True)  # type: ignore[arg-type]
        self.assertIsNone(err)
        self.assertIsNone(row.pg_api_key_enc)

    def test_store_rejects_short_key(self) -> None:
        row = SimpleNamespace(id=1, pg_api_key_enc=None)
        err = store_staff_pg_api_key(row, api_key="short")  # type: ignore[arg-type]
        self.assertIsNotNone(err)
        self.assertIsNone(row.pg_api_key_enc)

    def test_store_empty_keeps_existing(self) -> None:
        existing = encrypt_secret(_API_KEY)
        row = SimpleNamespace(id=2, pg_api_key_enc=existing)
        err = store_staff_pg_api_key(row, api_key="   ")  # type: ignore[arg-type]
        self.assertIsNone(err)
        self.assertEqual(row.pg_api_key_enc, existing)

    def test_clear_wins_over_new_value_semantics(self) -> None:
        row = SimpleNamespace(id=3, pg_api_key_enc=encrypt_secret(_API_KEY))
        err = store_staff_pg_api_key(row, api_key=_API_KEY, clear=True)  # type: ignore[arg-type]
        self.assertIsNone(err)
        self.assertIsNone(row.pg_api_key_enc)


class PrincipalIdentityApiKeyTests(unittest.TestCase):
    def test_l2_ready_with_api_key_only(self) -> None:
        row = SimpleNamespace(
            depth=2,
            pg_username="l2_api",
            pg_password_enc=None,
            pg_api_key_enc=encrypt_secret(_API_KEY),
        )
        _assert_pg_identity_ready(row)  # type: ignore[arg-type]

    def test_l2_denies_when_neither_credential(self) -> None:
        row = SimpleNamespace(
            depth=2,
            pg_username="l2_empty",
            pg_password_enc=None,
            pg_api_key_enc=None,
        )
        with self.assertRaises(PrincipalWebIdentityError) as ctx:
            _assert_pg_identity_ready(row)  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "pg_credential_missing")

    def test_session_rejects_api_key_ciphertext(self) -> None:
        self.assertTrue(
            session_contains_plaintext_secret({"pg_api_key_enc": "cipher"})
        )
        self.assertTrue(session_contains_plaintext_secret({"pg_api_key": _API_KEY}))
        self.assertFalse(session_contains_plaintext_secret({"username": "a"}))


class SchemaApiKeyColumnTests(unittest.TestCase):
    def test_models_have_pg_api_key_enc(self) -> None:
        self.assertTrue(hasattr(ResellerProfile, "pg_api_key_enc"))
        self.assertTrue(hasattr(PgStaffAccess, "pg_api_key_enc"))
        self.assertTrue(hasattr(OrgPrincipal, "pg_api_key_enc"))

    def test_alembic_0042_exists(self) -> None:
        from pathlib import Path

        path = Path("alembic/versions/0042_pg_api_key_enc.py")
        self.assertTrue(path.is_file())
        src = path.read_text()
        self.assertIn("pg_api_key_enc", src)
        self.assertIn("0041_demo_users", src)


class InvalidateStaffCacheTests(unittest.TestCase):
    def test_invalidate_by_staff_id(self) -> None:
        import app.services.pasarguard as pg_mod

        pg_mod._pg_staff_cache[(42, "u")] = MagicMock()
        invalidate_pg_staff_cache(staff_id=42)
        self.assertNotIn((42, "u"), pg_mod._pg_staff_cache)

    def test_invalidate_by_username(self) -> None:
        import app.services.pasarguard as pg_mod

        pg_mod._pg_staff_cache[(1, "alice")] = MagicMock()
        pg_mod._pg_staff_cache[(2, "bob")] = MagicMock()
        invalidate_pg_staff_cache(pg_username="Alice")
        self.assertNotIn((1, "alice"), pg_mod._pg_staff_cache)
        self.assertIn((2, "bob"), pg_mod._pg_staff_cache)


class ResellerApplyApiKeyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_apply_and_clear_on_profile(self) -> None:
        from app.services.resellers import apply_reseller_pg_api_key

        async with self.Session() as session:
            user = BotUser(
                telegram_id=900010,
                role=Role.RESELLER.value,
                referral_code="ref_apply_key",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="shop_apply",
            )
            session.add(profile)
            await session.commit()

            await apply_reseller_pg_api_key(session, profile, api_key=_API_KEY)
            self.assertEqual(decrypt_secret(profile.pg_api_key_enc), _API_KEY)

            await apply_reseller_pg_api_key(session, profile, clear=True)
            self.assertIsNone(profile.pg_api_key_enc)

    async def test_apply_rejects_short_key(self) -> None:
        from app.services.resellers import apply_reseller_pg_api_key

        async with self.Session() as session:
            user = BotUser(
                telegram_id=900011,
                role=Role.RESELLER.value,
                referral_code="ref_short_key",
            )
            session.add(user)
            await session.flush()
            profile = ResellerProfile(
                user_id=int(user.id),
                pg_admin_username="shop_short",
            )
            session.add(profile)
            await session.commit()
            with self.assertRaises(ValueError):
                await apply_reseller_pg_api_key(session, profile, api_key="tiny")


class OwnerEnvApiKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_pg_uses_env_api_key(self) -> None:
        from app.services.pasarguard import get_pg, reset_pg

        reset_pg()
        fake_settings = SimpleNamespace(
            pg_base_url="https://pg.example.com",
            pg_username="panel_admin",
            pg_password=_PASSWORD,
            pg_api_key=_API_KEY,
            pg_access_token="",
        )
        with patch("app.services.pasarguard.get_settings", return_value=fake_settings):
            with patch(
                "app.services.pasarguard.assert_safe_pg_base_url",
                side_effect=lambda u: u,
            ):
                client = get_pg()
        self.assertTrue(client.uses_api_key)
        self.assertEqual(client._api_key, _API_KEY)
        self.assertIsNone(client._login_password)
        reset_pg()

    async def test_platform_caps_accepts_api_key_without_password(self) -> None:
        from app.services.pg_access import resolve_platform_pg_capabilities

        class _FakeClient:
            def __init__(self, *, username=None, password=None, api_key=None, access_token=None):
                self.username = username
                self.password = password
                self._api_key = api_key
                self.base_url = "https://pg.example.com"
                self._client = MagicMock()

            async def ensure_token(self):
                return "__apikey__"

            async def get_current_admin(self):
                return {"username": "limited_admin", "is_sudo": False, "role": {"id": 3}}

            async def get_admin(self, uname):
                return None

            async def close(self):
                return None

        with patch("app.services.pasarguard.PasarGuardClient", _FakeClient):
            with patch(
                "app.services.pg_access.assert_safe_pg_base_url",
                side_effect=lambda u: u,
            ):
                with patch(
                    "app.services.pg_access.acl_from_admin_payload",
                    return_value=(["pg_users"], {"id": 3, "permissions": {}}, False),
                ):
                    caps = await resolve_platform_pg_capabilities(
                        username="limited_admin",
                        password=None,
                        api_key=_API_KEY,
                        base_url="https://pg.example.com",
                        use_cache=False,
                    )
        self.assertTrue(caps.get("ok"))
        self.assertEqual(caps.get("username"), "limited_admin")

    async def test_platform_caps_fail_closed_without_either_secret(self) -> None:
        from app.services.pg_access import resolve_platform_pg_capabilities

        caps = await resolve_platform_pg_capabilities(
            username="x",
            password="",
            api_key="",
            base_url="https://pg.example.com",
            use_cache=False,
        )
        self.assertFalse(caps.get("ok"))
        self.assertIn("کلید API", caps.get("error") or "")


class RedactApiKeyHeaderTests(unittest.TestCase):
    def test_x_api_key_header_redacted(self) -> None:
        from app.services.redact import redact

        out = redact(f"X-Api-Key: {_API_KEY}")
        self.assertNotIn(_API_KEY, out)
        self.assertIn("<redacted>", out)


class ConnectionUiCopyTests(unittest.TestCase):
    def test_settings_bot_has_api_key_field(self) -> None:
        from pathlib import Path

        html = Path("app/web/templates/_settings_bot.html").read_text(encoding="utf-8")
        self.assertIn('name="PG_API_KEY"', html)
        self.assertIn("ادمین متصل", html)
        self.assertIn("لزوماً owner پاسارگارد نیست", html)
        self.assertIn('name="clear_pg_api_key"', html)


if __name__ == "__main__":
    unittest.main()
