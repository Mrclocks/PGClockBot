"""«تأیید ورود کاربران جدید»: who waits, who decides, isolation, idempotency, the bot gate and endpoints."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import sqlalchemy as sa
from aiogram.types import CallbackQuery, Chat, Message, User
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot import entry_gate
from app.db import Base
from app.db.models import BotUser, BotUserShop, EntryApproval, Order, PurchaseContact, ResellerProfile, ResellerSetting, Setting
from app.services import entry_approval as ea
from app.services.entry_approval_config import KEY_SINCE, prepare_saved_values, read_config
from app.services.entry_approval_notify import parse_admin_callback, request_text
from app.services.miniapp_auth import ENTRY_PENDING, ERROR_HEADER, assert_mini_entry_approval
from app.services.miniapp_shop import MiniScope
from app.services.users import clear_settings_cache, get_all_settings, set_settings_bulk
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

PERMS_WITH_APPROVAL = "dashboard,shop_settings,customers_approve"
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)
SWITCHED_ON = datetime(2021, 1, 1, tzinfo=timezone.utc)
ON = {"entry_approval_enabled": "1"}


async def pin_switch_moment(session) -> None:
    """The gate was switched on after LONG_AGO and before the fixtures' «new» customers joined."""
    await session.execute(update(Setting).where(Setting.key == KEY_SINCE).values(value=SWITCHED_ON.isoformat()))
    await session.execute(update(ResellerSetting).where(ResellerSetting.key == KEY_SINCE).values(value=SWITCHED_ON.isoformat()))
    await session.commit()


class GateCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clear_settings_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'e.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.Session() as session:
            session.add_all([
                BotUser(id=1, telegram_id=101, referral_code="old", role="user", created_at=LONG_AGO),
                BotUser(id=2, telegram_id=102, referral_code="new", role="user", full_name="<b>تازه</b>"),
                BotUser(id=3, telegram_id=103, referral_code="ref", role="user", referred_by_id=1),
                BotUser(id=5, telegram_id=105, referral_code="shop5", role="reseller"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
                BotUser(id=7, telegram_id=107, referral_code="shop7", role="reseller"),
                ResellerProfile(user_id=5, is_active=True, web_permissions=PERMS_WITH_APPROVAL, bot_admin_ids="205"),
                ResellerProfile(user_id=7, is_active=True, web_permissions="dashboard,shop_settings"),
            ])
            await session.commit()
            await set_settings_bulk(session, ON)
            await set_settings_bulk(session, ON, reseller_id=5)
            await set_settings_bulk(session, ON, reseller_id=7)
            await pin_switch_moment(session)
        clear_settings_cache()

    async def asyncTearDown(self):
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def settings(self, values: dict[str, str], shop: int | None = None) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, values, reseller_id=shop)
        clear_settings_cache()

    async def gate(self, user_id: int, shop_key: int = 0, *, create: bool = True) -> ea.GateState:
        async with self.Session() as session:
            config = await ea.gate_config(session, shop_key)
            self.assertIsNotNone(config)
            user = await session.get(BotUser, user_id)
            return await ea.resolve_gate(session, user, shop_key=shop_key, config=config, create=create)

    async def decide(self, approval_id: int, action: ea.Action, shop_key: int = 0) -> ea.Decision:
        async with self.Session() as session:
            return await ea.decide(session, approval_id, shop_key=shop_key, action=action, decided_by="admin:test")

    async def rows(self) -> list[EntryApproval]:
        async with self.Session() as session:
            return list((await session.scalars(select(EntryApproval).order_by(EntryApproval.id))).all())

    async def config(self, shop_key: int = 0):
        async with self.Session() as session:
            return await ea.gate_config(session, shop_key)


