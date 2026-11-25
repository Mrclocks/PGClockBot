"""An owner turns a customer or a shop off with every panel service, and back on exactly as before."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models import (
    AccountHold,
    AccountHoldItem,
    Base,
    BotUser,
    BotUserShop,
    OrgPrincipal,
    PgAdminSubscription,
    ResellerProfile,
    ServiceFreeze,
    UserService,
)
from app.services.account_hold_actions import (
    customer_hold_panel,
    disable_customer,
    disable_shop,
    enable_customer,
    enable_shop,
)
from app.services.account_hold_guard import (
    HELD_REFUSAL,
    UNBLOCK_REFUSAL,
    assert_service_not_held,
    defer_reenable_to_holds,
)
from app.services.account_hold_runner import run_hold, run_unfinished_holds
from app.services.billing_suspend import restore_payg_reseller
from app.services.org_principals import DEPTH_ONE, DEPTH_OWNER, DEPTH_TWO, STATUS_ACTIVE
from app.services.pasarguard import PasarGuardError
from app.services.shop_customer_block import set_shop_block
from tests.postgres_support import scoped_async_engine

SHOP_A, SHOP_B, CHILD = 81, 82, 83
CUSTOMER, OTHER = 101, 102
OWNER = {
    "role": "admin", "id": 1, "username": "root", "org_principal_id": 1,
    "org_depth": 0, "org_parent_id": None, "org_status": "active",
}


def shop_staff(shop_id: int, perms: list[str] | None = None, principal_id: int | None = None) -> dict:
    staff = {
        "role": "reseller", "bot_user_id": shop_id, "id": shop_id, "username": f"shop{shop_id}",
        "permissions": ["accounts_disable", "customers_block"] if perms is None else perms,
    }
    if principal_id is not None:
        staff.update({"org_principal_id": principal_id, "org_depth": 1, "org_parent_id": 1, "org_status": "active",
                      "pg_permissions": ["admins.create"]})
    return staff


class FakePanel:
    """Panel users and admins in memory; ``flaky`` users time out once on their next write."""

    def __init__(self) -> None:
        self.users: dict[int, dict] = {}
        self.admins: dict[str, dict] = {}
        self.flaky: set[int] = set()
        self.writes: list[tuple[int, bool]] = []

    def add_user(self, uid: int, admin: str, status: str = "active") -> None:
        self.users[uid] = {"id": uid, "status": status, "admin": admin}

    async def get_user_by_id(self, uid: int) -> dict:
        if uid not in self.users:
            raise PasarGuardError("gone", 404)
        return dict(self.users[uid])

    async def set_disabled_by_id(self, uid: int, disabled: bool) -> dict:
        if uid not in self.users:
            raise PasarGuardError("gone", 404)
        self.writes.append((uid, disabled))
        if uid in self.flaky:
            self.flaky.discard(uid)
            # The write lands but the answer is lost: the outcome is unknown to the caller.
            self.users[uid]["status"] = "disabled" if disabled else "active"
            raise httpx.ReadTimeout("slow")
        self.users[uid]["status"] = "disabled" if disabled else "active"
        return dict(self.users[uid])

    async def get_users(self, *, admin: str, limit: int, offset: int) -> dict:
        owned = sorted(u for u, row in self.users.items() if row["admin"] == admin)
        return {"users": [{"id": u} for u in owned[offset:offset + limit]]}

    async def get_admin_gate(self, name: str) -> tuple[str, dict | None]:
        if name not in self.admins:
            return "missing", None
        return "ok", dict(self.admins[name])

    async def modify_admin(self, name: str, payload: dict) -> dict:
        self.admins[name].update(payload)
        return dict(self.admins[name])

    def status(self, uid: int) -> str:
        return self.users[uid]["status"]


class HoldHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.panel = FakePanel()
        self.notices: list[dict] = []

        async def record_notice(session, **kw):
            self.notices.append({"user": int(kw["user"].id), "event": kw["event"], "reason": kw.get("reason"),
                                 "shop_id": kw.get("shop_id")})
            return {"subject": True, "admins": False}

        self.patches = [
            patch("app.services.pasarguard.get_pg", return_value=self.panel),
            patch("app.services.account_hold_actions.run_job_now", return_value=False),
            patch("app.services.notifications.notify_account_edit", new=record_notice),
        ]
        for p in self.patches:
            p.start()
        await self._seed()

    async def asyncTearDown(self) -> None:
        for p in self.patches:
            p.stop()
        await self.engine.dispose()

    async def _seed(self) -> None:
        async with self.Session() as session:
            for shop in (SHOP_A, SHOP_B, CHILD):
                session.add(BotUser(id=shop, telegram_id=shop * 10, referral_code=f"s{shop}", role="reseller"))
            session.add_all([
                BotUser(id=CUSTOMER, telegram_id=1010, referral_code="c101", reseller_id=SHOP_A),
                BotUser(id=OTHER, telegram_id=1020, referral_code="c102", reseller_id=SHOP_B),
            ])
            await session.flush()
            for shop in (SHOP_A, SHOP_B, CHILD):
                session.add(ResellerProfile(user_id=shop, is_active=True, pg_admin_username=f"admin{shop}"))
                self.panel.admins[f"admin{shop}"] = {"username": f"admin{shop}", "status": "active"}
            session.add_all([
                UserService(id=1, bot_user_id=CUSTOMER, shop_id=SHOP_A, pg_user_id=501, pg_username="a1"),
                UserService(id=2, bot_user_id=CUSTOMER, shop_id=SHOP_A, pg_user_id=502, pg_username="a2"),
                UserService(id=3, bot_user_id=CUSTOMER, shop_id=SHOP_B, pg_user_id=503, pg_username="b1"),
                UserService(id=4, bot_user_id=OTHER, shop_id=SHOP_A, pg_user_id=504, pg_username="o1"),
                UserService(id=5, bot_user_id=CUSTOMER, shop_id=SHOP_A, pg_user_id=505, pg_username="gone",
                            is_cancelled=True),
            ])
            session.add(BotUserShop(bot_user_id=CUSTOMER, shop_id=SHOP_A))
            await session.commit()
        for uid, admin in ((501, "adminA"), (502, "adminA"), (503, "adminB"), (504, "adminA")):
            self.panel.add_user(uid, f"admin{SHOP_A if admin == 'adminA' else SHOP_B}")
        self.panel.add_user(601, f"admin{SHOP_A}")  # made directly in the panel, no local service
        self.panel.add_user(701, f"admin{CHILD}")

    async def _run_all(self) -> None:
        for _ in range(3):
            await run_unfinished_holds(self.Session, self.panel)

    async def _disable_customer(self, staff: dict, customer_id: int = CUSTOMER) -> None:
        async with self.Session() as session:
            await disable_customer(session, staff, customer_id, "تخلف از قوانین")
        await self._run_all()

    async def _enable_customer(self, staff: dict, customer_id: int = CUSTOMER) -> None:
        async with self.Session() as session:
            await enable_customer(session, staff, customer_id)
        await self._run_all()

    async def _holds(self) -> list[AccountHold]:
        async with self.Session() as session:
            return list(await session.scalars(select(AccountHold).order_by(AccountHold.id)))

    async def _user(self, user_id: int) -> BotUser:
        async with self.Session() as session:
            return await session.get(BotUser, user_id)

    async def _membership_blocked(self, user_id: int, shop_id: int) -> bool:
        async with self.Session() as session:
            row = await session.scalar(
                select(BotUserShop).where(BotUserShop.bot_user_id == user_id, BotUserShop.shop_id == shop_id)
            )
            return bool(row and row.is_blocked)


class CustomerHoldTests(HoldHarness):
    async def test_owner_disables_every_service_and_release_restores_them(self) -> None:
        await self._disable_customer(OWNER)
        self.assertTrue((await self._user(CUSTOMER)).is_blocked)
        self.assertEqual([self.panel.status(u) for u in (501, 502, 503)], ["disabled"] * 3)
        self.assertEqual(self.panel.status(504), "active")  # another customer
        self.assertEqual((await self._holds())[0].status, "held")

        await self._enable_customer(OWNER)
        self.assertFalse((await self._user(CUSTOMER)).is_blocked)
        self.assertEqual([self.panel.status(u) for u in (501, 502, 503)], ["active"] * 3)
        self.assertEqual((await self._holds())[0].status, "released")

    async def test_shop_staff_reach_only_their_own_shop_services(self) -> None:
        await self._disable_customer(shop_staff(SHOP_A))
        self.assertEqual([self.panel.status(u) for u in (501, 502)], ["disabled"] * 2)
        self.assertEqual(self.panel.status(503), "active")  # the customer's service in shop B
        self.assertTrue(await self._membership_blocked(CUSTOMER, SHOP_A))
        self.assertFalse((await self._user(CUSTOMER)).is_blocked)

    async def test_service_already_off_stays_off_after_release(self) -> None:
        self.panel.users[502]["status"] = "disabled"
        await self._disable_customer(OWNER)
        await self._enable_customer(OWNER)
        self.assertEqual(self.panel.status(501), "active")
        self.assertEqual(self.panel.status(502), "disabled")

    async def test_customer_blocked_before_the_hold_stays_blocked(self) -> None:
        async with self.Session() as session:
            (await session.get(BotUser, CUSTOMER)).is_blocked = True
            await session.commit()
        await self._disable_customer(OWNER)
        await self._enable_customer(OWNER)
        self.assertTrue((await self._user(CUSTOMER)).is_blocked)

    async def test_second_disable_is_refused_and_changes_nothing(self) -> None:
        await self._disable_customer(OWNER)
        with self.assertRaisesRegex(ValueError, "از قبل غیرفعال"):
            await self._disable_customer(OWNER)
        self.assertEqual(len(await self._holds()), 1)

    async def test_repeated_release_is_refused(self) -> None:
        await self._disable_customer(OWNER)
        await self._enable_customer(OWNER)
        with self.assertRaisesRegex(ValueError, "غیرفعال نیست"):
            await self._enable_customer(OWNER)

    async def test_lost_panel_answer_is_retried_with_the_same_call(self) -> None:
        self.panel.flaky.add(501)
        async with self.Session() as session:
            await disable_customer(session, OWNER, CUSTOMER, "تخلف از قوانین")
        await run_unfinished_holds(self.Session, self.panel)
        hold = (await self._holds())[0]
        self.assertEqual(hold.status, "applying")
        async with self.Session() as session:
            state = await session.scalar(select(AccountHoldItem.state).where(AccountHoldItem.pg_user_id == 501))
        self.assertEqual(state, "sending")  # not «skipped»: the hold owns it although the panel shows it off
        await self._run_all()
        self.assertEqual((await self._holds())[0].status, "held")
        await self._enable_customer(OWNER)
        self.assertEqual(self.panel.status(501), "active")

    async def test_removed_panel_user_does_not_block_the_hold(self) -> None:
        del self.panel.users[502]
        await self._disable_customer(OWNER)
        self.assertEqual((await self._holds())[0].status, "held")
        await self._enable_customer(OWNER)
        self.assertEqual((await self._holds())[0].status, "released")

    async def test_staff_without_the_key_or_from_another_shop_are_refused(self) -> None:
        for staff, customer in ((shop_staff(SHOP_A, ["customers_block"]), CUSTOMER), (shop_staff(SHOP_B), CUSTOMER)):
            with self.subTest(staff=staff["permissions"], shop=staff["bot_user_id"]):
                with self.assertRaises((PermissionError, ValueError)):
                    async with self.Session() as session:
                        await disable_customer(session, staff, customer, "تخلف از قوانین")
        self.assertEqual(await self._holds(), [])
        self.assertEqual(self.panel.writes, [])

    async def test_shop_cannot_release_the_owners_hold(self) -> None:
        await self._disable_customer(OWNER)
        async with self.Session() as session:
            panel = await customer_hold_panel(session, shop_staff(SHOP_A), CUSTOMER)
        self.assertIsNotNone(panel)
        self.assertIsNone(panel.hold)  # the owner's hold is account-wide, not this shop's
        with self.assertRaisesRegex(ValueError, "غیرفعال نیست"):
            await self._enable_customer(shop_staff(SHOP_A))
        self.assertEqual(self.panel.status(501), "disabled")

    async def test_reason_is_required(self) -> None:
        with self.assertRaisesRegex(ValueError, "علت"):
            async with self.Session() as session:
                await disable_customer(session, OWNER, CUSTOMER, " ")

    async def test_old_unblock_paths_cannot_lift_the_hold(self) -> None:
        await self._disable_customer(shop_staff(SHOP_A))
        with self.assertRaisesRegex(ValueError, UNBLOCK_REFUSAL[:20]):
            async with self.Session() as session:
                await set_shop_block(session, staff=shop_staff(SHOP_A), customer_id=CUSTOMER, blocked=False,
                                     reason="")
        self.assertTrue(await self._membership_blocked(CUSTOMER, SHOP_A))

    async def test_renewal_of_a_held_service_is_refused(self) -> None:
        await self._disable_customer(OWNER)
        async with self.Session() as session:
            with self.assertRaisesRegex(ValueError, HELD_REFUSAL[:15]):
                await assert_service_not_held(session, 1)


class OverlapTests(HoldHarness):
    async def test_two_holds_on_one_user_keep_it_off_until_both_are_released(self) -> None:
        await self._disable_customer(shop_staff(SHOP_A))
        await self._disable_customer(OWNER)
        await self._enable_customer(shop_staff(SHOP_A))
        self.assertEqual(self.panel.status(501), "disabled")  # the owner's hold still covers it
        await self._enable_customer(OWNER)
        self.assertEqual(self.panel.status(501), "active")

    async def test_frozen_service_stays_off_at_release(self) -> None:
        from datetime import datetime, timedelta, timezone

        await self._disable_customer(OWNER)
        async with self.Session() as session:
            session.add(ServiceFreeze(service_id=1, shop_id=SHOP_A, user_id=CUSTOMER, status="frozen",
                                      remaining_seconds=3600,
                                      resume_by=datetime.now(timezone.utc) + timedelta(days=1)))
            await session.commit()
        await self._enable_customer(OWNER)
        self.assertEqual(self.panel.status(501), "disabled")
        self.assertEqual(self.panel.status(502), "active")

    async def test_other_reenable_paths_hand_their_users_to_the_hold(self) -> None:
        self.panel.users[502]["status"] = "disabled"  # off for another reason before the hold
        await self._disable_customer(OWNER)
        async with self.Session() as session:
            held = await defer_reenable_to_holds(session, [501, 502, 504])
            await session.commit()
        self.assertEqual(held, {501, 502})
        await self._enable_customer(OWNER)
        self.assertEqual(self.panel.status(502), "active")  # the path that wanted it on is honoured


class ShopHoldTests(HoldHarness):
    async def _tree(self) -> None:
        async with self.Session() as session:
            owner = OrgPrincipal(id=1, parent_id=None, depth=DEPTH_OWNER, status=STATUS_ACTIVE, pg_username="root")
            session.add(owner)
            await session.flush()
            profiles = {p.user_id: p for p in (await session.scalars(select(ResellerProfile))).all()}
            parent = OrgPrincipal(id=2, parent_id=1, depth=DEPTH_ONE, status=STATUS_ACTIVE,
                                  pg_username=f"admin{SHOP_A}", reseller_profile_id=profiles[SHOP_A].id)
            session.add(parent)
            await session.flush()
            session.add(OrgPrincipal(id=3, parent_id=2, depth=DEPTH_TWO, status=STATUS_ACTIVE,
                                     pg_username=f"admin{CHILD}", reseller_profile_id=profiles[CHILD].id))
            session.add(OrgPrincipal(id=4, parent_id=1, depth=DEPTH_ONE, status=STATUS_ACTIVE,
                                     pg_username=f"admin{SHOP_B}", reseller_profile_id=profiles[SHOP_B].id))
            await session.commit()

    async def _profile(self, shop: int) -> ResellerProfile:
        async with self.Session() as session:
            return await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == shop))

    async def _disable_shop(self, staff: dict, shop: int) -> None:
        async with self.Session() as session:
            await disable_shop(session, staff, shop, "بدهی پرداخت نشده")
        await self._run_all()

    async def _enable_shop(self, staff: dict, shop: int) -> None:
        async with self.Session() as session:
            await enable_shop(session, staff, shop)
        await self._run_all()

    async def test_owner_turns_off_shop_admin_users_and_sub_shops_then_back_on(self) -> None:
        await self._tree()
        await self._disable_shop(OWNER, SHOP_A)
        self.assertFalse((await self._profile(SHOP_A)).is_active)
        self.assertFalse((await self._profile(CHILD)).is_active)
        self.assertEqual(self.panel.admins[f"admin{SHOP_A}"]["status"], "disabled")
        self.assertEqual(self.panel.admins[f"admin{CHILD}"]["status"], "disabled")
        self.assertEqual([self.panel.status(u) for u in (501, 502, 504, 601, 701)], ["disabled"] * 5)
        self.assertEqual(self.panel.status(503), "active")  # shop B
        self.assertTrue((await self._profile(SHOP_B)).is_active)

        await self._enable_shop(OWNER, SHOP_A)
        self.assertTrue((await self._profile(SHOP_A)).is_active)
        self.assertTrue((await self._profile(CHILD)).is_active)
        self.assertEqual(self.panel.admins[f"admin{SHOP_A}"]["status"], "active")
        self.assertEqual([self.panel.status(u) for u in (501, 502, 504, 601, 701)], ["active"] * 5)

    async def test_parent_shop_may_hold_only_its_own_sub_shop(self) -> None:
        await self._tree()
        parent_staff = shop_staff(SHOP_A, principal_id=2)
        with patch("app.services.representative_unification.staff_can_manage_representatives", return_value=True):
            with self.assertRaises(PermissionError):
                async with self.Session() as session:
                    await disable_shop(session, parent_staff, SHOP_B, "بدهی پرداخت نشده")
            await self._disable_shop(parent_staff, CHILD)
        self.assertFalse((await self._profile(CHILD)).is_active)
        self.assertTrue((await self._profile(SHOP_B)).is_active)
        self.assertEqual(self.panel.status(701), "disabled")

    async def test_shop_staff_without_the_key_cannot_hold_a_shop(self) -> None:
        await self._tree()
        with self.assertRaises(PermissionError):
            async with self.Session() as session:
                await disable_shop(session, shop_staff(SHOP_A, ["dashboard"], principal_id=2), CHILD, "بدهی معوق")

    async def test_shop_disabled_before_the_hold_stays_off(self) -> None:
        async with self.Session() as session:
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == SHOP_B))
            profile.is_active = False
            await session.commit()
        self.panel.admins[f"admin{SHOP_B}"]["status"] = "disabled"
        await self._disable_shop(OWNER, SHOP_B)
        await self._enable_shop(OWNER, SHOP_B)
        self.assertFalse((await self._profile(SHOP_B)).is_active)
        self.assertEqual(self.panel.admins[f"admin{SHOP_B}"]["status"], "disabled")

    async def test_payg_restore_while_held_leaves_the_shop_off(self) -> None:
        await self._disable_shop(OWNER, SHOP_B)
        async with self.Session() as session:
            profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == SHOP_B))
            from datetime import datetime, timezone

            profile.billing_suspended_at = datetime.now(timezone.utc)
            profile.billing_suspended_user_ids = "[503]"
            await session.commit()
            await restore_payg_reseller(session, profile, force=True)
        self.assertEqual(self.panel.admins[f"admin{SHOP_B}"]["status"], "disabled")
        self.assertEqual(self.panel.status(503), "disabled")
        await self._enable_shop(OWNER, SHOP_B)
        self.assertEqual(self.panel.admins[f"admin{SHOP_B}"]["status"], "active")
        self.assertEqual(self.panel.status(503), "active")

    async def test_expired_subscription_users_wait_for_the_renewal(self) -> None:
        async with self.Session() as session:
            session.add(PgAdminSubscription(pg_username=f"admin{SHOP_B}", access_status="expired",
                                            expiry_disabled_user_ids="[503]"))
            await session.commit()
        self.panel.users[503]["status"] = "disabled"
        self.panel.admins[f"admin{SHOP_B}"]["status"] = "disabled"
        await self._disable_shop(OWNER, SHOP_B)
        await self._enable_shop(OWNER, SHOP_B)
        self.assertEqual(self.panel.status(503), "disabled")
        self.assertEqual(self.panel.admins[f"admin{SHOP_B}"]["status"], "disabled")

    async def test_unreadable_user_list_keeps_the_hold_applying(self) -> None:
        async def broken(**_kw: object) -> dict:
            raise httpx.ConnectError("down")

        with patch.object(self.panel, "get_users", side_effect=broken):
            async with self.Session() as session:
                await disable_shop(session, OWNER, SHOP_B, "بدهی پرداخت نشده")
            await run_unfinished_holds(self.Session, self.panel)
        hold = (await self._holds())[0]
        self.assertEqual(hold.status, "applying")
        self.assertFalse(hold.items_listed)
        await self._run_all()
        self.assertEqual((await self._holds())[0].status, "held")
        self.assertEqual(self.panel.status(503), "disabled")

    async def test_busy_lease_is_not_run_twice(self) -> None:
        from datetime import datetime, timedelta, timezone

        async with self.Session() as session:
            await disable_customer(session, OWNER, CUSTOMER, "تخلف از قوانین")
        async with self.Session() as session:
            hold = await session.scalar(select(AccountHold))
            hold.lease_token, hold.lease_until = "x" * 32, datetime.now(timezone.utc) + timedelta(minutes=5)
            await session.commit()
            hold_id = int(hold.id)
        self.assertIsNone(await run_hold(self.Session, self.panel, hold_id))
        self.assertEqual(self.panel.writes, [])


class AccountHoldRouteTests(HoldHarness):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from fastapi import FastAPI

        from app.api.account_hold_pages import register_account_hold_pages

        self.staff = shop_staff(SHOP_A)

        async def get_db():
            async with self.Session() as session:
                yield session

        app = FastAPI()
        register_account_hold_pages(app, require_staff=lambda: self.staff, get_db=get_db)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await super().asyncTearDown()

    async def _post(self, path: str, **data: str) -> str:
        return (await self.client.post(path, data=data)).headers["location"]

    async def test_customer_route_disables_and_enables_within_the_shop(self) -> None:
        self.assertIn("ok=", await self._post(f"/users/{CUSTOMER}/account-hold", action="disable", reason="spam!"))
        await self._run_all()
        self.assertEqual(self.panel.status(501), "disabled")
        self.assertIn("err=", await self._post(f"/users/{OTHER}/account-hold", action="disable", reason="spam!"))
        self.assertIn("err=", await self._post(f"/users/{CUSTOMER}/account-hold", action="drop"))
        self.assertIn("ok=", await self._post(f"/users/{CUSTOMER}/account-hold", action="enable"))
        await self._run_all()
        self.assertEqual(self.panel.status(501), "active")

    async def test_shop_route_refuses_shop_staff_on_an_unrelated_shop(self) -> None:
        self.assertIn("err=", await self._post(f"/resellers/{SHOP_B}/account-hold", action="disable", reason="debt!"))
        self.assertEqual(await self._holds(), [])


class HoldNoticeTests(HoldHarness):
    async def test_customer_hears_once_when_disabled_and_once_when_enabled(self) -> None:
        await self._disable_customer(OWNER)
        await self._run_all()
        await self._enable_customer(OWNER)
        await self._run_all()
        self.assertEqual(
            [(n["user"], n["event"], n["shop_id"]) for n in self.notices],
            [(CUSTOMER, "account_hold", None), (CUSTOMER, "account_release", None)],
        )
        self.assertEqual(self.notices[0]["reason"], "تخلف از قوانین")
        self.assertIsNone(self.notices[1]["reason"])

    async def test_shop_notice_comes_from_that_shops_bot(self) -> None:
        await self._disable_customer(shop_staff(SHOP_A))
        self.assertEqual([(n["event"], n["shop_id"]) for n in self.notices], [("account_hold", SHOP_A)])

    async def test_refused_disable_sends_nothing(self) -> None:
        with self.assertRaises((PermissionError, ValueError)):
            async with self.Session() as session:
                await disable_customer(session, shop_staff(SHOP_B), CUSTOMER, "تخلف از قوانین")
        self.assertEqual(self.notices, [])

    async def test_shop_and_its_sub_shop_both_hear(self) -> None:
        await ShopHoldTests._tree(self)
        async with self.Session() as session:
            await disable_shop(session, OWNER, SHOP_A, "بدهی پرداخت نشده")
        await self._run_all()
        self.assertEqual(sorted((n["user"], n["event"]) for n in self.notices),
                         [(SHOP_A, "shop_hold"), (CHILD, "shop_hold")])
        async with self.Session() as session:
            await enable_shop(session, OWNER, SHOP_A)
        await self._run_all()
        self.assertEqual(sorted((n["user"], n["event"]) for n in self.notices[2:]),
                         [(SHOP_A, "shop_release"), (CHILD, "shop_release")])


class HoldNoticeTextTests(unittest.TestCase):
    def test_reason_and_name_are_escaped_for_telegram_html(self) -> None:
        from types import SimpleNamespace

        from app.services.notifications import format_account_edit_admin, format_account_edit_subject

        text = format_account_edit_subject(event="account_hold", reason="<b>x</b>")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("خاموش", text)
        user = SimpleNamespace(full_name="<i>evil</i>", username=None, telegram_id=5, role="user")
        admin = format_account_edit_admin(event="shop_hold", user=user, reason="r", actor="<a>")
        self.assertNotIn("<i>evil", admin)
        self.assertNotIn("<a>", admin)


class HoldRowActionTests(unittest.TestCase):
    """The row «⋯» forms that the user card copies: disable, enable, or nothing to lift."""

    def _render(self, hold: object) -> str:
        from app.api.app import templates
        from app.services.account_hold_actions import ShopHoldRow

        row = None if hold is None else ShopHoldRow(*hold)
        macro = templates.env.get_template("_account_hold_actions.html").module.customer_hold_actions
        return str(macro(7, row, platform=False))

    def test_no_hold_offers_disable_with_a_reason(self) -> None:
        html = self._render(None)
        self.assertIn('action="/users/7/account-hold"', html)
        self.assertIn('value="disable"', html)
        self.assertIn('data-confirm-reason="1"', html)

    def test_own_hold_offers_enable(self) -> None:
        html = self._render(("held", True))
        self.assertIn('value="enable"', html)
        self.assertNotIn('value="disable"', html)

    def test_someone_elses_or_finishing_hold_offers_nothing(self) -> None:
        self.assertNotIn("<form", self._render(("held", False)))
        self.assertNotIn("<form", self._render(("releasing", True)))


class AccountHoldMigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_rows(self) -> None:
        import importlib.util
        from pathlib import Path

        import sqlalchemy as sa
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from sqlalchemy import text

        from tests.postgres_support import scoped_sync_engine

        path = Path(__file__).resolve().parents[1] / "alembic/versions/0133_account_holds.py"
        spec = importlib.util.spec_from_file_location("account_holds_migration", path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        engine = scoped_sync_engine()
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
            conn.execute(text("CREATE TABLE user_services (id INTEGER PRIMARY KEY, pg_username TEXT)"))
            conn.execute(text("INSERT INTO bot_users VALUES (7)"))
            conn.execute(text("INSERT INTO user_services VALUES (1, 'old-service')"))
            with Operations.context(MigrationContext.configure(conn)):
                migration.upgrade()
                migration.upgrade()
                names = {ix["name"] for ix in sa.inspect(conn).get_indexes("account_holds")}
                self.assertIn("uq_account_holds_open", names)
                conn.execute(text("INSERT INTO account_holds (kind, target_user_id) VALUES ('customer', 7)"))
                with self.assertRaises(sa.exc.IntegrityError):
                    with conn.begin_nested():
                        conn.execute(text("INSERT INTO account_holds (kind, target_user_id) VALUES ('customer', 7)"))
                migration.downgrade()
                self.assertNotIn("account_holds", sa.inspect(conn).get_table_names())
                self.assertEqual(conn.execute(text("SELECT pg_username FROM user_services")).scalar(), "old-service")
                migration.upgrade()
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
