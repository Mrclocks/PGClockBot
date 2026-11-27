"""After a staff revoke, customers' services keep the panel's new subscription link, inside the acting shop."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Base, BotUser, UserService
from app.services.subscription_link_store import LinkScope, PanelLink, link_after_revoke, save_links, store_revoked_link
from tests.postgres_support import scoped_async_engine
from tests.test_customer_services_web_permission import SERVICE_ID, CustomerServicesWebPermissionTests
from tests.test_update_owner_only import OWNER_SESSION

NEW_URL = "https://sub.example/sub/NEWTOKEN123"
NEW_LINK = PanelLink(NEW_URL, "NEWTOKEN123")
OLD_URL = "https://sub.example/sub/OLDTOKEN000"
SHOP_A, SHOP_B = 7, 8
ALL_ACTIONS = {"read": True, "update": True, "revoke_sub": True}


class FakePanel:
    def __init__(self, *, answer: dict | None = None, read_error: Exception | None = None) -> None:
        self.answer = answer if answer is not None else {"id": 500, "subscription_url": NEW_URL}
        self.read_error = read_error
        self.revoked: list[int] = []
        self.reads: list[tuple[int, bool]] = []

    async def revoke_sub_by_id(self, user_id: int) -> dict:
        self.revoked.append(user_id)
        return dict(self.answer)

    async def get_user_by_id(self, user_id: int, *, use_cache: bool = True) -> dict:
        self.reads.append((user_id, use_cache))
        if self.read_error is not None:
            raise self.read_error
        return {"id": user_id, "username": "svc", "subscription_url": NEW_URL}


class LinkStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=1001, referral_code="c1"),
                BotUser(id=SHOP_A, telegram_id=1007, referral_code="a", role="reseller"),
                BotUser(id=SHOP_B, telegram_id=1008, referral_code="b", role="reseller"),
            ])
            await session.flush()
            session.add_all([
                UserService(id=10, bot_user_id=1, pg_user_id=500, pg_username="p", subscription_url=OLD_URL),
                UserService(id=11, bot_user_id=1, pg_user_id=500, pg_username="a", subscription_url=OLD_URL, shop_id=SHOP_A),
                UserService(id=12, bot_user_id=1, pg_user_id=500, pg_username="b", subscription_url=OLD_URL, shop_id=SHOP_B),
                UserService(id=13, bot_user_id=1, pg_user_id=501, pg_username="o", subscription_url=OLD_URL, shop_id=SHOP_A),
            ])
            await session.commit()

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()

    async def tokens(self) -> dict[int, str | None]:
        async with self.Session() as session:
            rows = await session.execute(select(UserService.id, UserService.subscription_token))
            return {int(sid): token for sid, token in rows}

    async def test_a_shop_revoke_changes_only_that_shops_services_of_the_user(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await save_links(session, {500: NEW_LINK}, scope=LinkScope.for_owner(SHOP_A)), 1)
            await session.commit()
        self.assertEqual(await self.tokens(), {10: None, 11: "NEWTOKEN123", 12: None, 13: None})

    async def test_the_platform_shop_scope_leaves_reseller_services_alone(self) -> None:
        async with self.Session() as session:
            await save_links(session, {500: NEW_LINK}, scope=LinkScope(shop_id=None))
            await session.commit()
        self.assertEqual(await self.tokens(), {10: "NEWTOKEN123", 11: None, 12: None, 13: None})

    async def test_the_platform_owner_updates_every_service_of_the_user(self) -> None:
        async with self.Session() as session:
            await save_links(session, {500: NEW_LINK}, scope=LinkScope.for_owner(None))
            await session.commit()
            url = await session.scalar(select(UserService.subscription_url).where(UserService.id == 12))
        self.assertEqual(await self.tokens(), {10: "NEWTOKEN123", 11: "NEWTOKEN123", 12: "NEWTOKEN123", 13: None})
        self.assertEqual(url, NEW_URL)

    async def test_a_revoke_answer_without_a_link_is_read_again_without_the_cache(self) -> None:
        panel = FakePanel(answer={"id": 500})
        async with self.Session() as session:
            link = await store_revoked_link(
                session, panel, pg_user_id=500, revoke_response=await panel.revoke_sub_by_id(500),
                scope=LinkScope.for_owner(SHOP_A),
            )
        self.assertEqual(link, NEW_LINK)
        self.assertEqual((panel.reads, panel.revoked), ([(500, False)], [500]))
        self.assertEqual((await self.tokens())[11], "NEWTOKEN123")

    async def test_an_unreadable_new_link_is_left_alone_and_never_revoked_again(self) -> None:
        panel = FakePanel(answer={"id": 500}, read_error=TimeoutError("lost"))
        self.assertIsNone(await link_after_revoke(panel, 500, await panel.revoke_sub_by_id(500)))
        self.assertEqual(panel.revoked, [500])
        self.assertEqual(await self.tokens(), {10: None, 11: None, 12: None, 13: None})

    async def test_a_link_that_is_not_a_subscription_url_is_ignored(self) -> None:
        panel = FakePanel()
        async with self.Session() as session:
            self.assertEqual(
                await store_revoked_link(session, panel, pg_user_id=500, revoke_response={"subscription_url": "not a url"},
                                         scope=LinkScope.for_owner(None)),
                NEW_LINK,
            )

    async def test_bulk_revoke_stores_every_new_link_in_the_shop(self) -> None:
        from app.services.table_bulk_ext import bulk_pg_user_action

        panel = FakePanel()
        staff = {"role": "reseller", "reseller_user_id": SHOP_A}
        with (
            patch("app.services.pg_access.staff_user_actions", return_value=ALL_ACTIONS),
            patch("app.services.pg_quota.assert_can_mutate_owned_users", AsyncMock()),
            patch("app.services.pg_user_scope.is_full_pg_owner", return_value=True),
            patch("app.services.pasarguard.get_pg", return_value=panel),
            patch("app.services.table_bulk_ext._staff_pg_client", AsyncMock(return_value=(panel, False))),
            patch("app.services.subscription_link_store.resolve_shop_scope_id", return_value=SHOP_A),
        ):
            async with self.Session() as session:
                self.assertEqual(await bulk_pg_user_action(session, staff, [500, 501], "revoke"), (2, 0))
        self.assertEqual(panel.revoked, [500, 501])
        self.assertEqual(await self.tokens(), {10: None, 11: "NEWTOKEN123", 12: None, 13: "NEWTOKEN123"})


    async def test_bot_staff_revoke_saves_the_new_link_in_the_bots_shop(self) -> None:
        from app.bot.handlers import admin_pg_users

        panel = FakePanel()
        gate = SimpleNamespace(allowed=True, pg_user={"id": 500}, pg_client=panel)
        callback = SimpleNamespace(data="adm:pg:rev:500", answer=AsyncMock(), message=None)
        with (
            patch.object(admin_pg_users, "_pg_user_gate", AsyncMock(return_value=gate)),
            patch.object(admin_pg_users, "take_reason", AsyncMock(return_value="لینک لو رفته بود")),
            patch.object(admin_pg_users, "_finish_reasoned_pg_action", AsyncMock()),
        ):
            async with self.Session() as session:
                await admin_pg_users.pg_rev(callback, await session.get(BotUser, SHOP_A), session=session,
                                            reseller_owner_id=SHOP_A)
        self.assertEqual(panel.revoked, [500])
        self.assertEqual(await self.tokens(), {10: None, 11: "NEWTOKEN123", 12: None, 13: None})


class WebRevokeKeepsTheCustomerLinkTests(CustomerServicesWebPermissionTests):
    async def test_staff_revoke_saves_the_new_link_on_the_customer_service(self) -> None:
        panel = FakePanel(answer={"id": SERVICE_ID, "subscription_url": NEW_URL})
        for target, value in (
            ("app.api.pg_pages.staff_user_actions", ALL_ACTIONS),
            ("app.services.authz.can_pg_page", True),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (
            ("app.api.pg_pages._staff_pg", AsyncMock(return_value=(panel, True))),
            ("app.api.pg_pages.get_pg", lambda: panel),
            ("app.api.pg_pages.assert_can_mutate_owned_users", AsyncMock()),
            ("app.services.pg_user_scope.is_full_pg_owner", lambda _staff: True),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        cookie = f"session={self.signer.dumps({**OWNER_SESSION, 'pg_permissions': ['pg_users']})}; csrf=token"
        response = await self.client.post(
            f"/pg/users/{SERVICE_ID}/revoke",
            headers={"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"},
            data={"reason": "لینک لو رفته بود"},
        )
        self.assertEqual(response.status_code, 303)
        self.assertNotIn("err=", response.headers["location"])
        self.assertEqual(panel.revoked, [SERVICE_ID])
        async with AsyncSession(bind=self.engine) as session:
            service = await session.get(UserService, SERVICE_ID)
            self.assertEqual((service.subscription_url, service.subscription_token), (NEW_URL, "NEWTOKEN123"))


del CustomerServicesWebPermissionTests