class SettingsTests(GateCase):
    async def test_switching_on_records_the_moment_and_saving_again_keeps_it(self):
        async with self.Session() as session:
            first = (await get_all_settings(session))[KEY_SINCE]
        self.assertTrue(first)
        await self.settings({"entry_approval_enabled": "1", "entry_approval_retry_hours": "5"})
        async with self.Session() as session:
            self.assertEqual((await get_all_settings(session))[KEY_SINCE], first)
        await self.settings({"entry_approval_enabled": "0"})
        async with self.Session() as session:
            self.assertEqual((await get_all_settings(session))[KEY_SINCE], "")
            self.assertIsNone(await ea.gate_config(session, 0))

    def test_numbers_are_clamped_and_since_never_comes_from_a_form(self):
        saved = prepare_saved_values(
            {"entry_approval_retry_hours": "-5", "entry_approval_expire_days": "۹۹۹", KEY_SINCE: "2000-01-01"}, {}
        )
        self.assertEqual((saved["entry_approval_retry_hours"], saved["entry_approval_expire_days"]), ("0", "90"))
        self.assertNotIn(KEY_SINCE, saved)
        self.assertEqual(prepare_saved_values({"entry_approval_retry_hours": "abc"}, {})["entry_approval_retry_hours"], "24")
        self.assertEqual(read_config({"entry_approval_retry_hours": "NaN"}).retry_hours, 24)

    async def test_a_shop_without_the_permission_lets_everyone_in(self):
        self.assertIsNone(await self.config(7))
        self.assertIsNotNone(await self.config(5))


class GateTests(GateCase):
    async def test_a_customer_from_before_the_switch_walks_in(self):
        self.assertTrue((await self.gate(1)).is_open)
        (row,) = await self.rows()
        self.assertEqual((row.status, row.decided_by), ("approved", "exempt"))

    async def test_a_shop_member_from_before_the_switch_walks_in_but_a_new_one_waits(self):
        async with self.Session() as session:
            session.add(BotUserShop(bot_user_id=2, shop_id=5, created_at=LONG_AGO))
            await session.commit()
        self.assertTrue((await self.gate(2, 5)).is_open)
        self.assertEqual((await self.gate(6, 5)).step, ea.GateStep.PENDING)

    async def test_a_shop_customer_who_bought_from_the_platform_before_walks_in(self):
        async with self.Session() as session:
            session.add(Order(user_id=6, reseller_id=None, amount=1000, status="delivered", created_at=LONG_AGO))
            await session.commit()
        self.assertTrue((await self.gate(6)).is_open)
        self.assertEqual((await self.gate(6, 5)).step, ea.GateStep.PENDING)

    async def test_a_new_customer_waits_and_the_admins_are_told_once(self):
        state = await self.gate(2)
        self.assertEqual((state.step, state.needs_notice), (ea.GateStep.PENDING, True))
        async with self.Session() as session:
            claims = await asyncio.gather(ea.claim_admin_notice(session, state.approval_id))
        async with self.Session() as session:
            claims.append(await ea.claim_admin_notice(session, state.approval_id))
        self.assertEqual(claims, [True, False])
        self.assertFalse((await self.gate(2)).needs_notice)
        self.assertEqual(len(await self.rows()), 1)

    async def test_an_invited_customer_skips_the_queue_only_when_allowed(self):
        self.assertEqual((await self.gate(3)).step, ea.GateStep.PENDING)
        async with self.Session() as session:
            await session.execute(update(EntryApproval).values(status="rejected"))
            await session.execute(update(BotUser).where(BotUser.id == 3).values(referred_by_id=1))
            await session.commit()
        await self.settings({"entry_approval_skip_referred": "1"})
        async with self.Session() as session:
            session.add(BotUser(id=4, telegram_id=104, referral_code="r4", role="user", referred_by_id=1))
            # Invited in shop 5's bot, so it is no pass for the platform.
            session.add(BotUser(id=8, telegram_id=108, referral_code="r8", role="user", referred_by_id=6, referral_shop_id=5))
            await session.commit()
        self.assertTrue((await self.gate(4)).is_open)
        self.assertEqual((await self.gate(8)).step, ea.GateStep.PENDING)

    async def test_the_phone_comes_before_the_request_when_asked(self):
        await self.settings({"entry_approval_require_phone": "1"})
        self.assertEqual((await self.gate(2)).step, ea.GateStep.NEEDS_PHONE)
        self.assertEqual(await self.rows(), [])
        async with self.Session() as session:
            session.add(PurchaseContact(user_id=2, shop_key=0, telegram_id=102, phone_hash="h"))
            await session.commit()
        self.assertEqual((await self.gate(2)).step, ea.GateStep.PENDING)

    async def test_the_mini_app_never_opens_a_request(self):
        self.assertEqual((await self.gate(2, create=False)).step, ea.GateStep.PENDING)
        self.assertEqual(await self.rows(), [])

    async def test_an_unanswered_request_expires_into_a_rejection(self):
        await self.settings({"entry_approval_expire_days": "1"})
        state = await self.gate(2)
        async with self.Session() as session:
            await session.execute(update(EntryApproval).values(requested_at=datetime.now(timezone.utc) - timedelta(days=2)))
            await session.commit()
            self.assertEqual(await ea.list_requests(session, 0), [])
        self.assertEqual((await self.gate(2)).step, ea.GateStep.REJECTED)
        async with self.Session() as session:
            (closed,) = await ea.list_requests(session, 0, view=ea.VIEW_CLOSED)
        self.assertEqual((closed["id"], closed["expired"]), (state.approval_id, True))


