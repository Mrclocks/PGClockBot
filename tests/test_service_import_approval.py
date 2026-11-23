"""«افزودن سرویس قبلی» with admin approval: queueing, deciding, isolation, idempotency, endpoints."""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch


import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, update

from app.db.models import BotUser, ServiceImportRequest, UserService
from app.services import service_import as si
from app.services.pasarguard import PasarGuardError
from app.services.service_import import ImportOutcome, ServiceImportError
from app.services.service_import_requests import approve_request, list_requests, pending_count, reject_request
from app.services.service_import_store import MAX_OPEN_PER_USER
from app.services.users import clear_settings_cache, set_settings_bulk
from tests.test_service_import import ImportCase
from tests.test_service_import_settings_tab import TabHarness
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION
from tests.postgres_support import scoped_sync_engine

APPROVAL = {"service_import_enabled": "1", "service_import_mode": "approval"}


class ApprovalCase(ImportCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for token, user in self.platform_users.items():
            user["subscription_url"] = f"https://p.example/sub/{token}"
        async with self.Session() as session:
            await set_settings_bulk(session, APPROVAL)
            await set_settings_bulk(session, APPROVAL, reseller_id=5)
        clear_settings_cache()

    async def requests(self) -> list[ServiceImportRequest]:
        return await self.rows(ServiceImportRequest)

    async def approve(self, request_id: int, scope: int | None = None):
        async with self.Session() as session:
            return await approve_request(session, request_id, scope, decided_by="admin:test")

    async def reject(self, request_id: int, scope: int | None = None):
        async with self.Session() as session:
            return await reject_request(session, request_id, scope, decided_by="admin:test")


class QueueTests(ApprovalCase):
    async def test_a_valid_link_waits_for_approval_and_nothing_is_attached(self):
        result = await self.run_import(1, "https://p.example/sub/tok-plat-0000001")
        self.assertEqual((result.outcome, result.service), (ImportOutcome.PENDING_APPROVAL, None))
        self.assertEqual(await self.rows(), [])
        (req,) = await self.requests()
        self.assertEqual((req.bot_user_id, req.shop_id, req.panel_user_id, req.status), (1, None, 11, "pending"))

    async def test_the_token_is_never_stored(self):
        await self.run_import(1, "https://p.example/sub/tok-plat-0000001")
        (req,) = await self.requests()
        self.assertNotIn("tok-plat-0000001", repr(vars(req)))

    async def test_asking_twice_keeps_one_request(self):
        await self.run_import(1, "tok-plat-0000001")
        await self.run_import(1, "https://p.example/sub/tok-plat-0000001")
        self.assertEqual(len(await self.requests()), 1)

    async def test_a_refused_link_never_creates_a_request(self):
        await self.refused(1, "tok-shop-0000001")
        await self.refused(1, "tok-unknown-0000000")
        self.assertEqual(await self.requests(), [])

    async def test_instant_mode_still_attaches_at_once(self):
        async with self.Session() as session:
            await set_settings_bulk(session, {"service_import_mode": "instant"})
        clear_settings_cache()
        self.assertEqual((await self.run_import(1, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)
        self.assertEqual(await self.requests(), [])

    async def test_view_only_links_ignore_approval(self):
        result = await self.run_import(1, "tok-plat-0000001", renewable=False)
        self.assertEqual(result.outcome, ImportOutcome.ADDED)

    async def test_waiting_requests_per_customer_are_capped(self):
        for index in range(MAX_OPEN_PER_USER):
            name = f"extra-{index}"
            self.platform_users[f"tok-extra-{index:08d}"] = {
                "id": 100 + index, "username": name, "admin": {"username": "platformadmin"},
                "subscription_url": f"https://p.example/sub/tok-extra-{index:08d}",
            }
            await self.run_import(1, f"tok-extra-{index:08d}")
        with self.assertRaises(ServiceImportError) as caught:
            await self.run_import(1, "tok-plat-0000001")
        self.assertEqual(caught.exception.reason, si.Reason.REQUEST_LIMIT)
        self.assertEqual(len(await self.requests()), MAX_OPEN_PER_USER)

    async def test_a_service_already_taken_is_not_queued(self):
        async with self.Session() as session:
            await set_settings_bulk(session, {"service_import_mode": "instant"})
        clear_settings_cache()
        await self.run_import(2, "tok-plat-0000001")
        async with self.Session() as session:
            await set_settings_bulk(session, APPROVAL)
        clear_settings_cache()
        await self.refused(1, "tok-plat-0000001")
        self.assertEqual(await self.requests(), [])


class DecisionTests(ApprovalCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.run_import(1, "tok-plat-0000001")
        await self.run_import(6, "tok-shop-0000001", shop_id=5)
        self.platform_req, self.shop_req = sorted(await self.requests(), key=lambda r: r.id)

    async def test_approving_attaches_a_renewable_service(self):
        decision = await self.approve(self.platform_req.id)
        self.assertTrue(decision.approved)
        (service,) = await self.rows()
        self.assertEqual((service.bot_user_id, service.pg_user_id, service.remark), (1, 11, "imported"))
        self.assertEqual(service.subscription_token, "tok-plat-0000001")
        self.assertEqual(decision.service_id, service.id)
        refreshed = {r.id: r for r in await self.requests()}
        self.assertEqual(refreshed[self.platform_req.id].status, "approved")

    async def test_approving_twice_attaches_once(self):
        await self.approve(self.platform_req.id)
        with self.assertRaises(ValueError):
            await self.approve(self.platform_req.id)
        self.assertEqual(len(await self.rows()), 1)

    async def test_simultaneous_approvals_attach_once(self):
        results = await asyncio.gather(
            self.approve(self.platform_req.id), self.approve(self.platform_req.id), return_exceptions=True
        )
        self.assertEqual(sum(1 for r in results if not isinstance(r, BaseException)), 1)
        self.assertEqual(len(await self.rows()), 1)

    async def test_rejecting_attaches_nothing_and_cannot_be_approved_later(self):
        decision = await self.reject(self.platform_req.id)
        self.assertFalse(decision.approved)
        with self.assertRaises(ValueError):
            await self.approve(self.platform_req.id)
        self.assertEqual(await self.rows(), [])

    async def test_a_decision_is_final(self):
        await self.approve(self.platform_req.id)
        with self.assertRaises(ValueError):
            await self.reject(self.platform_req.id)

    async def test_a_shop_cannot_decide_a_platform_request_and_vice_versa(self):
        for scope, request in ((5, self.platform_req), (None, self.shop_req), (99, self.shop_req)):
            with self.subTest(scope=scope, request=request.id):
                with self.assertRaises(ValueError):
                    await self.approve(request.id, scope)
                with self.assertRaises(ValueError):
                    await self.reject(request.id, scope)
        self.assertEqual(await self.rows(), [])
        self.assertEqual({r.status for r in await self.requests()}, {"pending"})

    async def test_lists_and_counts_are_scoped(self):
        async with self.Session() as session:
            platform = await list_requests(session, None)
            shop = await list_requests(session, 5)
            self.assertEqual([r["customer"] for r in platform], [101])
            self.assertEqual([r["customer"] for r in shop], [106])
            self.assertEqual((await pending_count(session, None), await pending_count(session, 5), await pending_count(session, 9)), (1, 1, 0))
        self.assertNotIn("tok-", str(platform) + str(shop))

    async def test_an_expired_request_cannot_be_approved_or_listed(self):
        async with self.Session() as session:
            await session.execute(
                update(ServiceImportRequest).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
            )
            await session.commit()
            self.assertEqual(await list_requests(session, None), [])
        with self.assertRaises(ValueError):
            await self.approve(self.platform_req.id)

    async def test_approval_rechecks_the_owner_admin(self):
        self.platform_users["tok-plat-0000001"]["admin"] = {"username": "someone-else"}
        with self.assertRaises(ServiceImportError) as caught:
            await self.approve(self.platform_req.id)
        self.assertEqual(caught.exception.reason, si.Reason.ADMIN_NOT_ALLOWED)
        self.assertEqual(await self.rows(), [])
        status = {r.id: r.status for r in await self.requests()}[self.platform_req.id]
        self.assertEqual(status, "failed")

    async def test_approval_refuses_a_service_taken_meanwhile(self):
        async with self.Session() as session:
            session.add(UserService(
                bot_user_id=2, pg_user_id=11, pg_username="plat-one", subscription_url="u",
                subscription_token="tok-plat-0000001", remark="imported",
            ))
            await session.commit()
        with self.assertRaises(ServiceImportError) as caught:
            await self.approve(self.platform_req.id)
        self.assertEqual(caught.exception.reason, si.Reason.ALREADY_TAKEN)
        self.assertEqual(len(await self.rows()), 1)

    async def test_approval_refuses_a_blocked_customer(self):
        async with self.Session() as session:
            await session.execute(update(BotUser).where(BotUser.id == 1).values(is_blocked=True))
            await session.commit()
        with self.assertRaises(ServiceImportError):
            await self.approve(self.platform_req.id)

    async def test_a_panel_outage_keeps_the_request_open_for_a_retry(self):
        working = self.platform_panel.get_user_by_username
        self.platform_panel.get_user_by_username = AsyncMock(side_effect=PasarGuardError("down", status_code=500))
        with self.assertRaises(ServiceImportError) as caught:
            await self.approve(self.platform_req.id)
        self.assertEqual(caught.exception.reason, si.Reason.PANEL_ERROR)
        self.assertEqual({r.id: r.status for r in await self.requests()}[self.platform_req.id], "pending")
        self.platform_panel.get_user_by_username = working
        self.assertTrue((await self.approve(self.platform_req.id)).approved)

    async def test_an_interrupted_approval_can_be_resumed_after_its_lease(self):
        async with self.Session() as session:
            await session.execute(
                update(ServiceImportRequest).where(ServiceImportRequest.id == self.platform_req.id)
                .values(status="approving", decided_at=datetime.now(timezone.utc) - timedelta(minutes=30))
            )
            await session.commit()
        self.assertTrue((await self.approve(self.platform_req.id)).approved)
        self.assertEqual(len(await self.rows()), 1)

    async def test_a_fresh_claim_is_not_taken_over(self):
        async with self.Session() as session:
            await session.execute(
                update(ServiceImportRequest).where(ServiceImportRequest.id == self.platform_req.id)
                .values(status="approving", decided_at=datetime.now(timezone.utc))
            )
            await session.commit()
        with self.assertRaises(ValueError):
            await self.approve(self.platform_req.id)


class EndpointTests(TabHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for token, user in self.users.items():
            user["subscription_url"] = f"https://p.example/sub/{token}"
        async with self._session_factory()() as session:
            await set_settings_bulk(session, APPROVAL)
            await set_settings_bulk(session, APPROVAL, reseller_id=5)
            for user_id, link, shop in ((1, "tok-plat-0000001", None), (6, "tok-shop-0000001", 5)):
                user = await session.get(BotUser, user_id)
                await si.import_existing_service(session, user=user, raw_link=link, shop_id=shop)
        clear_settings_cache()
        notify = patch("app.api.service_import_pages.notify_payer", AsyncMock(return_value=True))
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    async def ids(self) -> dict[int | None, int]:
        async with self._session_factory()() as session:
            rows = (await session.scalars(select(ServiceImportRequest))).all()
        return {r.shop_id: r.id for r in rows}

    async def test_each_viewer_lists_only_its_own_requests(self):
        owner = (await self.call(OWNER_SESSION, "GET", "/service-import/requests")).json()["requests"]
        shop = (await self.call(SHOP_SESSION, "GET", "/service-import/requests")).json()["requests"]
        self.assertEqual([r["customer"] for r in owner], [101])
        self.assertEqual([r["customer"] for r in shop], [106])

    async def test_approving_attaches_and_tells_the_customer(self):
        ids = await self.ids()
        response = await self.call(OWNER_SESSION, "POST", f"/service-import/requests/{ids[None]}/approve")
        self.assertEqual((response.status_code, response.json()), (200, {"ok": True, "approved": True}))
        async with self._session_factory()() as session:
            self.assertEqual(len((await session.scalars(select(UserService))).all()), 1)
        self.assertEqual(self.notify.await_args.args[1], 1)

    async def test_rejecting_tells_the_customer_and_attaches_nothing(self):
        ids = await self.ids()
        response = await self.call(SHOP_SESSION, "POST", f"/service-import/requests/{ids[5]}/reject")
        self.assertEqual(response.json(), {"ok": True, "approved": False})
        async with self._session_factory()() as session:
            self.assertEqual((await session.scalars(select(UserService))).all(), [])
        self.assertEqual(self.notify.await_args.args[1], 6)

    async def test_a_shop_cannot_decide_another_shops_or_the_platforms_request(self):
        ids = await self.ids()
        response = await self.call(SHOP_SESSION, "POST", f"/service-import/requests/{ids[None]}/approve")
        self.assertEqual(response.status_code, 409)
        owner = await self.call(OWNER_SESSION, "POST", f"/service-import/requests/{ids[5]}/approve")
        self.assertEqual(owner.status_code, 409)
        self.notify.assert_not_awaited()

    async def test_deciding_twice_is_refused_and_notifies_once(self):
        ids = await self.ids()
        await self.call(OWNER_SESSION, "POST", f"/service-import/requests/{ids[None]}/approve")
        again = await self.call(OWNER_SESSION, "POST", f"/service-import/requests/{ids[None]}/approve")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.notify.await_count, 1)

    async def test_staff_without_the_permission_cannot_list_or_decide(self):
        ids = await self.ids()
        limited = {"role": "reseller", "bot_user_id": 5, "permissions": ["orders"], "pv": SHOP_SESSION["pv"]}
        # a shop profile that does not hold the key
        async with self._session_factory()() as session:
            from app.db.models import ResellerProfile

            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(web_permissions="orders"))
            await session.commit()
        self.assertEqual((await self.call(limited, "GET", "/service-import/requests")).status_code, 403)
        self.assertEqual((await self.call(limited, "POST", f"/service-import/requests/{ids[5]}/approve")).status_code, 403)

    async def test_unknown_actions_and_other_roles_are_turned_away(self):
        ids = await self.ids()
        self.assertEqual((await self.call(OWNER_SESSION, "POST", f"/service-import/requests/{ids[None]}/delete")).status_code, 404)
        principal = {"role": "principal", "org_principal_id": 2, "org_depth": 1}
        self.assertEqual((await self.call(principal, "GET", "/service-import/requests")).status_code, 303)

    async def test_the_tab_shows_the_mode_and_the_requests_card(self):
        owner = await self.call(OWNER_SESSION, "GET", "/settings?tab=service_import")
        self.assertIn("s_service_import_mode", owner.text)
        self.assertIn('id="si-requests"', owner.text)


class MigrationTests(unittest.TestCase):
    def _run(self, fn) -> None:
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0053_service_import_requests.py"
        spec = importlib.util.spec_from_file_location("requests_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(sa.text("INSERT INTO bot_users (id) VALUES (1)"))
            with Operations.context(MigrationContext.configure(conn)):
                fn(module, conn)

    def test_upgrade_is_idempotent_and_downgrade_removes_the_table(self):
        def check(module, conn):
            module.upgrade()
            module.upgrade()
            insp = sa.inspect(conn)
            self.assertTrue(insp.has_table("service_import_requests"))
            self.assertIn("uq_service_import_requests_open", {i["name"] for i in insp.get_indexes("service_import_requests")})
            conn.execute(sa.text(
                "INSERT INTO service_import_requests (bot_user_id, panel_user_id, panel_username, expires_at) "
                "VALUES (1, 5, 'u', '2030-01-01')"
            ))
            with self.assertRaises(sa.exc.IntegrityError), conn.begin_nested():
                conn.execute(sa.text(
                    "INSERT INTO service_import_requests (bot_user_id, panel_user_id, panel_username, expires_at) "
                    "VALUES (1, 5, 'u', '2030-01-01')"
                ))
            module.downgrade()
            self.assertFalse(sa.inspect(conn).has_table("service_import_requests"))

        self._run(check)


if __name__ == "__main__":
    unittest.main()
