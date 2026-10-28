"""Exercise management through the real session, origin, CSRF and HTML rendering stack."""

from __future__ import annotations

from html.parser import HTMLParser
import logging
from io import StringIO
from unittest.mock import AsyncMock, patch

import httpx
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from test_integration_management import ManagementDatabaseCase
from app.api import app as panel
from app.db.integration_audit_models import IntegrationAdminAudit
from app.db.integration_models import Integration, IntegrationCredential
from app.services.formatting import format_expire_short
from app.db.models import OrgPrincipalWebIdentity, Plan, PurchaseContact, ResellerProfile, Setting

SECRET = "synthetic-panel-secret-"*4


class Forms(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.forms: dict[str, dict] = {}
        self.current: dict | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self.current = {}
            self.forms[values.get("action", "")] = self.current
        elif tag == "input" and self.current is not None and values.get("type") == "hidden":
            self.current[values["name"]] = values.get("value", "")

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.current = None


class IntegrationManagementPageFixture(ManagementDatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.patches = [
            patch.object(panel, "SessionLocal", self.sessions),
            patch.object(panel, "ensure_web_secret", return_value=SECRET),
            patch.object(panel, "is_setup_complete", return_value=True),
            patch("app.services.pasarguard.get_pg_for_principal", AsyncMock(return_value=None)),
            patch("app.services.panel_sidebar_cache.peek_sidebar_counts", return_value=(0, False, False)),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.app = panel.create_api_app()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://panel.test",
            cookies={"session": self.cookie(), "csrf": "synthetic-csrf"}, headers={"Origin": "https://panel.test"})

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    def cookie(self, **changes: object) -> str:
        payload = {"role": "principal", "username": "principal11", "web_identity_id": 11, "pv": "synthetic-hash"}
        payload.update(changes)
        return URLSafeTimedSerializer(SECRET, salt="pgclock-session").dumps(payload)

    async def form(self, path: str, action: str) -> dict:
        response = await self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text[:500])
        return Forms(response.text).forms[action]

class IntegrationManagementPageTests(IntegrationManagementPageFixture):
    async def test_authorized_list_create_details_and_disabled_global_switch(self) -> None:
        form = await self.form("/integrations", "/integrations")
        response = await self.client.post("/integrations", data=form)
        self.assertEqual(response.status_code, 303, response.text[:500])
        page = await self.client.get(response.headers["location"])
        self.assertEqual(page.status_code, 200, page.text[:500])
        self.assertIn("غیرفعال", page.text)
        await self.session.execute(update(Setting).where(Setting.key == "integration_api_enabled").values(value="0"))
        await self.session.commit()
        self.assertEqual((await self.client.get("/integrations")).status_code, 200)
        for path in ("/api/v1/capabilities", "/api/v1/plans", "/api/v1/auth-test"):
            self.assertEqual((await self.client.get(path)).status_code, 404)
        self.assertEqual(await self.session.scalar(select(Setting.value).where(Setting.key == "integration_api_enabled")), "0")

    async def test_missing_invalid_csrf_and_cross_origin_have_no_side_effects(self) -> None:
        form = await self.form("/integrations", "/integrations")
        for data, headers in (({k: v for k, v in form.items() if k != "csrf_token"}, {}),
                              ({**form, "csrf_token": "wrong"}, {}),
                              (form, {"Origin": "https://evil.test"}),
                              (form, {"Origin": ""})):
            response = await self.client.post("/integrations", data=data, headers=headers)
            self.assertEqual(response.status_code, 403, response.text[:500])
            self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(Integration)), 1)

    async def test_existing_signed_session_binding_revocation_and_no_cookie_behavior(self) -> None:
        for cookie in ("invalid-cookie", self.cookie(pv="wrong"), self.cookie(org_principal_id=12), self.cookie(role="user")):
            self.client.cookies.set("session", cookie)
            response = await self.client.get("/integrations")
            self.assertEqual(response.status_code, 303)
            self.assertTrue(response.headers["location"].startswith("/login"))
        self.client.cookies.clear()
        self.assertEqual((await self.client.get("/integrations")).status_code, 303)
        self.client.cookies.set("session", self.cookie())
        await self.session.execute(update(OrgPrincipalWebIdentity).where(OrgPrincipalWebIdentity.id == 11).values(is_active=False))
        await self.session.commit()
        self.assertEqual((await self.client.get("/integrations")).status_code, 303)

    async def test_live_permission_removal_denies_all_management_reads(self) -> None:
        await self.change(ResellerProfile, ResellerProfile.id == 11, web_permissions="plans")
        for path in ("/integrations", "/integrations/audit", f"/integrations/{self.integration.id}", f"/integrations/{self.integration.id}/audit"):
            response = await self.client.get(path)
            self.assertEqual(response.status_code, 403, response.text[:500])

    async def test_cross_shop_and_same_owner_cross_integration_action_targets_are_rejected(self) -> None:
        other_shop = await self.new_integration(self.other_staff)
        for path in (f"/integrations/{other_shop.id}", f"/integrations/{other_shop.id}/audit"):
            self.assertEqual((await self.client.get(path)).status_code, 404)
        other = await self.new_integration(self.staff)
        path = f"/integrations/{self.integration.id}"
        action = f"{path}/credentials/{self.key.credential.selector}/revoke"
        form = await self.form(path, action)
        wrong = f"/integrations/{other.id}/credentials/{self.key.credential.selector}/revoke"
        response = await self.client.post(wrong, data={**form, "confirm": "yes"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationAdminAudit)), 0)

    async def test_owner_shop_fields_cannot_reassign_any_integration(self) -> None:
        path = f"/integrations/{self.integration.id}"
        form = await self.form(path, path+"/configure")
        for extra in ({"shop_id": "12"}, {"owner_principal_id": "12"}, {"integration_id": "other"}, {"scope_kind": "platform"}):
            form = await self.form(path, path+"/configure")
            response = await self.client.post(path+"/configure", data={**form, "confirm": "yes", **extra})
            self.assertEqual(response.status_code, 400)
        row = await self.session.get(Integration, self.integration.id, populate_existing=True)
        self.assertEqual((row.owner_principal_id, row.shop_id, row.status), (11, 11, "active"))

    async def test_secret_response_is_private_accessible_and_one_time_with_safe_audits_and_logs(self) -> None:
        path = f"/integrations/{self.integration.id}"
        form = await self.form(path, path+"/issue")
        output = StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        response = await self.client.post(path+"/issue", data={**form, "confirm": "yes"})
        self.assertEqual(response.status_code, 200, response.text[:500])
        bearer = response.text.split('aria-describedby="once-help">', 1)[1].split("</textarea>")[0]
        self.assertTrue(bearer.startswith("pgint_"))
        self.assertIn('id="one-time-bearer" dir="ltr"', response.text)
        self.assertIn("readonly", response.text)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("private", response.headers["cache-control"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertNotIn("location", response.headers)
        for header in response.headers.values():
            self.assertNotIn(bearer, header)
        replay = await self.client.post(path+"/issue", data={**form, "confirm": "yes"})
        self.assertEqual(replay.status_code, 409)
        for url in ("/integrations", path, path+"/audit"):
            result = await self.client.get(url)
            self.assertEqual(result.status_code, 200, result.text[:500])
            self.assertNotIn(bearer, result.text)
            for digest in await self.session.scalars(select(IntegrationCredential.secret_digest)):
                self.assertNotIn(digest, result.text)
        self.assertNotIn(bearer, replay.text)
        self.assertNotIn(bearer, output.getvalue())
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationCredential)), 2)

    async def test_rotation_posts_return_only_one_replacement_and_revocation_is_immediate(self) -> None:
        path = f"/integrations/{self.integration.id}"
        action = path+f"/credentials/{self.key.credential.selector}/rotate"
        form = await self.form(path, action)
        response = await self.client.post(action, data={**form, "confirm": "yes", "overlap_minutes": "0"})
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual((await self.client.post(action, data={**form, "confirm": "yes"})).status_code, 409)
        selector = response.text.split("pgint_", 1)[1].split(".", 1)[0]
        revoke = path+f"/credentials/{selector}/revoke"
        form = await self.form(path, revoke)
        self.assertEqual((await self.client.post(revoke, data={**form, "confirm": "yes"})).status_code, 303)
        key = await self.session.get(IntegrationCredential, selector, populate_existing=True)
        self.assertEqual(key.status, "revoked")

    async def test_storage_failure_rolls_back_and_returns_safe_unlogged_details(self) -> None:
        path = f"/integrations/{self.integration.id}"
        form = await self.form(path, path+"/configure")
        with patch("app.services.integration_management.append_admin_audit", AsyncMock(side_effect=SQLAlchemyError("sensitive-synthetic-detail"))):
            response = await self.client.post(path+"/configure", data={**form, "status": "disabled", "confirm": "yes"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sensitive-synthetic-detail", response.text)
        row = await self.session.get(Integration, self.integration.id, populate_existing=True)
        self.assertEqual(row.status, "active")
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationAdminAudit)), 0)

    async def test_hostile_text_is_escaped_invalid_inputs_and_oversized_bodies_are_safe(self) -> None:
        hostile = '<script>alert("synthetic")</script>'
        await self.change(Plan, Plan.id == 11, name=hostile)
        path = f"/integrations/{self.integration.id}"
        response = await self.client.get(path)
        self.assertNotIn(hostile, response.text)
        self.assertIn("&lt;script&gt;", response.text)
        form = Forms(response.text).forms[path+"/issue"]
        for invalid in ({"lifetime_days": "NaN"}, {"lifetime_days": "-1"}, {"lifetime_days": "366"}, {"scopes": hostile}, {"scopes": "x"*513}):
            form = await self.form(path, path+"/issue")
            result = await self.client.post(path+"/issue", data={**form, "confirm": "yes", **invalid})
            self.assertEqual(result.status_code, 400)
            self.assertNotIn(hostile, result.text)
        denied = list(await self.session.scalars(select(IntegrationAdminAudit)))
        self.assertEqual(len(denied), 5)
        self.assertTrue(all(event.outcome == "denied" and event.error_code == "invalid_request" for event in denied))
        oversized = await self.client.post(path+"/issue", content=b"x"*(16*1024+1))
        self.assertEqual(oversized.status_code, 413)
        duplicates = await self.client.post(path+"/issue", content="ticket=a&ticket=b&csrf_token=synthetic-csrf",
                                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(duplicates.status_code, 400)
        self.assertEqual((await self.client.get(path+"/audit?before=-1")).status_code, 400)

    async def test_existing_origin_csrf_and_body_replay_still_accept_valid_panel_mutations(self) -> None:
        path = f"/integrations/{self.integration.id}"
        form = await self.form(path, path+"/plans")
        response = await self.client.post(path+"/plans", data={**form, "confirm": "yes", "plan_ids": "11"})
        self.assertEqual(response.status_code, 303, response.text[:500])
        form = await self.form(path, path+"/plans")
        response = await self.client.post(path+"/plans", data={**form, "confirm": "yes", "plan_ids": "12"})
        self.assertEqual(response.status_code, 400)
        event = await self.session.scalar(select(IntegrationAdminAudit).where(IntegrationAdminAudit.outcome == "denied"))
        self.assertEqual((event.shop_id, event.error_code), (11, "plan_ineligible"))

    async def test_one_time_secrets_require_https_and_do_not_trust_a_forwarded_header(self) -> None:
        path = f"/integrations/{self.integration.id}"
        for action in (path+"/issue", path+f"/credentials/{self.key.credential.selector}/rotate"):
            form = await self.form(path, action)
            response = await self.client.post("http://panel.test"+action, data={**form, "confirm": "yes"},
                headers={"Origin": "http://panel.test", "X-Forwarded-Proto": "https"})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("pgint_", response.text)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationCredential)), 1)

    async def test_credential_metadata_displays_the_actual_expiry(self) -> None:
        response = await self.client.get(f"/integrations/{self.integration.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(format_expire_short(self.key.credential.expires_at.isoformat()), response.text)

    async def test_owner_history_includes_failed_creation_without_disclosing_another_owner(self) -> None:
        form = await self.form("/integrations", "/integrations")
        response = await self.client.post("/integrations", data={**form, "shop_id": "12"})
        self.assertEqual(response.status_code, 400)
        other = await self.new_integration(self.other_staff)
        await self.execute(self.target("configure", integration_id=other.id), staff=self.other_staff)
        history = await self.client.get("/integrations/audit")
        self.assertEqual(history.status_code, 200)
        self.assertIn("ایجاد یکپارچه‌سازی انجام نشد", history.text)
        self.assertNotIn(other.id, history.text)
        self.assertNotIn("pgint_", history.text)

    async def test_explicit_policy_is_protected_by_csrf_confirmation_scope_and_atomic_audit(self) -> None:
        path = f"/integrations/{self.integration.id}"
        action = f"{path}/configure"
        form = await self.form(path, action)
        data = {**form, "status": "active", "scopes": "customers:read", "customer_entry_policy": "website", "confirm": "yes"}
        without_csrf = {key: value for key, value in data.items() if key != "csrf_token"}
        assert (await self.client.post(action, data=without_csrf)).status_code == 403
        assert (await self.client.post(action, data=data)).status_code == 303
        self.session.expire_all()
        assert await self.session.scalar(select(Integration.customer_entry_policy).where(Integration.id == self.integration.id)) == "website"
        audit = (await self.session.scalars(select(IntegrationAdminAudit))).one()
        assert audit.before["customer_entry_policy"] == "denied"
        assert audit.after["customer_entry_policy"] == "website"
        assert await self.session.scalar(select(func.count()).select_from(PurchaseContact)) == 0
        # Audit failure rolls back both the policy and the permanent form claim.
        data = {**await self.form(path, action), "status": "active", "scopes": "customers:read",
                "customer_entry_policy": "denied", "confirm": "yes"}
        with patch("app.services.integration_management.append_admin_audit", AsyncMock(side_effect=SQLAlchemyError("synthetic"))):
            response = await self.client.post(action, data=data)
        assert response.status_code == 503
        self.session.expire_all()
        assert await self.session.scalar(select(Integration.customer_entry_policy).where(Integration.id == self.integration.id)) == "website"
        assert await self.session.scalar(select(func.count()).select_from(IntegrationAdminAudit)) == 1
        assert (await self.client.post(action, data=data)).status_code == 303