class DecisionTests(GateCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.platform_id = (await self.gate(2)).approval_id
        self.shop_id = (await self.gate(6, 5)).approval_id

    async def test_approval_opens_the_bot(self):
        decision = await self.decide(self.platform_id, ea.Action.APPROVE)
        self.assertEqual((decision.user_id, decision.status), (2, "approved"))
        self.assertTrue((await self.gate(2)).is_open)

    async def test_repeated_approval_is_refused(self):
        await self.decide(self.platform_id, ea.Action.APPROVE)
        with self.assertRaises(ValueError):
            await self.decide(self.platform_id, ea.Action.REJECT)

    async def test_simultaneous_decisions_only_one_wins(self):
        results = await asyncio.gather(
            self.decide(self.platform_id, ea.Action.APPROVE),
            self.decide(self.platform_id, ea.Action.BLOCK),
            return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(r, ea.Decision) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)

    async def test_a_shop_cannot_decide_the_platforms_request_and_vice_versa(self):
        with self.assertRaises(ValueError):
            await self.decide(self.platform_id, ea.Action.APPROVE, shop_key=5)
        with self.assertRaises(ValueError):
            await self.decide(self.shop_id, ea.Action.APPROVE, shop_key=0)
        with self.assertRaises(ValueError):
            await self.decide(self.shop_id, ea.Action.APPROVE, shop_key=7)
        self.assertEqual({r.status for r in await self.rows()}, {"pending"})

    async def test_lists_are_scoped_to_their_shop(self):
        async with self.Session() as session:
            platform = await ea.list_requests(session, 0)
            shop = await ea.list_requests(session, 5)
            other = await ea.list_requests(session, 7)
        self.assertEqual([r["customer"] for r in platform], [102])
        self.assertEqual([r["customer"] for r in shop], [106])
        self.assertEqual(other, [])

    async def test_a_rejected_customer_may_ask_again_only_after_the_wait(self):
        await self.decide(self.platform_id, ea.Action.REJECT)
        config = await self.config()
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await ea.request_again(session, 2, shop_key=0, config=config)
            await session.execute(update(EntryApproval).values(decided_at=datetime.now(timezone.utc) - timedelta(hours=25)))
            await session.commit()
            state = await ea.request_again(session, 2, shop_key=0, config=config)
            self.assertEqual((state.step, state.needs_notice), (ea.GateStep.PENDING, True))
            with self.assertRaises(ValueError):
                await ea.request_again(session, 2, shop_key=0, config=config)

    async def test_no_second_request_when_retry_is_off_or_blocked(self):
        await self.settings({"entry_approval_retry_hours": "0"})
        await self.decide(self.platform_id, ea.Action.REJECT)
        config = await self.config()
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await ea.request_again(session, 2, shop_key=0, config=config)
        await self.settings({"entry_approval_retry_hours": "1"})
        await self.decide(self.platform_id, ea.Action.BLOCK)
        async with self.Session() as session:
            await session.execute(update(EntryApproval).values(decided_at=LONG_AGO))
            await session.commit()
            with self.assertRaises(ValueError):
                await ea.request_again(session, 2, shop_key=0, config=await self.config())
        self.assertEqual((await self.gate(2)).step, ea.GateStep.BLOCKED)
        await self.decide(self.platform_id, ea.Action.APPROVE)
        self.assertTrue((await self.gate(2)).is_open)


class BotDeciderTests(GateCase):
    async def may(self, telegram_id: int, user_id: int, *, shop_key: int, bot_shop_id: int | None) -> bool:
        async with self.Session() as session:
            reviewer = await session.get(BotUser, user_id) or BotUser(id=user_id, telegram_id=telegram_id)
            reviewer.telegram_id = telegram_id
            return await ea.can_decide_in_bot(session, reviewer, shop_key=shop_key, bot_shop_id=bot_shop_id)

    async def test_only_the_right_people_in_the_right_bot_decide(self):
        admins = SimpleNamespace(admin_ids=[999])
        with patch.object(ea, "get_settings", lambda: admins):
            self.assertTrue(await self.may(999, 1, shop_key=0, bot_shop_id=None))
            self.assertFalse(await self.may(101, 1, shop_key=0, bot_shop_id=None))
            self.assertFalse(await self.may(999, 1, shop_key=0, bot_shop_id=5))
            self.assertTrue(await self.may(105, 5, shop_key=5, bot_shop_id=5))
            self.assertTrue(await self.may(205, 1, shop_key=5, bot_shop_id=5))
            self.assertFalse(await self.may(105, 5, shop_key=5, bot_shop_id=None))
            self.assertFalse(await self.may(105, 5, shop_key=7, bot_shop_id=7))
            self.assertFalse(await self.may(107, 7, shop_key=7, bot_shop_id=7))


class ShopBotMenuTests(GateCase):
    async def test_the_shop_menu_offers_the_switches_only_with_the_permission(self):
        from app.bot.handlers import reseller_settings

        async with self.Session() as session:
            self.assertEqual(await reseller_settings._subs_without_permission(session, 5), set())
            self.assertEqual(await reseller_settings._subs_without_permission(session, 7), {"approval"})


class NoticeTests(unittest.TestCase):
    def test_hostile_names_are_escaped_in_the_admin_card(self):
        user = BotUser(id=2, telegram_id=102, full_name="<b>x</b>", username="a<b")
        text = request_text(user, shop_key=0, phone="+98<912>")
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("+98&lt;912&gt;", text)

    def test_only_well_formed_admin_buttons_are_read(self):
        self.assertEqual(parse_admin_callback("eap:ok:12"), (ea.Action.APPROVE, 12))
        self.assertEqual(parse_admin_callback("eap:bk:3"), (ea.Action.BLOCK, 3))
        for bad in ("eap:ok:-1", "eap:xx:1", "eap:ok:1:2", "eap:ok:", "eapx:go", None, "eap:ok:" + "9" * 20):
            self.assertIsNone(parse_admin_callback(bad), bad)


def _message(text: str = "سلام", *, contact: bool = False) -> Message:
    return Message.model_construct(
        message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=102, type="private"),
        from_user=User(id=102, is_bot=False, first_name="t"), text=text, contact=object() if contact else None,
    )


