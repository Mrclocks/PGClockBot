"""Operational forms use real cookie/CSRF middleware and isolate one-time signing keys."""

from __future__ import annotations

import re
from unittest.mock import patch

from sqlalchemy import func, select, update

from app.db.integration_webhook_models import IntegrationControlAction, IntegrationWebhookKey
from app.db.models import ResellerProfile
from test_integration_management_pages import IntegrationManagementPageFixture


class IntegrationControlPageTests(IntegrationManagementPageFixture):
    async def test_control_page_and_one_time_key_require_csrf_and_current_permission(self) -> None:
        profile = await self.session.get(ResellerProfile, 11)
        profile.web_permissions += ",integration_operations_read,integration_webhooks_manage"
        await self.session.commit()
        base = f"/integrations/{self.integration.id}"
        path, action = base+"/operations", base+"/controls/signing_key"
        form = await self.form(path, action)
        invalid = await self.client.post(action, data={**form, "csrf_token": "wrong", "confirm": "yes"})
        self.assertEqual(invalid.status_code, 403)
        with patch("app.services.secret_box._root_secret", return_value=b"synthetic-webhook-key-root-000000"):
            response = await self.client.post(action, data={**form, "confirm": "yes", "overlap_minutes": "0"})
        self.assertEqual(response.status_code, 200, response.text[:500])
        secret = re.search(r">([0-9a-f]{64})</textarea>", response.text).group(1)
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        repeated = await self.client.post(action, data={**form, "confirm": "yes"})
        self.assertEqual(repeated.status_code, 409)
        self.assertNotIn(secret, repeated.text)
        page = await self.client.get(path)
        self.assertNotIn(secret, page.text)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationWebhookKey)), 1)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationControlAction)), 1)
        await self.session.execute(update(ResellerProfile).where(ResellerProfile.id == 11).values(web_permissions="integrations_manage"))
        await self.session.commit()
        self.assertEqual((await self.client.get(path)).status_code, 403)

    async def test_other_shop_cannot_view_operations_or_reuse_a_control_ticket(self) -> None:
        profile = await self.session.get(ResellerProfile, 11)
        profile.web_permissions += ",integration_operations_read,integration_webhooks_manage"
        await self.session.commit()
        other = await self.new_integration(self.other_staff)
        base = f"/integrations/{self.integration.id}"
        action = base+"/controls/signing_key"
        form = await self.form(base+"/operations", action)
        self.assertEqual((await self.client.get(f"/integrations/{other.id}/operations")).status_code, 403)
        response = await self.client.post(f"/integrations/{other.id}/controls/signing_key", data={**form, "confirm": "yes"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(IntegrationWebhookKey)), 0)
