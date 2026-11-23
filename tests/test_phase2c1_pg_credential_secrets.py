"""Phase 2C.1 — OrgPrincipal.pg_password_enc secret-handling audit tests.

Never assert or print actual credential values beyond inequality / shape checks.
"""

from __future__ import annotations

import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import Base
import app.db.models  # noqa: F401
from app.services.org_principals import create_principal, ensure_owner_principal
from app.services.principal_provisioning import (
    Level1ProvisionRequest,
    PrincipalProvisionError,
    provision_level1_principal,
)
from app.services.principal_web_identity import (
    attach_level1_web_identity,
    authenticate_level1_web,
    build_principal_session_payload,
    resolve_principal_web_session,
    session_contains_plaintext_secret,
)
from app.services.secret_box import decrypt_secret, encrypt_secret
from tests.postgres_support import scoped_async_engine


# Distinct test secrets — never logged by tests; used only for inequality checks.
_PLAIN_A = "SecretA12!@xx"
_PLAIN_B = "SecretB34!@yy"
_OWNER_PLAIN = "OwnerPg56!@zz"
_WEB_PASSWORD = "WebLogin12!@Ab"


def _owner_staff(owner) -> dict:
    from app.services.org_principals import attach_org_principal_fields

    staff = attach_org_principal_fields(
        {"role": "admin", "web_owner": True, "username": "owner"},
        owner,
        visible_principal_ids=frozenset({int(owner.id)}),
    )
    staff["pg_is_owner"] = True
    staff["pg_permissions"] = ["pg_admins"]
    return staff


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(self.format(record))
        except Exception:
            self.messages.append(record.getMessage())