class BotGateTests(GateCase):
    async def run_middleware(self, event, user_id: int = 2) -> tuple[bool, AsyncMock]:
        handler = AsyncMock(return_value="handled")
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            data = {"session": session, "db_user": user, "bot": None, "is_reseller_bot": False, "reseller_owner_id": None}
            with patch.object(entry_gate, "show_gate", AsyncMock()) as shown:
                result = await entry_gate.EntryApprovalMiddleware()(handler, event, data)
        return result == "handled", shown

    async def test_a_waiting_customers_message_is_stopped_with_the_waiting_text(self):
        passed, shown = await self.run_middleware(_message())
        self.assertFalse(passed)
        self.assertEqual(shown.await_args.args[1].step, ea.GateStep.PENDING)

    async def test_start_and_contacts_reach_their_handlers(self):
        self.assertTrue((await self.run_middleware(_message("/start")))[0])
        self.assertTrue((await self.run_middleware(_message("", contact=True)))[0])

    async def test_a_waiting_customers_tap_gets_an_alert(self):
        callback = CallbackQuery.model_construct(
            id="1", from_user=User(id=102, is_bot=False, first_name="t"), chat_instance="c", data="shop:list", message=None,
        )
        with patch.object(CallbackQuery, "answer", AsyncMock()) as answered:
            passed, _ = await self.run_middleware(callback)
        self.assertFalse(passed)
        self.assertEqual(answered.await_args.args[0], entry_gate.WAIT_TOAST)

    async def test_approved_customers_and_staff_pass(self):
        self.assertTrue((await self.run_middleware(_message(), user_id=1))[0])
        self.assertTrue((await self.run_middleware(_message(), user_id=5))[0])

    async def test_the_gate_tells_the_admins_once_and_shows_the_waiting_text(self):
        message = SimpleNamespace(bot=object(), answer=AsyncMock())
        with patch.object(entry_gate, "notify_admins", AsyncMock(return_value=1)) as notified:
            async with self.Session() as session:
                ui = await get_all_settings(session)
                user = await session.get(BotUser, 2)
                self.assertTrue(await entry_gate.run_entry_gate(message, session, user, ui=ui, shop_id=None))
                self.assertTrue(await entry_gate.run_entry_gate(message, session, user, ui=ui, shop_id=None))
                old = await session.get(BotUser, 1)
                self.assertFalse(await entry_gate.run_entry_gate(message, session, old, ui=ui, shop_id=None))
        self.assertEqual(notified.await_count, 1)
        self.assertEqual(message.answer.await_count, 2)


