"""Role forms follow ADMIN_IDS: a bot admin's role is not changed from the panel, and «admin» is never granted there.

The bot re-syncs ``BotUser.role`` from ADMIN_IDS on the person's next update, so a role the
panel wrote would be undone silently: an admin "demoted" here kept the admin menu and access.
"""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.app import create_api_app, templates
from app.db.models import Base, BotUser, ResellerProfile, Role
from tests.postgres_support import scoped_async_engine
from app.services.bot_admins import (
    BOT_ADMIN_GRANT_REFUSED,
    BOT_ADMIN_LABEL,
    BOT_ADMIN_ROLE_LOCKED,
    BOT_ADMIN_SETTINGS_HREF,
    BotAdminRoleLocked,
    assert_role_change_allowed,
    is_bot_admin,
    role_badge,
)

ADMIN_TID = 7001
OTHER_TID = 7002
OWNER_STAFF = {
    "role": "admin", "org_principal_id": 1, "org_depth": 0,
    "org_parent_id": None, "org_status": "active",
}


def listed_admins(*ids: int):
    return patch("app.services.bot_admins.get_settings", return_value=SimpleNamespace(admin_ids=list(ids)))


def person(*, telegram_id: int, role: str = Role.USER.value) -> SimpleNamespace:
    return SimpleNamespace(
        id=40, role=role, telegram_id=telegram_id, username="ali", full_name="علی", first_name="علی",
        wallet_balance=0, points=0, is_blocked=False, is_demo=False, created_at=None, color_tag="green",
    )


class BotAdminRuleTests(unittest.TestCase):
    def test_only_admin_ids_make_a_bot_admin(self) -> None:
        with listed_admins(ADMIN_TID):
            self.assertTrue(is_bot_admin(person(telegram_id=ADMIN_TID, role=Role.USER.value)))
            self.assertFalse(is_bot_admin(person(telegram_id=OTHER_TID, role=Role.ADMIN.value)))
            self.assertFalse(is_bot_admin(person(telegram_id=0)))
            self.assertFalse(is_bot_admin(None))

    def test_role_badge_reads_bot_admin_from_admin_ids(self) -> None:
        with listed_admins(ADMIN_TID):
            self.assertEqual(role_badge(person(telegram_id=ADMIN_TID)).label, BOT_ADMIN_LABEL)
            self.assertEqual(role_badge(person(telegram_id=ADMIN_TID)).css_class, "role-admin")
            self.assertEqual(role_badge(person(telegram_id=OTHER_TID, role=Role.RESELLER.value)).label, "نماینده")
            # A stored admin role without an ADMIN_IDS entry is dropped by the bot on the next update.
            self.assertEqual(role_badge(person(telegram_id=OTHER_TID, role=Role.ADMIN.value)).label, "کاربر")

    def test_any_role_change_for_a_bot_admin_is_refused(self) -> None:
        with listed_admins(ADMIN_TID):
            for new_role in (Role.USER.value, Role.RESELLER.value, Role.ADMIN.value):
                with self.assertRaises(BotAdminRoleLocked) as caught:
                    assert_role_change_allowed(person(telegram_id=ADMIN_TID, role=Role.ADMIN.value), new_role)
                self.assertEqual(str(caught.exception), BOT_ADMIN_ROLE_LOCKED)

    def test_admin_is_never_granted_from_the_role_form(self) -> None:
        with listed_admins(ADMIN_TID):
            with self.assertRaises(BotAdminRoleLocked) as caught:
                assert_role_change_allowed(person(telegram_id=OTHER_TID), Role.ADMIN.value)
        self.assertEqual(str(caught.exception), BOT_ADMIN_GRANT_REFUSED)

    def test_user_and_reseller_changes_stay_allowed_for_everyone_else(self) -> None:
        with listed_admins(ADMIN_TID):
            assert_role_change_allowed(person(telegram_id=OTHER_TID), Role.RESELLER.value)
            assert_role_change_allowed(person(telegram_id=OTHER_TID, role=Role.RESELLER.value), Role.USER.value)
            assert_role_change_allowed(person(telegram_id=OTHER_TID, role=Role.ADMIN.value), Role.USER.value)


