"""Behavioral authorization and credential tests with synthetic local identities."""

from __future__ import annotations

import hashlib
import logging
import secrets
import unittest
from collections.abc import Awaitable
from typing import Any
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import ColumnElement, event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.integration_transport import IntegrationPeerCapture
from app.api.integration_auth import PGVerifier, integration_bearer_dependency, register_integration_auth_errors
from app.db import Base
from app.db.integration_models import Integration, IntegrationCredential, IntegrationPlan
from app.db.models import BotUser, OrgPrincipal, OrgPrincipalWebIdentity, Plan, ResellerProfile, Setting
from app.config import get_settings
from app.services.integration_authority import IntegrationAuthority, IntegrationPGEvidence, resolve_integration_authority
from app.services.integration_credentials import (
    CredentialIssuance, DEFAULT_CREDENTIAL_LIFETIME, issue_credential, read_credential, revoke_credential, rotate_credential,
)
from app.services.integration_policy import IntegrationError, IntegrationShop, ScopeKind
from app.services.integrations import IntegrationView, configure_integration, create_integration, set_integration_plans
from app.services.resellers import DEFAULT_FEATURE_PERMS, FEATURE_PERMS
from app.services.users import DEFAULT_SETTINGS

READ_SCOPES = frozenset({"customers:read", "operations:read", "services:read", "plans:read"})
ALL_SCOPES = READ_SCOPES | {"services:provision", "services:renew", "services:addons", "services:access"}
OWNER_PERMS = ",".join(key for key, _ in FEATURE_PERMS)


class IntegrationFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")

        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection: Any, record: Any) -> None:
            connection.execute("PRAGMA foreign_keys=ON")

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.session = self.sessions()
        self.settings_patch = patch("app.api.integration_auth.get_settings", return_value=get_settings().model_copy(
            update={"web_secret": "synthetic-integration-security-key-0001", "integration_trusted_proxies": ""},
        ))
        self.settings_patch.start()
        self.now = datetime.now(timezone.utc)
        self.session.add_all([
            BotUser(id=n, telegram_id=10000+n, referral_code=f"synthetic-{n}") for n in (11, 12, 13)
        ])
        await self.session.flush()
        self.session.add_all([
            ResellerProfile(id=n, user_id=n, is_active=True, web_username=f"shop{n}",
                            web_password_hash="synthetic-hash", setup_completed_at=self.now,
                            pg_admin_username=f"pg-shop{n}", web_permissions=OWNER_PERMS)
            for n in (11, 12, 13)
        ])
        await self.session.flush()
        self.session.add(OrgPrincipal(id=1, depth=0, status="active", pg_username="pg-owner"))
        await self.session.flush()
        for principal_id, parent_id, depth in ((11, 1, 1), (12, 1, 1), (13, 11, 2)):
            self.session.add(OrgPrincipal(
                id=principal_id, parent_id=parent_id, depth=depth, status="active", bot_user_id=principal_id,
                reseller_profile_id=principal_id, pg_username=f"pg-shop{principal_id}", pg_api_key_enc="synthetic-ciphertext",
            ))
            await self.session.flush()
        self.session.add_all([
            OrgPrincipalWebIdentity(id=n, principal_id=n, web_username=f"principal{n}",
                                    web_password_hash="synthetic-hash", is_active=True)
            for n in (11, 12, 13)
        ])
        self.session.add_all([
            Plan(id=1, name="platform", price=1),
            Plan(id=11, name="shop11", price=1, owner_reseller_id=11),
            Plan(id=12, name="shop12", price=1, owner_reseller_id=12),
            Plan(id=14, name="trial", price=1, owner_reseller_id=11, is_trial=True),
            Plan(id=15, name="inactive", price=1, owner_reseller_id=11, is_active=False),
            Plan(id=16, name="negative duration", price=1, owner_reseller_id=11, duration_days=-1),
            Plan(id=17, name="negative data", price=1, owner_reseller_id=11, data_limit_gb=-1),
            Plan(id=18, name="infinite data", price=1, owner_reseller_id=11, data_limit_gb=float("inf")),
            Setting(key="integration_api_enabled", value="1"),
        ])
        await self.session.commit()
        self.staff = {"role": "principal", "web_identity_id": 11}
        self.other_staff = {"role": "principal", "web_identity_id": 12}
        self.integration = await self.new_integration(self.staff)
        self.key = await issue_credential(self.session, self.staff, self.integration.id, scopes=ALL_SCOPES)

    async def asyncTearDown(self) -> None:
        self.settings_patch.stop()
        await self.session.close()
        await self.engine.dispose()

    async def new_integration(self, staff: dict) -> IntegrationView:
        result = await create_integration(self.session, staff, grants=ALL_SCOPES)
        return await configure_integration(self.session, staff, result.id, status="active", grants=ALL_SCOPES)

    async def resolve(self, **kwargs: Any) -> IntegrationAuthority:
        return await resolve_integration_authority(self.session, self.key.bearer, **kwargs)

    def evidence(self, **kwargs: Any) -> IntegrationPGEvidence:
        return replace(IntegrationPGEvidence(
            11, "pg-shop11", datetime.now(timezone.utc), True,
            frozenset({"read", "create", "update"}), frozenset({"templates", "groups"}),
        ), **kwargs)

    async def change(self, model: type[Base], condition: ColumnElement[bool], **values: Any) -> None:
        await self.session.execute(update(model).where(condition).values(**values))
        await self.session.commit()

    async def expect_code(self, awaitable: Awaitable[Any], code: str) -> None:
        with self.assertRaises(IntegrationError) as caught:
            await awaitable
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(self.key.bearer, str(caught.exception))
        await self.session.rollback()

    async def test_valid_authentication_and_permission_intersection(self) -> None:
        authority = await self.resolve(required_scopes=ALL_SCOPES, pg_evidence=self.evidence())
        self.assertEqual(authority.local_scopes, ALL_SCOPES | {"capabilities:read"})
        self.assertEqual(authority.owner.principal_id, 11)
        self.assertEqual(authority.shop, IntegrationShop(ScopeKind.SHOP, 11))
        await self.change(ResellerProfile, ResellerProfile.id == 11, web_permissions="integration_services_read")
        authority = await self.resolve()
        self.assertEqual(authority.local_scopes, {"services:read", "capabilities:read"})
        await self.expect_code(self.resolve(required_scopes=frozenset({"customers:read"})), "permission_denied")

    async def test_global_switch_is_false_by_default_and_not_cached(self) -> None:
        self.assertEqual(DEFAULT_SETTINGS["integration_api_enabled"], "0")
        await self.resolve()
        await self.change(Setting, Setting.key == "integration_api_enabled", value="0")
        await self.expect_code(self.resolve(), "api_disabled")
        await self.session.execute(Setting.__table__.delete())
        await self.session.commit()
        await self.expect_code(self.resolve(), "api_disabled")

    async def test_new_integrations_are_disabled(self) -> None:
        row = await create_integration(self.session, self.staff)
        self.assertEqual(row.status, "disabled")
        key = await issue_credential(self.session, self.staff, row.id)
        await self.expect_code(resolve_integration_authority(self.session, key.bearer), "integration_disabled")

    async def test_new_permissions_are_declared_without_default_grants(self) -> None:
        keys = {key for key, _ in FEATURE_PERMS if key.startswith("integration")}
        self.assertEqual(len(keys), 13)
        self.assertFalse(keys & set(DEFAULT_FEATURE_PERMS.split(",")))

    async def test_credential_scopes_default_empty(self) -> None:
        empty = await issue_credential(self.session, self.staff, self.integration.id)
        self.assertFalse(empty.credential.scopes)
        authority = await resolve_integration_authority(self.session, empty.bearer)
        self.assertEqual(authority.local_scopes, {"capabilities:read"})
        await self.expect_code(resolve_integration_authority(
            self.session, empty.bearer, required_scopes=frozenset({"customers:read"}),
        ), "permission_denied")

    async def test_unknown_scopes_rejected_for_keys_and_grants(self) -> None:
        for call in (
            issue_credential(self.session, self.staff, self.integration.id, scopes=frozenset({"unknown:write"})),
            configure_integration(self.session, self.staff, self.integration.id, status="active", grants=frozenset({"*"})),
            self.resolve(required_scopes=frozenset({"unknown"})),
        ):
            await self.expect_code(call, "invalid_request")

    async def test_grants_cannot_exceed_current_owner_permissions(self) -> None:
        await self.change(ResellerProfile, ResellerProfile.id == 11, web_permissions="integrations_manage")
        await self.expect_code(create_integration(self.session, self.staff, grants=READ_SCOPES), "permission_denied")
        await self.expect_code(issue_credential(self.session, self.staff, self.integration.id, scopes=READ_SCOPES), "permission_denied")

    async def test_credential_cannot_exceed_integration_grants(self) -> None:
        await configure_integration(self.session, self.staff, self.integration.id, status="active", grants=READ_SCOPES)
        await self.expect_code(issue_credential(self.session, self.staff, self.integration.id, scopes=ALL_SCOPES), "permission_denied")
        self.assertEqual((await self.resolve()).local_scopes, READ_SCOPES | {"capabilities:read"})

    async def test_invalid_unknown_and_revoked_keys_share_safe_classification(self) -> None:
        for bearer in ("", "malformed", f"pgint_{'0'*32}.{'1'*64}", self.key.bearer[:-1] + ("0" if self.key.bearer[-1] != "0" else "1")):
            await self.expect_code(resolve_integration_authority(self.session, bearer), "invalid_credential")
        await self.resolve()
        await revoke_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)
        await revoke_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)
        await self.expect_code(self.resolve(), "invalid_credential")

    async def test_unknown_selector_performs_same_digest_comparison(self) -> None:
        actual_compare = secrets.compare_digest
        with patch("app.services.integration_credentials.secrets.compare_digest", wraps=actual_compare) as compare:
            await self.resolve()
            self.assertEqual(compare.call_count, 1)
            compare.reset_mock()
            await self.expect_code(resolve_integration_authority(self.session, f"pgint_{'0'*32}.{'1'*64}"), "invalid_credential")
            self.assertEqual(compare.call_count, 1)
            self.assertEqual(tuple(map(len, compare.call_args.args)), (64, 64))

    async def test_default_and_maximum_expiry_and_invalid_expiry_inputs(self) -> None:
        row = await self.session.get(IntegrationCredential, self.key.credential.selector)
        self.assertEqual(row.expires_at - row.created_at, DEFAULT_CREDENTIAL_LIFETIME)
        maximum = await issue_credential(self.session, self.staff, self.integration.id,
                                         expires_at=datetime.now(timezone.utc)+timedelta(days=365))
        self.assertIsNotNone(maximum.credential.expires_at.tzinfo)
        for expiry in (self.now - timedelta(seconds=1), self.now + timedelta(days=366), self.now.replace(tzinfo=None), True):
            await self.expect_code(issue_credential(self.session, self.staff, self.integration.id, expires_at=expiry), "invalid_request")

    async def test_expired_credential_is_rejected(self) -> None:
        await self.change(IntegrationCredential, IntegrationCredential.selector == self.key.credential.selector,
                          created_at=self.now-timedelta(days=2), expires_at=self.now-timedelta(days=1))
        await self.expect_code(self.resolve(), "invalid_credential")

    async def test_rotation_preserves_identity_and_bounds_overlap(self) -> None:
        replacement = await rotate_credential(self.session, self.staff, self.integration.id,
                                             self.key.credential.selector, overlap=timedelta(hours=24))
        self.assertEqual(replacement.credential.integration_id, self.integration.id)
        self.assertNotEqual(replacement.bearer, self.key.bearer)
        await self.resolve()
        await resolve_integration_authority(self.session, replacement.bearer)
        old = await read_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)
        self.assertLessEqual(old.expires_at - old.rotated_at, timedelta(hours=24))
        await self.expect_code(rotate_credential(self.session, self.staff, self.integration.id, self.key.credential.selector), "invalid_credential")
        await self.change(IntegrationCredential, IntegrationCredential.selector == self.key.credential.selector,
                          created_at=self.now-timedelta(days=2), expires_at=self.now-timedelta(days=1))
        await self.expect_code(self.resolve(), "invalid_credential")
        await resolve_integration_authority(self.session, replacement.bearer)

    async def test_rotation_without_overlap_immediately_expires_old_key(self) -> None:
        new_key = await rotate_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)
        await self.expect_code(self.resolve(), "invalid_credential")
        await resolve_integration_authority(self.session, new_key.bearer)

    async def test_overlap_cannot_extend_original_expiry(self) -> None:
        near = await issue_credential(self.session, self.staff, self.integration.id,
                                      expires_at=datetime.now(timezone.utc)+timedelta(minutes=10))
        await rotate_credential(self.session, self.staff, self.integration.id, near.credential.selector, overlap=timedelta(hours=24))
        old = await read_credential(self.session, self.staff, self.integration.id, near.credential.selector)
        self.assertEqual(old.expires_at, near.credential.expires_at)

    async def test_rotation_rejects_invalid_overlap(self) -> None:
        for overlap in (timedelta(hours=25), timedelta(seconds=-1), True):
            await self.expect_code(rotate_credential(self.session, self.staff, self.integration.id,
                                                    self.key.credential.selector, overlap=overlap), "invalid_request")

    async def test_revoking_rotated_key_is_immediate_and_does_not_revoke_replacement(self) -> None:
        key = await rotate_credential(self.session, self.staff, self.integration.id,
                                     self.key.credential.selector, overlap=timedelta(hours=1))
        await revoke_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)
        await self.expect_code(self.resolve(), "invalid_credential")
        await resolve_integration_authority(self.session, key.bearer)

    async def test_disabled_and_retired_integrations_deny_authentication(self) -> None:
        for status in ("disabled", "retired"):
            await configure_integration(self.session, self.staff, self.integration.id, status=status, grants=ALL_SCOPES)
            await self.expect_code(self.resolve(), "integration_disabled")
        await self.expect_code(configure_integration(self.session, self.staff, self.integration.id,
                                                    status="active", grants=ALL_SCOPES), "integration_disabled")

    async def test_inactive_owner_and_ineligible_profile_deny(self) -> None:
        await self.change(OrgPrincipal, OrgPrincipal.id == 11, status="disabled")
        await self.expect_code(self.resolve(), "principal_inactive")
        await self.change(OrgPrincipal, OrgPrincipal.id == 11, status="active")
        for values in ({"is_active": False}, {"setup_completed_at": None}, {"web_password_hash": None}):
            await self.change(ResellerProfile, ResellerProfile.id == 11, **values)
            await self.expect_code(self.resolve(), "principal_inactive")
            await self.change(ResellerProfile, ResellerProfile.id == 11, is_active=True,
                              setup_completed_at=self.now, web_password_hash="synthetic-hash")

    async def test_own_pg_identity_required_without_parent_or_profile_fallback(self) -> None:
        await self.change(OrgPrincipal, OrgPrincipal.id == 11, pg_api_key_enc=None, pg_password_enc=None)
        await self.change(ResellerProfile, ResellerProfile.id == 11, pg_api_key_enc="parent-must-not-be-used")
        await self.expect_code(self.resolve(), "principal_inactive")

    async def test_depth_two_requires_active_correct_parent(self) -> None:
        staff = {"role": "principal", "web_identity_id": 13}
        integration = await self.new_integration(staff)
        key = await issue_credential(self.session, staff, integration.id)
        await resolve_integration_authority(self.session, key.bearer)
        await self.change(OrgPrincipal, OrgPrincipal.id == 11, status="disabled")
        await self.expect_code(resolve_integration_authority(self.session, key.bearer), "principal_inactive")
        await self.change(OrgPrincipal, OrgPrincipal.id == 13, parent_id=1)
        await self.expect_code(resolve_integration_authority(self.session, key.bearer), "principal_inactive")

    async def test_bare_admin_and_scopeless_staff_cannot_manage(self) -> None:
        for staff in ({"role": "admin"}, {"role": "pg_staff"}, {"role": "reseller"}, {},
                      {"role": "principal", "org_principal_id": 1, "org_depth": 0, "web_owner": True}):
            await self.expect_code(create_integration(self.session, staff), "permission_denied")

    async def test_management_requires_its_own_permission(self) -> None:
        await self.change(ResellerProfile, ResellerProfile.id == 11, web_permissions="integration_services_read")
        await self.expect_code(issue_credential(self.session, self.staff, self.integration.id), "permission_denied")
        await self.expect_code(revoke_credential(self.session, self.staff, self.integration.id,
                                                self.key.credential.selector), "permission_denied")

    async def test_request_supplied_principal_and_shop_cannot_establish_authority(self) -> None:
        spoofed_staff = {**self.staff, "org_principal_id": 12, "org_depth": 0, "bot_user_id": 12, "web_owner": True}
        integration = await create_integration(self.session, spoofed_staff)
        self.assertEqual(integration.owner_principal_id, 11)
        self.assertEqual(integration.shop.shop_id, 11)

    async def test_other_shop_cannot_manage_integration_or_credentials(self) -> None:
        await self.expect_code(issue_credential(self.session, self.other_staff, self.integration.id), "not_found")
        await self.expect_code(revoke_credential(self.session, self.other_staff, self.integration.id,
                                                self.key.credential.selector), "not_found")
        await self.expect_code(set_integration_plans(self.session, self.other_staff, self.integration.id,
                                                   plan_ids=frozenset({12})), "not_found")

    async def test_other_integration_cannot_read_or_rotate_credentials(self) -> None:
        second = await self.new_integration(self.staff)
        await self.expect_code(read_credential(self.session, self.staff, second.id, self.key.credential.selector), "not_found")
        await self.expect_code(rotate_credential(self.session, self.staff, second.id, self.key.credential.selector), "not_found")
        authority = await self.resolve()
        for integration, shop in ((second.id, authority.shop), (authority.integration_id, IntegrationShop(ScopeKind.SHOP, 12))):
            with self.assertRaises(IntegrationError) as caught:
                authority.require_resource(integration_id=integration, shop=shop)
            self.assertEqual(caught.exception.code, "not_found")

    async def test_platform_scope_is_explicit_and_only_for_owner(self) -> None:
        owner_staff = {"role": "admin", "web_owner": True}
        platform = await self.new_integration(owner_staff)
        self.assertEqual(platform.shop, IntegrationShop(ScopeKind.PLATFORM, None))
        key = await issue_credential(self.session, owner_staff, platform.id, scopes=READ_SCOPES)
        authority = await resolve_integration_authority(self.session, key.bearer)
        self.assertEqual(authority.owner.principal_id, 1)
        await set_integration_plans(self.session, owner_staff, platform.id, plan_ids=frozenset({1}))
        await self.expect_code(set_integration_plans(self.session, owner_staff, platform.id, plan_ids=frozenset({11})), "plan_ineligible")
        for kind, shop_id in ((ScopeKind.SHOP, None), (ScopeKind.PLATFORM, 11)):
            with self.assertRaises(IntegrationError):
                IntegrationShop(kind, shop_id)

    async def test_owner_shop_reassignment_is_rejected(self) -> None:
        for field, value in (("owner_principal_id", 12), ("shop_id", 12), ("scope_kind", "platform"), ("id", "intg_changed")):
            row = await self.session.get(Integration, self.integration.id)
            setattr(row, field, value)
            with self.assertRaises(ValueError):
                await self.session.flush()
            await self.session.rollback()
        self.assertEqual((await self.resolve()).owner.principal_id, 11)

    async def test_owner_profile_shop_change_fails_closed(self) -> None:
        await self.change(OrgPrincipal, OrgPrincipal.id == 11, reseller_profile_id=None)
        await self.expect_code(self.resolve(), "principal_inactive")

    async def test_allowlist_is_scoped_active_and_nontrial_and_revalidated(self) -> None:
        await set_integration_plans(self.session, self.staff, self.integration.id, plan_ids=frozenset({11}))
        self.assertEqual((await self.resolve()).allowed_plan_ids, {11})
        for plans in ({12}, {1}, {14}, {15}, {16}, {17}, {18}, {99999}):
            await self.expect_code(set_integration_plans(self.session, self.staff, self.integration.id,
                                                       plan_ids=frozenset(plans)), "plan_ineligible")
        await self.change(Plan, Plan.id == 11, is_active=False)
        self.assertFalse((await self.resolve()).allowed_plan_ids)
        with self.assertRaises(IntegrationError):
            (await self.resolve()).require_plan(11)

    async def test_empty_allowlist_and_strict_plan_ids(self) -> None:
        self.assertFalse((await self.resolve()).allowed_plan_ids)
        for plans in ({True}, {-1}, {2**31}, {1.5}, set(range(1, 102))):
            await self.expect_code(set_integration_plans(self.session, self.staff, self.integration.id,
                                                       plan_ids=frozenset(plans)), "invalid_request")

    async def test_allowlisted_plan_moved_to_another_shop_is_no_longer_authorized(self) -> None:
        await set_integration_plans(self.session, self.staff, self.integration.id, plan_ids=frozenset({11}))
        await self.change(Plan, Plan.id == 11, owner_reseller_id=12)
        self.assertFalse((await self.resolve()).allowed_plan_ids)

    async def test_credential_cannot_be_reassigned_to_another_integration(self) -> None:
        second = await self.new_integration(self.staff)
        key = await self.session.get(IntegrationCredential, self.key.credential.selector)
        key.integration_id = second.id
        with self.assertRaises(ValueError):
            await self.session.flush()
        await self.session.rollback()
        self.assertEqual((await self.resolve()).integration_id, self.integration.id)

    async def test_new_integration_does_not_transfer_existing_allowlist(self) -> None:
        await set_integration_plans(self.session, self.staff, self.integration.id, plan_ids=frozenset({11}))
        second = await self.new_integration(self.staff)
        key = await issue_credential(self.session, self.staff, second.id)
        self.assertFalse((await resolve_integration_authority(self.session, key.bearer)).allowed_plan_ids)
        row = await self.session.scalar(select(IntegrationPlan).where(IntegrationPlan.integration_id == self.integration.id))
        self.assertEqual(row.plan_id, 11)

    async def test_local_scopes_do_not_prove_panel_authority(self) -> None:
        await self.expect_code(self.resolve(required_scopes=frozenset({"services:read"})), "authorization_unavailable")
        await self.expect_code(self.resolve(required_scopes=frozenset({"services:read"}),
                                            pg_evidence=self.evidence(user_actions=frozenset())), "permission_denied")
        await self.expect_code(self.resolve(required_scopes=frozenset({"plans:read"}),
                                            pg_evidence=self.evidence(catalog_actions=frozenset({"groups"}))), "permission_denied")

    async def test_panel_evidence_bound_to_principal_identity_and_freshness(self) -> None:
        for evidence in (self.evidence(principal_id=12), self.evidence(pg_username="pg-parent"),
                         self.evidence(observed_at=self.now-timedelta(minutes=6)),
                         self.evidence(observed_at=self.now+timedelta(days=1))):
            await self.expect_code(self.resolve(required_scopes=frozenset({"services:read"}),
                                                pg_evidence=evidence), "authorization_unavailable")

    async def test_read_snapshot_allowed_but_mutations_require_live_evidence(self) -> None:
        evidence = self.evidence(is_live=False)
        await self.resolve(required_scopes=frozenset({"services:read"}), pg_evidence=evidence)
        await self.expect_code(self.resolve(required_scopes=frozenset({"services:provision"}),
                                            pg_evidence=evidence), "authorization_unavailable")
        await self.expect_code(self.resolve(required_scopes=frozenset({"services:read"}),
                                            pg_evidence=self.evidence(rejected=True)), "permission_denied")

    async def test_private_access_also_requires_read_scope(self) -> None:
        key = await issue_credential(self.session, self.staff, self.integration.id, scopes=frozenset({"services:access"}))
        await self.expect_code(resolve_integration_authority(self.session, key.bearer,
                              required_scopes=frozenset({"services:access"}), pg_evidence=self.evidence()), "permission_denied")

    async def test_secrets_excluded_from_storage_ordinary_outputs_logs_and_errors(self) -> None:
        output = StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        try:
            issued = await issue_credential(self.session, self.staff, self.integration.id, scopes=READ_SCOPES)
            view = await read_credential(self.session, self.staff, self.integration.id, issued.credential.selector)
            authority = await resolve_integration_authority(self.session, issued.bearer)
            records = (await self.session.execute(IntegrationCredential.__table__.select())).mappings().all()
            secret = issued.bearer.split(".")[1]
            record = next(row for row in records if row["selector"] == issued.credential.selector)
            self.assertEqual(record["secret_digest"], hashlib.sha256(secret.encode()).hexdigest())
            self.assertEqual(len(secret), 64)
            ordinary = repr((records, asdict(view), asdict(authority), issued))
            self.assertNotIn(secret, ordinary)
            self.assertNotIn(issued.bearer, ordinary)
            await self.expect_code(resolve_integration_authority(self.session, issued.bearer + "oops"), "invalid_credential")
            self.assertNotIn(secret, output.getvalue())
        finally:
            logging.getLogger().removeHandler(handler)

    def isolated_app(self, *, scopes: frozenset[str] = frozenset(), verifier: PGVerifier | None = None) -> FastAPI:
        app = FastAPI()
        app.add_middleware(IntegrationPeerCapture)
        register_integration_auth_errors(app)

        async def database() -> AsyncSession:
            yield self.session

        dependency = integration_bearer_dependency(database, required_scopes=scopes, pg_verifier=verifier)

        @app.get("/protected")
        async def protected(authority: IntegrationAuthority = Depends(dependency)) -> dict:
            return {"integration_id": authority.integration_id, "scopes": sorted(authority.local_scopes)}

        return app

    async def request(self, *, headers: Any = None, query: str = "", app: FastAPI | None = None) -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.isolated_app()), base_url="https://test") as client:
            return await client.get(f"/protected{query}", headers=headers)

    async def test_bearer_dependency_returns_typed_authority(self) -> None:
        response = await self.request(headers={"Authorization": f"bEaReR {self.key.bearer}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["integration_id"], self.integration.id)

    async def test_missing_and_invalid_auth_have_safe_bearer_challenge(self) -> None:
        for headers in ({}, {"Authorization": "Bearer unknown"}, {"Authorization": "Bearer"}):
            response = await self.request(headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
            self.assertEqual(response.json()["error"]["code"], "invalid_credential")
            self.assertIn("no-store", response.headers["Cache-Control"])
            self.assertNotIn(self.key.bearer, response.text)

    async def test_duplicate_headers_and_mixed_auth_are_rejected(self) -> None:
        bearer = ("Authorization", f"Bearer {self.key.bearer}")
        for headers in ([bearer, bearer], [bearer, ("authorization", "Basic unknown")],
                        [bearer, ("X-Api-Key", "unknown")], [bearer, ("Proxy-Authorization", "Basic unknown")]):
            response = await self.request(headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "unsupported_authentication")

    async def test_ambiguous_bearer_values_are_rejected(self) -> None:
        for value in (f"Bearer {self.key.bearer}, Bearer unknown", f"Bearer  {self.key.bearer}",
                      f"Bearer {self.key.bearer} extra", f"Basic unknown, Bearer {self.key.bearer}"):
            self.assertNotEqual((await self.request(headers={"Authorization": value})).status_code, 200)

    async def test_cookies_and_origin_have_no_authentication_fallback(self) -> None:
        bearer = {"Authorization": f"Bearer {self.key.bearer}"}
        for headers in ({"Cookie": "session=synthetic"}, {**bearer, "Cookie": "setup=synthetic"},
                        {**bearer, "Cookie": " "}, {**bearer, "Origin": "https://website.invalid"}):
            response = await self.request(headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "unsupported_authentication")

    async def test_query_credentials_are_rejected_even_with_valid_header(self) -> None:
        for key in ("token", "api_key", "Authorization", "access_token", "bearer", "credential"):
            response = await self.request(headers={"Authorization": f"Bearer {self.key.bearer}"}, query=f"?{key}={self.key.bearer}")
            self.assertEqual(response.status_code, 400)
            self.assertNotIn(self.key.bearer, response.text)

    async def test_verifier_runs_outside_transaction_and_revocation_is_rechecked(self) -> None:
        async def verifier(authority: IntegrationAuthority) -> IntegrationPGEvidence:
            self.assertFalse(self.session.in_transaction())
            await revoke_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)
            return self.evidence()

        response = await self.request(headers={"Authorization": f"Bearer {self.key.bearer}"},
                                      app=self.isolated_app(scopes=frozenset({"services:read"}), verifier=verifier))
        self.assertEqual(response.status_code, 401)

    async def test_dependency_requires_separate_panel_evidence(self) -> None:
        response = await self.request(headers={"Authorization": f"Bearer {self.key.bearer}"},
                                      app=self.isolated_app(scopes=frozenset({"services:read"})))
        self.assertEqual(response.status_code, 503)

        async def verifier(authority: IntegrationAuthority) -> IntegrationPGEvidence:
            return self.evidence()

        response = await self.request(headers={"Authorization": f"Bearer {self.key.bearer}"},
                                      app=self.isolated_app(scopes=frozenset({"services:read"}), verifier=verifier))
        self.assertEqual(response.status_code, 200)

    async def test_panel_transport_failure_returns_safe_authorization_error(self) -> None:
        async def verifier(authority: IntegrationAuthority) -> IntegrationPGEvidence:
            raise httpx.ReadTimeout(f"synthetic sensitive upstream: {self.key.bearer}")

        response = await self.request(headers={"Authorization": f"Bearer {self.key.bearer}"},
                                      app=self.isolated_app(scopes=frozenset({"services:read"}), verifier=verifier))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "authorization_unavailable")
        self.assertNotIn(self.key.bearer, response.text)

    async def test_tls_required_by_isolated_api_dependency(self) -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.isolated_app()), base_url="http://test") as client:
            response = await client.get("/protected", headers={"Authorization": f"Bearer {self.key.bearer}"})
        self.assertEqual(response.status_code, 400)

    async def test_disable_during_verification_prevents_authorization(self) -> None:
        async def verifier(authority: IntegrationAuthority) -> IntegrationPGEvidence:
            self.assertFalse(self.session.in_transaction())
            await configure_integration(self.session, self.staff, self.integration.id,
                                        status="disabled", grants=ALL_SCOPES)
            return self.evidence()

        response = await self.request(headers={"Authorization": f"Bearer {self.key.bearer}"},
                                      app=self.isolated_app(scopes=frozenset({"services:read"}), verifier=verifier))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "integration_disabled")

    async def test_repeated_rotation_claims_old_credential_once(self) -> None:
        async def rotate() -> CredentialIssuance | str:
            async with self.sessions() as session:
                try:
                    return await rotate_credential(session, self.staff, self.integration.id, self.key.credential.selector)
                except IntegrationError as error:
                    return error.code

        first = await rotate()
        second = await rotate()
        self.assertNotIsInstance(first, str)
        self.assertEqual(second, "invalid_credential")
        self.assertEqual(len((await self.session.scalars(select(IntegrationCredential))).all()), 2)


if __name__ == "__main__":
    unittest.main()