class MiniAppTests(GateCase):
    async def test_a_customer_not_approved_is_refused_without_creating_a_request(self):
        scope = MiniScope(shop_id=None, bot_token=None)
        async with self.Session() as session:
            with self.assertRaises(HTTPException) as caught:
                await assert_mini_entry_approval(session, await session.get(BotUser, 2), scope=scope)
            self.assertEqual(caught.exception.status_code, 403)
            self.assertEqual(caught.exception.headers, {ERROR_HEADER: ENTRY_PENDING})
            await assert_mini_entry_approval(session, await session.get(BotUser, 1), scope=scope)
        self.assertEqual([r.bot_user_id for r in await self.rows()], [1])


class EndpointTests(RoutesHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        clear_settings_cache()
        Session = async_sessionmaker(self.engine, expire_on_commit=False)
        self.Session = Session
        async with Session() as session:
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(web_permissions=PERMS_WITH_APPROVAL))
            session.add_all([
                BotUser(id=2, telegram_id=102, referral_code="u2", role="user"),
                BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
                EntryApproval(id=1, bot_user_id=2, shop_key=0, status="pending"),
                EntryApproval(id=2, bot_user_id=6, shop_key=5, status="pending"),
            ])
            await session.commit()
            await set_settings_bulk(session, ON)
            await set_settings_bulk(session, ON, reseller_id=5)
        clear_settings_cache()
        for target in ("tell_customer", "settle_admin_cards"):
            patcher = patch(f"app.api.entry_approval_pages.{target}", AsyncMock(return_value=True))
            setattr(self, target, patcher.start())
            self.addCleanup(patcher.stop)
        bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()))
        patcher = patch("app.api.entry_approval_pages.open_notify_bot_for_user", AsyncMock(return_value=(bot, True)))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        clear_settings_cache()
        await super().asyncTearDown()

    async def test_each_viewer_lists_only_its_own_requests(self):
        owner = (await self.call(OWNER_SESSION, "GET", "/entry-approvals")).json()["requests"]
        shop = (await self.call(SHOP_SESSION, "GET", "/entry-approvals")).json()["requests"]
        self.assertEqual(([r["customer"] for r in owner], [r["customer"] for r in shop]), ([102], [106]))

    async def test_approving_tells_the_customer_once(self):
        first = await self.call(OWNER_SESSION, "POST", "/entry-approvals/1/approve")
        self.assertEqual((first.status_code, first.json()), (200, {"ok": True, "status": "approved"}))
        again = await self.call(OWNER_SESSION, "POST", "/entry-approvals/1/approve")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.tell_customer.await_count, 1)
        self.assertEqual(self.settle_admin_cards.await_count, 1)

    async def test_a_shop_cannot_decide_the_platforms_request(self):
        response = await self.call(SHOP_SESSION, "POST", "/entry-approvals/1/block")
        self.assertEqual(response.status_code, 409)
        owner = await self.call(OWNER_SESSION, "POST", "/entry-approvals/2/approve")
        self.assertEqual(owner.status_code, 409)
        self.tell_customer.assert_not_awaited()

    async def test_staff_without_the_permission_and_unknown_actions_are_turned_away(self):
        self.assertEqual((await self.call(OWNER_SESSION, "POST", "/entry-approvals/1/delete")).status_code, 404)
        self.assertEqual((await self.call(OWNER_SESSION, "POST", "/entry-approvals/99999999999999999999/approve")).status_code, 422)
        async with self.Session() as session:
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(web_permissions="dashboard"))
            await session.commit()
        limited = {**SHOP_SESSION, "permissions": ["dashboard"]}
        self.assertEqual((await self.call(limited, "GET", "/entry-approvals")).status_code, 403)
        self.assertEqual((await self.call(limited, "POST", "/entry-approvals/2/approve")).status_code, 403)

    async def test_the_limits_tab_shows_the_switches_and_the_list(self):
        owner = await self.call(OWNER_SESSION, "GET", "/settings?tab=limits")
        for needle in ("s_entry_approval_enabled", "s_entry_approval_retry_hours", 'id="ea-requests"', "entry-approval-admin.js"):
            self.assertIn(needle, owner.text)
        shop = await self.call(SHOP_SESSION, "GET", "/shop-settings?tab=limits")
        self.assertIn("s_entry_approval_enabled", shop.text)

    async def test_dependent_fields_follow_the_switch_before_saving(self):
        # The switch shows its fields live only when the show-when script is on the page.
        for session, path in ((OWNER_SESSION, "/settings?tab=limits"), (SHOP_SESSION, "/shop-settings?tab=limits")):
            page = (await self.call(session, "GET", path)).text
            self.assertIn('data-show-when="s_entry_approval_enabled:1"', page)
            self.assertIn("/static/settings-show-when.js", page)

    async def test_a_shop_without_the_permission_does_not_see_the_switch(self):
        async with self.Session() as session:
            await session.execute(update(ResellerProfile).where(ResellerProfile.user_id == 5).values(web_permissions="dashboard,shop_settings"))
            await session.commit()
        shop = await self.call({**SHOP_SESSION, "permissions": ["dashboard", "shop_settings"]}, "GET", "/shop-settings?tab=limits")
        self.assertEqual(shop.status_code, 200)
        self.assertNotIn("s_entry_approval_enabled", shop.text)