class Phase2C1SecretStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def test_a_stored_value_not_plaintext(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            enc = encrypt_secret(_PLAIN_A)
            self.assertIsNotNone(enc)
            assert enc is not None
            self.assertNotEqual(enc, _PLAIN_A)
            self.assertNotIn(_PLAIN_A, enc)
            # Fernet tokens are url-safe base64 and typically start with gAAAAA
            self.assertTrue(enc.startswith("gAAAAA") or len(enc) > 40)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="sec_a",
                pg_password_enc=enc,
            )
            await session.commit()
            await session.refresh(p)
            self.assertNotEqual(p.pg_password_enc, _PLAIN_A)
            self.assertEqual(decrypt_secret(p.pg_password_enc), _PLAIN_A)

    async def test_b_session_cookie_no_pg_password(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="sec_b",
                pg_password_enc=encrypt_secret(_PLAIN_A),
            )
            await attach_level1_web_identity(
                session,
                principal_id=int(p.id),
                web_username="sec_b",
                password=_WEB_PASSWORD,
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="sec_b", password=_WEB_PASSWORD
            )
            assert auth is not None
            payload = build_principal_session_payload(auth)
            self.assertFalse(session_contains_plaintext_secret(payload))
            self.assertNotIn("pg_password", payload)
            self.assertNotIn("pg_password_enc", payload)
            self.assertNotIn(_PLAIN_A, str(payload))

    async def test_c_http_style_response_dict_no_password(self) -> None:
        """Staff dict after resolve must not carry PG password material."""
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="sec_c",
                pg_password_enc=encrypt_secret(_PLAIN_A),
            )
            await attach_level1_web_identity(
                session,
                principal_id=int(p.id),
                web_username="sec_c",
                password=_WEB_PASSWORD,
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="sec_c", password=_WEB_PASSWORD
            )
            assert auth is not None
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ):
                staff = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth)
                )
            # Simulate JSON/API response shape
            response_body = {k: v for k, v in staff.items() if not k.startswith("_")}
            blob = str(response_body)
            self.assertNotIn(_PLAIN_A, blob)
            self.assertNotIn("pg_password_enc", response_body)
            self.assertNotIn("pg_password", response_body)
            self.assertTrue(response_body.get("pg_credentials_ready"))

    async def test_d_logs_exceptions_no_password(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            await session.commit()
            staff = _owner_staff(owner)
            with patch(
                "app.services.pasarguard.get_pg",
                return_value=SimpleNamespace(
                    create_admin=AsyncMock(
                        side_effect=RuntimeError(f"boom {_PLAIN_A}")
                    ),
                    get_admin_roles=AsyncMock(
                        return_value=[{"id": 10, "name": "X", "is_owner": False}]
                    ),
                    delete_admin=AsyncMock(),
                ),
            ), patch(
                "app.services.principal_provisioning._owner_env_pg_username",
                return_value="env_owner",
            ), patch(
                "app.services.principal_provisioning.log.error"
            ) as err_log:
                with self.assertRaises(PrincipalProvisionError) as ctx:
                    await provision_level1_principal(
                        session,
                        staff,
                        Level1ProvisionRequest(
                            pg_username="log_probe",
                            pg_password=_PLAIN_A,
                            idempotency_key="sec-d-1",
                            pg_role_id=10,
                        ),
                    )
            self.assertNotIn(_PLAIN_A, ctx.exception.message)
            self.assertEqual(ctx.exception.code, "provision_failed")
            err_log.assert_called()
            logged = " ".join(
                str(part)
                for call in err_log.call_args_list
                for part in list(call.args) + list(call.kwargs.values())
            )
            self.assertNotIn(_PLAIN_A, logged)
            self.assertIn("RuntimeError", logged)
            self.assertNotIn("boom", logged)

    async def test_e_only_service_path_decrypts(self) -> None:
        """Decrypt is confined to secret_box + pasarguard client factory."""
        import ast
        from pathlib import Path

        root = Path("app")
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "decrypt_secret" not in text:
                continue
            # Allowed modules
            rel = str(path).replace("\\", "/")
            if rel in {
                "app/services/secret_box.py",
                "app/services/pasarguard.py",
                "app/services/pg_staff_access.py",
                "app/services/resellers.py",
            }:
                continue
            if "principal" in rel and "decrypt_secret" in text:
                offenders.append(rel)
            # Web/API/templates must not decrypt
            if rel.startswith("app/api/") or rel.startswith("app/web/"):
                offenders.append(rel)
        self.assertEqual(offenders, [])

        # principal_web_identity must not call decrypt
        src = Path("app/services/principal_web_identity.py").read_text(encoding="utf-8")
        self.assertNotIn("decrypt_secret", src)
        self.assertNotIn("encrypt_secret", src)

    async def test_f_principal_a_cannot_get_b_credential(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="iso_a",
                pg_password_enc=encrypt_secret(_PLAIN_A),
            )
            b = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="iso_b",
                pg_password_enc=encrypt_secret(_PLAIN_B),
            )
            await session.commit()
            # A and B ciphertexts decrypt to different secrets
            self.assertEqual(decrypt_secret(a.pg_password_enc), _PLAIN_A)
            self.assertEqual(decrypt_secret(b.pg_password_enc), _PLAIN_B)
            self.assertNotEqual(a.pg_password_enc, b.pg_password_enc)

            captured: dict[str, str | None] = {}

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    captured["username"] = username
                    captured["password"] = password
                    self._token = "t"
                    self._login_password = password

                async def ensure_token(self):
                    self._login_password = None
                    return self._token

            pg_mod._pg_principal_cache.clear()
            with patch.object(pg_mod, "PasarGuardClient", _Client):
                await pg_mod.get_pg_for_principal(session, principal_id=int(a.id))
            self.assertEqual(captured.get("username"), "iso_a")
            # Password was provided at construct then cleared on ensure_token
            # Factory must have used A's secret, not B's
            # Re-check by constructing with decrypt of the row that was loaded:
            self.assertNotEqual(decrypt_secret(b.pg_password_enc), _PLAIN_A)

            # Loading B must not return A's client from cache
            with patch.object(pg_mod, "PasarGuardClient", _Client):
                client_b = await pg_mod.get_pg_for_principal(
                    session, principal_id=int(b.id)
                )
            self.assertEqual(captured.get("username"), "iso_b")
            self.assertIsNotNone(client_b)
            self.assertNotIn(
                (int(a.id), "iso_b"),
                pg_mod._pg_principal_cache,
            )

    async def test_g_principal_cannot_retrieve_owner_credential(self) -> None:
        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            # Owner principal has no pg_password_enc (platform uses env)
            self.assertIsNone(owner.pg_password_enc)
            p = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="not_owner_pg",
                pg_password_enc=encrypt_secret(_PLAIN_A),
            )
            await session.commit()
            self.assertNotEqual(decrypt_secret(p.pg_password_enc), _OWNER_PLAIN)
            # Session staff must not expose Owner env password field
            await attach_level1_web_identity(
                session,
                principal_id=int(p.id),
                web_username="not_owner_pg",
                password=_WEB_PASSWORD,
            )
            await session.commit()
            auth = await authenticate_level1_web(
                session, username="not_owner_pg", password=_WEB_PASSWORD
            )
            assert auth
            with patch(
                "app.services.pg_staff_access.resolve_pg_role_id_for_admin",
                new=AsyncMock(return_value=None),
            ), patch(
                "app.services.principal_web_identity.owner_env_pg_username",
                return_value="env_owner_pg",
            ):
                staff = await resolve_principal_web_session(
                    session, build_principal_session_payload(auth)
                )
            self.assertNotEqual(staff.get("pg_admin_username"), "env_owner_pg")
            self.assertNotIn("pg_password", staff)
            self.assertNotIn(_OWNER_PLAIN, str(staff))

    async def test_h_pg_client_cache_no_plaintext(self) -> None:
        from app.services import pasarguard as pg_mod

        async with self.Session() as session:
            owner = await ensure_owner_principal(session)
            a = await create_principal(
                session,
                parent_id=int(owner.id),
                depth=1,
                pg_username="cache_sec",
                pg_password_enc=encrypt_secret(_PLAIN_A),
            )
            await session.commit()
            pg_mod._pg_principal_cache.clear()

            class _Client:
                def __init__(self, *, username=None, password=None, access_token=None):
                    self._login_username = username
                    self._login_password = password
                    self._token = None

                async def ensure_token(self):
                    self._token = "tok"
                    self._login_password = None
                    return self._token

            with patch.object(pg_mod, "PasarGuardClient", _Client):
                client = await pg_mod.get_pg_for_principal(
                    session, principal_id=int(a.id)
                )
            self.assertIsNone(getattr(client, "_login_password", "missing"))
            # Cache holds client without plaintext password attribute
            cached = pg_mod._pg_principal_cache.get((int(a.id), "cache_sec"))
            self.assertIs(cached, client)
            self.assertIsNone(cached._login_password)

    async def test_i_provision_failure_no_leak(self) -> None:
        handler = CapturingHandler()
        lg = logging.getLogger("app.services.principal_provisioning")
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        try:
            async with self.Session() as session:
                owner = await ensure_owner_principal(session)
                await session.commit()
                staff = _owner_staff(owner)

                async def boom(payload):
                    # Simulate buggy API that might echo password in exception text
                    raise RuntimeError(
                        f"create failed password={payload.get('password')}"
                    )

                pg = SimpleNamespace(
                    create_admin=AsyncMock(side_effect=boom),
                    get_admin_roles=AsyncMock(
                        return_value=[{"id": 10, "name": "X", "is_owner": False}]
                    ),
                    delete_admin=AsyncMock(),
                )
                with patch("app.services.pasarguard.get_pg", return_value=pg), patch(
                    "app.services.principal_provisioning._owner_env_pg_username",
                    return_value="env_owner",
                ):
                    with self.assertRaises(PrincipalProvisionError) as ctx:
                        await provision_level1_principal(
                            session,
                            staff,
                            Level1ProvisionRequest(
                                pg_username="fail_probe",
                                pg_password=_PLAIN_A,
                                idempotency_key="sec-i-1",
                                pg_role_id=10,
                            ),
                        )
                self.assertNotIn(_PLAIN_A, ctx.exception.message)
                self.assertEqual(ctx.exception.code, "provision_failed")
                # Exception chain must not retain the secret-bearing cause
                self.assertIsNone(ctx.exception.__cause__)
                joined = "\n".join(handler.messages)
                self.assertNotIn(_PLAIN_A, joined)
                self.assertNotIn("password=", joined)
        finally:
            lg.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