class RoleFormTemplateTests(unittest.TestCase):
    def render(self, name: str, user: SimpleNamespace) -> str:
        context = {"user": user, "can_manage_users": True, "reseller_plans": [], "csrf_token": "t"}
        if name == "_reseller_edit_body.html":
            context.update(profile=SimpleNamespace(), feature_perms={}, selected_perms=[], color_tags=[])
        return templates.env.get_template(name).render(**context)

    def test_bot_admin_gets_a_note_and_a_settings_link_instead_of_the_role_form(self) -> None:
        with listed_admins(ADMIN_TID):
            html = self.render("_user_edit_body.html", person(telegram_id=ADMIN_TID, role=Role.ADMIN.value))
        self.assertNotIn('action="/users/40/role"', html)
        self.assertNotIn('name="role"', html)
        self.assertIn(f'href="{BOT_ADMIN_SETTINGS_HREF}"', html)
        self.assertIn(f'<span class="badge role-admin">{BOT_ADMIN_LABEL}</span>', html)

    def test_role_form_offers_no_admin_option(self) -> None:
        with listed_admins(ADMIN_TID):
            html = self.render("_user_edit_body.html", person(telegram_id=OTHER_TID))
        self.assertIn('action="/users/40/role"', html)
        self.assertIn('<option value="reseller"', html)
        self.assertNotIn('<option value="admin"', html)
        self.assertNotIn(BOT_ADMIN_LABEL, html)

    def test_user_card_shows_the_bot_admin_badge(self) -> None:
        card = SimpleNamespace(user=person(telegram_id=ADMIN_TID, role=Role.USER.value), risk_tag=None)
        with listed_admins(ADMIN_TID):
            html = templates.env.get_template("_user_card_body.html").render(card=card)
        self.assertIn(f'<span class="badge role-admin">{BOT_ADMIN_LABEL}</span>', html)
        self.assertNotIn(">مدیر<", html)


class RoleRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pgclock-bot-admin-role-")
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=ADMIN_TID, referral_code="admin1", role=Role.ADMIN.value),
                BotUser(id=2, telegram_id=OTHER_TID, referral_code="cust1", role=Role.USER.value),
                BotUser(id=3, telegram_id=7003, referral_code="shop1", role=Role.RESELLER.value),
                ResellerProfile(id=1, user_id=3, is_active=True),
            ])
            await session.commit()
        admins = listed_admins(ADMIN_TID)
        admins.start()
        self.addCleanup(admins.stop)
        self.notify = AsyncMock()
        for target in (
            "app.services.notifications.notify_account_edit",
            "app.services.resellers.notify_reseller_revoked",
        ):
            patcher = patch(target, self.notify)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self) -> None:
        await self.engine.dispose()
        self.temporary.cleanup()

    def app_for(self, path: str):
        with patch("app.api.app.DATA_DIR", Path(self.temporary.name)):
            app = create_api_app()
        route = next(route for route in app.routes if getattr(route, "path", None) == path)

        async def get_db() -> AsyncIterator[AsyncSession]:
            async with self.Session() as session:
                yield session

        dependencies = list(route.dependant.dependencies)
        while dependencies:
            dependency = dependencies.pop()
            if dependency.call.__name__ == "require_admin":
                app.dependency_overrides[dependency.call] = lambda: dict(OWNER_STAFF)
            elif dependency.call.__name__ == "get_db":
                app.dependency_overrides[dependency.call] = get_db
            dependencies.extend(dependency.dependencies)
        return app

    async def post_role(self, path: str, url: str, role: str) -> httpx.Response:
        app = self.app_for(path)
        with patch("app.api.app.is_setup_complete", return_value=True):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
                cookies={"session": "synthetic-session", "csrf": "synthetic-csrf"},
            ) as client:
                return await client.post(
                    url, data={"role": role, "csrf_token": "synthetic-csrf"}, headers={"Origin": "http://test"},
                )

    async def role_of(self, user_id: int) -> str:
        async with self.Session() as session:
            return (await session.get(BotUser, user_id)).role

    def error_of(self, response: httpx.Response) -> str:
        self.assertEqual(response.status_code, 303, response.text)
        return parse_qs(urlsplit(response.headers["location"]).query).get("err", [""])[0]

    async def test_demoting_a_bot_admin_from_the_user_card_is_refused_and_changes_nothing(self) -> None:
        response = await self.post_role("/users/{user_id}/role", "/users/1/role", Role.USER.value)
        self.assertEqual(self.error_of(response), BOT_ADMIN_ROLE_LOCKED)
        self.assertEqual(await self.role_of(1), Role.ADMIN.value)
        self.notify.assert_not_awaited()

    async def test_granting_admin_from_the_user_card_is_refused(self) -> None:
        response = await self.post_role("/users/{user_id}/role", "/users/2/role", Role.ADMIN.value)
        self.assertEqual(self.error_of(response), BOT_ADMIN_GRANT_REFUSED)
        self.assertEqual(await self.role_of(2), Role.USER.value)
        self.notify.assert_not_awaited()

    async def test_granting_admin_from_the_reseller_card_keeps_the_shop_active(self) -> None:
        response = await self.post_role("/resellers/{user_id}/role", "/resellers/3/role", Role.ADMIN.value)
        self.assertEqual(self.error_of(response), BOT_ADMIN_GRANT_REFUSED)
        self.assertEqual(await self.role_of(3), Role.RESELLER.value)
        async with self.Session() as session:
            self.assertTrue((await session.get(ResellerProfile, 1)).is_active)
        self.notify.assert_not_awaited()

    async def test_an_ordinary_role_change_still_goes_through(self) -> None:
        response = await self.post_role("/users/{user_id}/role", "/users/2/role", Role.USER.value)
        self.assertEqual(self.error_of(response), "")
        self.notify.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
