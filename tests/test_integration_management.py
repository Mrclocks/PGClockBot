"""Administrative isolation, atomic auditing and one-time management replay behavior."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

import test_integration_foundation as foundation
from app.db.integration_audit_models import IntegrationAdminAudit, IntegrationManagementAction
from app.db.integration_models import Integration, IntegrationCredential, IntegrationPlan
from app.db.models import OrgPrincipal, Plan, ResellerProfile
from app.services.integration_admin_audit import AUDIT_PAGE_SIZE, RETENTION_BATCH_SIZE, management_audit_page, prune_management_audit
from app.services.integration_credentials import issue_credential, read_credential, rotate_credential, revoke_credential
from app.services.integration_management import ManagementResult, execute_management
from app.services.integration_management_forms import (
    FORM_LIFETIME_SECONDS, ManagementAction, ManagementChange, ManagementTarget,
    management_signer, new_management_ticket, parse_management_form,
)
from app.services.integration_management_views import integration_management_detail, integration_management_list
from app.services.integration_owners import management_owner
from app.services.integration_policy import IntegrationError
from app.services.integrations import configure_integration, create_integration, set_integration_plans


class ManagementDatabaseCase(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = foundation.IntegrationFoundationTests.asyncSetUp
    asyncTearDown = foundation.IntegrationFoundationTests.asyncTearDown
    new_integration = foundation.IntegrationFoundationTests.new_integration
    change = foundation.IntegrationFoundationTests.change

    def target(self, action: str, *, integration_id: str | None = None, selector: str = "") -> ManagementTarget:
        return ManagementTarget(ManagementAction(action), None if action == "create" else integration_id or self.integration.id, selector)

    async def ticket(self, target: ManagementTarget, *, staff: dict | None = None) -> str:
        owner = await management_owner(self.session, staff or self.staff)
        return new_management_ticket(management_signer(b"synthetic-management-secret"), owner, target)

    async def execute(self, target: ManagementTarget, change: ManagementChange | None = None,
                      *, ticket: str | None = None, staff: dict | None = None) -> ManagementResult:
        return await execute_management(self.session, staff or self.staff, target, change or ManagementChange(confirmed=True),
            signer=management_signer(b"synthetic-management-secret"), ticket=ticket or await self.ticket(target, staff=staff))


class IntegrationManagementTests(ManagementDatabaseCase):
    async def test_create_disabled_with_fixed_authenticated_owner_and_shop_and_atomic_audit(self) -> None:
        result = await self.execute(self.target("create"))
        row = await self.session.get(Integration, result.integration_id)
        self.assertEqual((row.owner_principal_id, row.shop_id, row.status), (11, 11, "disabled"))
        event = await self.session.scalar(select(IntegrationAdminAudit))
        self.assertEqual((event.actor_principal_id, event.shop_id, event.integration_id), (11, 11, row.id))
        self.assertEqual(event.after, {"status": "disabled", "grants": [], "customer_entry_policy": "denied"})

    async def test_own_integrations_are_manageable_without_cross_integration_credential_access(self) -> None:
        other = await self.new_integration(self.staff)
        for action in ("rotate", "revoke"):
            target = self.target(action, integration_id=other.id, selector=self.key.credential.selector)
            with self.assertRaises(IntegrationError) as caught:
                await self.execute(target)
            self.assertEqual(caught.exception.code, "not_found")
        await self.execute(self.target("configure", integration_id=other.id))
        self.assertEqual((await self.session.get(Integration, other.id)).status, "disabled")
        self.assertEqual((await read_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)).status, "active")

    async def test_cross_shop_owner_and_parent_visibility_are_denied_without_foreign_audit(self) -> None:
        other = await self.new_integration(self.other_staff)
        missing = "intg_00000000-0000-0000-0000-000000000000"
        for integration_id in (other.id, missing):
            with self.assertRaises(IntegrationError) as caught:
                await self.execute(self.target("configure", integration_id=integration_id))
            self.assertEqual(caught.exception.code, "not_found")
        child = await self.new_integration({"role": "principal", "web_identity_id": 13})
        with self.assertRaises(IntegrationError):
            await management_audit_page(self.session, self.staff, child.id)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationAdminAudit)), 0)

    async def test_missing_management_permission_blocks_reads_and_mutations(self) -> None:
        target = self.target("configure")
        ticket = await self.ticket(target)
        await self.change(ResellerProfile, ResellerProfile.id == 11, web_permissions="integration_services_read")
        for call in (integration_management_list(self.session, self.staff),
                     integration_management_detail(self.session, self.staff, self.integration.id),
                     management_audit_page(self.session, self.staff, self.integration.id),
                     self.execute(target, ticket=ticket)):
            with self.assertRaises(IntegrationError) as caught:
                await call
            self.assertEqual(caught.exception.code, "permission_denied")
            await self.session.rollback()

    async def test_inactive_owner_and_required_parent_reject_a_previously_issued_ticket(self) -> None:
        target = self.target("configure")
        ticket = await self.ticket(target)
        for principal_id in (11, 1):
            await self.change(OrgPrincipal, OrgPrincipal.id == principal_id, status="disabled")
            with self.assertRaises(IntegrationError):
                await self.execute(target, ticket=ticket)
            await self.change(OrgPrincipal, OrgPrincipal.id == principal_id, status="active")
        child_staff = {"role": "principal", "web_identity_id": 13}
        child = await self.new_integration(child_staff)
        child_target = self.target("configure", integration_id=child.id)
        child_ticket = await self.ticket(child_target, staff=child_staff)
        await self.change(OrgPrincipal, OrgPrincipal.id == 11, status="disabled")
        with self.assertRaises(IntegrationError):
            await self.execute(child_target, ticket=child_ticket, staff=child_staff)

    async def test_allowed_plans_recheck_fixed_shop_and_eligibility(self) -> None:
        target = self.target("plans")
        await self.execute(target, ManagementChange(plan_ids=frozenset({11}), confirmed=True))
        for plan_id in (1, 12, 14, 15, 16, 17, 18):
            with self.assertRaises(IntegrationError):
                await self.execute(target, ManagementChange(plan_ids=frozenset({plan_id}), confirmed=True))
        self.assertEqual(list(await self.session.scalars(select(IntegrationPlan.plan_id))), [11])
        detail = await integration_management_detail(self.session, self.staff, self.integration.id)
        self.assertEqual({plan.id for plan in detail.available_plans}, {11})
        await self.change(Plan, Plan.id == 11, is_active=False)
        with self.assertRaises(IntegrationError):
            await self.execute(target, ManagementChange(plan_ids=frozenset({11}), confirmed=True))

    async def test_retired_status_is_terminal_and_disallows_new_credentials(self) -> None:
        await self.execute(self.target("configure"), ManagementChange(status="retired", confirmed=True))
        for action, change in (("configure", ManagementChange(status="active", confirmed=True)),
                               ("issue", ManagementChange(confirmed=True))):
            with self.assertRaises(IntegrationError):
                await self.execute(self.target(action), change)
        self.assertEqual((await self.session.get(Integration, self.integration.id)).status, "retired")

    async def test_issue_and_rotate_return_secret_once_without_audit_or_ordinary_metadata_leaks(self) -> None:
        result = await self.execute(self.target("issue"), ManagementChange(scopes=foundation.READ_SCOPES, confirmed=True))
        rotated = await self.execute(self.target("rotate", selector=result.issuance.credential.selector))
        for issued in (result, rotated):
            bearer = issued.issuance.bearer
            self.assertNotIn(bearer, repr(issued))
            metadata = await read_credential(self.session, self.staff, self.integration.id, issued.issuance.credential.selector)
            self.assertNotIn("secret", json.dumps(asdict(metadata), default=str))
            self.assertNotIn(bearer, json.dumps(asdict(metadata), default=str))
        events = list(await self.session.scalars(select(IntegrationAdminAudit)))
        for event in events:
            data = json.dumps({key: value for key, value in vars(event).items() if not key.startswith("_")}, default=str)
            self.assertNotIn("pgint_", data)
            for digest in await self.session.scalars(select(IntegrationCredential.secret_digest)):
                self.assertNotIn(digest, data)

    async def test_duplicate_create_configure_plans_issue_rotate_and_revoke_never_mutate_twice(self) -> None:
        for action in ManagementAction:
            target = self.target(action.value, selector=self.key.credential.selector if action in {
                ManagementAction.ROTATE, ManagementAction.REVOKE} else "")
            change = ManagementChange(scopes=foundation.ALL_SCOPES, confirmed=True)
            ticket = await self.ticket(target)
            await self.execute(target, change, ticket=ticket)
            count = await self.session.scalar(select(func.count()).select_from(IntegrationCredential))
            with self.assertRaises(IntegrationError) as caught:
                await self.execute(target, change, ticket=ticket)
            self.assertEqual(caught.exception.code, "already_submitted")
            self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationCredential)), count)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationAdminAudit)), 6)

    async def test_tickets_are_bound_to_actor_integration_action_and_selector(self) -> None:
        target = self.target("issue")
        ticket = await self.ticket(target)
        second = await self.new_integration(self.staff)
        targets = (self.target("issue", integration_id=second.id), self.target("configure"),
                   self.target("revoke", selector=self.key.credential.selector))
        for altered in targets:
            with self.assertRaises(IntegrationError) as caught:
                await self.execute(altered, ticket=ticket)
            self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(IntegrationError):
            await self.execute(target, ticket=ticket+"tampered")
        with self.assertRaises(IntegrationError):
            await self.execute(target, ticket=ticket, staff=self.other_staff)

    async def test_expired_ticket_cannot_issue_a_secret(self) -> None:
        target = self.target("issue")
        with patch("itsdangerous.timed.time.time", return_value=1000):
            ticket = await self.ticket(target)
        with patch("itsdangerous.timed.time.time", return_value=1001+FORM_LIFETIME_SECONDS):
            with self.assertRaises(IntegrationError):
                await self.execute(target, ticket=ticket)

    async def test_explicit_confirmation_is_required_and_safely_audited(self) -> None:
        with self.assertRaises(IntegrationError) as caught:
            await self.execute(self.target("revoke", selector=self.key.credential.selector), ManagementChange())
        self.assertEqual(caught.exception.code, "confirmation_required")
        event = await self.session.scalar(select(IntegrationAdminAudit))
        self.assertEqual((event.outcome, event.error_code), ("denied", "confirmation_required"))
        self.assertEqual((await read_credential(self.session, self.staff, self.integration.id, self.key.credential.selector)).status, "active")

    async def test_every_mutation_rolls_back_when_audit_persistence_fails(self) -> None:
        await self.execute(self.target("plans"), ManagementChange(plan_ids=frozenset({11}), confirmed=True))
        for action in ManagementAction:
            with self.subTest(action=action):
                target = self.target(action.value, selector=self.key.credential.selector if action in {
                    ManagementAction.ROTATE, ManagementAction.REVOKE} else "")
                ticket = await self.ticket(target)
                before = await self.database_snapshot()
                with patch("app.services.integration_management.append_admin_audit", AsyncMock(side_effect=SQLAlchemyError("synthetic storage fault"))):
                    with self.assertRaises(SQLAlchemyError):
                        await self.execute(target, ManagementChange(confirmed=True), ticket=ticket)
                self.assertEqual(await self.database_snapshot(), before)
                await self.execute(target, ManagementChange(scopes=foundation.ALL_SCOPES, confirmed=True), ticket=ticket)

    async def database_snapshot(self) -> tuple:
        values = []
        for table in (Integration, IntegrationPlan, IntegrationCredential, IntegrationManagementAction, IntegrationAdminAudit):
            result = await self.session.execute(select(table.__table__).order_by(*table.__table__.primary_key))
            values.append(tuple(tuple(json.dumps(value, sort_keys=True, default=str) for value in row) for row in result))
        return tuple(values)

    async def test_foundation_no_commit_boundary_rolls_back_and_default_callers_still_commit(self) -> None:
        before = await self.database_snapshot()
        await configure_integration(self.session, self.staff, self.integration.id, status="disabled", grants=frozenset(), commit=False)
        await self.session.rollback()
        self.assertEqual(await self.database_snapshot(), before)
        await set_integration_plans(self.session, self.staff, self.integration.id, plan_ids=frozenset({11}), commit=False)
        await self.session.rollback()
        self.assertEqual(await self.database_snapshot(), before)
        for call in (create_integration(self.session, self.staff, commit=False),
                     issue_credential(self.session, self.staff, self.integration.id, commit=False),
                     rotate_credential(self.session, self.staff, self.integration.id, self.key.credential.selector, commit=False),
                     revoke_credential(self.session, self.staff, self.integration.id, self.key.credential.selector, commit=False)):
            await call
            await self.session.rollback()
            self.assertEqual(await self.database_snapshot(), before)
        created = await create_integration(self.session, self.staff)
        async with self.sessions() as other:
            self.assertIsNotNone(await other.get(Integration, created.id))

    async def test_audit_pagination_is_bounded_scoped_and_stable(self) -> None:
        for _ in range(AUDIT_PAGE_SIZE+2):
            await self.execute(self.target("configure"))
        other = await self.new_integration(self.other_staff)
        await self.execute(self.target("configure", integration_id=other.id), staff=self.other_staff)
        rows = await management_audit_page(self.session, self.staff, self.integration.id)
        self.assertEqual(len(rows), AUDIT_PAGE_SIZE)
        older = await management_audit_page(self.session, self.staff, self.integration.id, before=rows[-1].id)
        self.assertEqual(len(older), 2)
        owner_rows = await management_audit_page(self.session, self.staff)
        self.assertEqual(len(owner_rows), AUDIT_PAGE_SIZE)
        self.assertEqual({row.actor_principal_id for row in owner_rows}, {11})
        self.assertFalse({row.id for row in rows} & {row.id for row in older})
        self.assertEqual({row.integration_id for row in (*rows, *older)}, {self.integration.id})

    async def test_retention_is_bounded_preserves_held_evidence_and_durable_replay_barrier(self) -> None:
        target = self.target("issue")
        ticket = await self.ticket(target)
        await self.execute(target, ticket=ticket)
        original = await self.session.scalar(select(IntegrationAdminAudit))
        old = datetime.now(timezone.utc)-timedelta(days=181)
        await self.session.execute(update(IntegrationAdminAudit).values(created_at=old))
        action = await self.session.scalar(select(IntegrationManagementAction))
        for n in range(RETENTION_BATCH_SIZE+1):
            key = f"{n+1:032x}"
            self.session.add(IntegrationManagementAction(id=key, actor_principal_id=11, shop_id=11,
                scope_kind="shop", integration_id=self.integration.id, action="configure"))
        await self.session.flush()
        self.session.add_all(IntegrationAdminAudit(action_id=f"{n+1:032x}", actor_principal_id=11, shop_id=11,
            scope_kind="shop", integration_id=self.integration.id, action="configure", outcome="succeeded",
            created_at=old, evidence_hold=n == RETENTION_BATCH_SIZE) for n in range(RETENTION_BATCH_SIZE+1))
        await self.session.commit()
        self.assertEqual(await prune_management_audit(self.session), RETENTION_BATCH_SIZE)
        self.assertEqual(await prune_management_audit(self.session), 1)
        remaining = list(await self.session.scalars(select(IntegrationAdminAudit)))
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].evidence_hold)
        self.assertIsNotNone(await self.session.get(IntegrationManagementAction, action.id))
        with self.assertRaises(IntegrationError) as caught:
            await self.execute(target, ticket=ticket)
        self.assertEqual(caught.exception.code, "already_submitted")
        self.assertIsNone(await self.session.get(IntegrationAdminAudit, original.id, populate_existing=True))

    async def test_invalid_enums_scopes_numbers_and_oversized_changes_are_rejected(self) -> None:
        changes = [ManagementChange(status="<script>", confirmed=True), ManagementChange(scopes=frozenset({"*"}), confirmed=True),
                   ManagementChange(lifetime_days=True, confirmed=True), ManagementChange(lifetime_days=366, confirmed=True),
                   ManagementChange(overlap_minutes=1441, confirmed=True), ManagementChange(plan_ids=frozenset({True}), confirmed=True),
                   ManagementChange(plan_ids=frozenset(range(1, 102)), confirmed=True)]
        changes.extend(ManagementChange(customer_entry_policy=value, confirmed=True)
                       for value in (False, None, ["website"], {"website": True}, "unknown"))
        for change in changes:
            with self.assertRaises(IntegrationError):
                await self.execute(self.target("configure"), change)
        for form in ({"owner_principal_id": "12"}, {"shop_id": "12"}, {"scopes": "x"*513}, {"plan_ids": "1 "*101},
                     {"lifetime_days": "NaN"}, {"lifetime_days": "-1"}, {"lifetime_days": "1e3"}):
            with self.assertRaises(IntegrationError):
                parse_management_form(form, ManagementAction.ISSUE)

    async def test_lists_and_credential_pages_are_bounded_and_only_current_owner(self) -> None:
        for _ in range(26):
            await create_integration(self.session, self.staff)
            await issue_credential(self.session, self.staff, self.integration.id)
        await self.new_integration(self.other_staff)
        _, rows = await integration_management_list(self.session, self.staff)
        self.assertEqual(len(rows), 25)
        _, second = await integration_management_list(self.session, self.staff, after=rows[-1].id)
        self.assertEqual(len(second), 2)
        self.assertEqual({row.owner_principal_id for row in (*rows, *second)}, {11})
        detail = await integration_management_detail(self.session, self.staff, self.integration.id)
        self.assertEqual(len(detail.credentials), 25)
        older = await integration_management_detail(self.session, self.staff, self.integration.id,
                                                    credential_after=detail.credentials[-1].selector)
        self.assertEqual(len(older.credentials), 2)
        self.assertFalse({row.selector for row in detail.credentials} & {row.selector for row in older.credentials})

    async def test_plan_updates_do_not_change_another_integration_in_the_same_shop(self) -> None:
        other = await self.new_integration(self.staff)
        for integration_id in (self.integration.id, other.id):
            await self.execute(self.target("plans", integration_id=integration_id), ManagementChange(plan_ids=frozenset({11}), confirmed=True))
        await self.execute(self.target("plans"))
        self.assertEqual(list(await self.session.scalars(select(IntegrationPlan.plan_id).where(IntegrationPlan.integration_id == other.id))), [11])
        self.assertEqual(list(await self.session.scalars(select(IntegrationPlan.plan_id).where(IntegrationPlan.integration_id == self.integration.id))), [])

    async def test_invalid_page_cursors_and_plan_counts_are_rejected(self) -> None:
        for cursor in (-1, True, 2**31, float("nan")):
            with self.assertRaises(IntegrationError):
                await management_audit_page(self.session, self.staff, self.integration.id, before=cursor)
            with self.assertRaises(IntegrationError):
                await integration_management_detail(self.session, self.staff, self.integration.id, plan_after=cursor)
        with self.assertRaises(IntegrationError):
            await integration_management_detail(self.session, self.staff, self.integration.id, credential_after="x"*10000)