class MigrationTests(unittest.TestCase):
    def test_upgrade_twice_and_downgrade_keep_existing_rows(self):
        path = Path(__file__).resolve().parents[1] / "alembic/versions/0114_entry_approvals.py"
        spec = importlib.util.spec_from_file_location("entry_approvals_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            engine = sa.create_engine(f"sqlite:///{Path(tmp) / 'm.db'}")
            with engine.begin() as conn:
                conn.execute(sa.text("CREATE TABLE bot_users (id INTEGER PRIMARY KEY)"))
                conn.execute(sa.text("INSERT INTO bot_users (id) VALUES (1)"))
                with Operations.context(MigrationContext.configure(conn)):
                    module.upgrade()
                    module.upgrade()
                    conn.execute(sa.text("INSERT INTO entry_approvals (bot_user_id) VALUES (1)"))
                    self.assertEqual(conn.execute(sa.text("SELECT shop_key, status FROM entry_approvals")).one(), (0, "pending"))
                    with self.assertRaises(sa.exc.IntegrityError):
                        conn.execute(sa.text("INSERT INTO entry_approvals (bot_user_id, shop_key) VALUES (1, 0)"))
                    module.downgrade()
                    self.assertFalse(sa.inspect(conn).has_table("entry_approvals"))
                    self.assertEqual(conn.execute(sa.text("SELECT id FROM bot_users")).scalar(), 1)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
