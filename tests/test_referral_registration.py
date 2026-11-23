"""Required referrals, registration retries, and isolated shop settings."""

from __future__ import annotations

import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from aiogram.types import CallbackQuery, Chat, Message, MessageEntity, ReplyKeyboardRemove, Update, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db import Base
from app.db.models import BotUser, PointsRule, ReferralEvent, ResellerProfile, Role
from app.services.referral_registration import ReferralRequired, referral_required_outbound
from app.services.users import clear_settings_cache, get_or_create_user, set_setting
from tests.postgres_support import add_shop_users, scoped_async_engine


class ReferralRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = scoped_async_engine()
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        # Shop 999 owns the "foreign" referrers below.
        await add_shop_users(sessions, 999)
        self.session = sessions()
        clear_settings_cache()
        self.settings = SimpleNamespace(admin_ids=[], bot_token="main-token")
        self.config_patch = patch("app.services.users.get_settings", return_value=self.settings)
        self.config_patch.start()

    async def asyncTearDown(self):
        self.config_patch.stop()
        clear_settings_cache()
        await self.session.close()
        await self.engine.dispose()

    async def user(self, tid=100, code="REF12345", **kwargs):
        user = BotUser(telegram_id=tid, referral_code=code, **kwargs)
        self.session.add(user)
        await self.session.commit()
        return user

    async def require_referral(self, *, scope=None):
        await set_setting(self.session, "referral_required", "1", reseller_id=scope)

    async def assert_no_registration(self, tid):
        self.assertIsNone(await self.session.scalar(select(BotUser.id).where(BotUser.telegram_id == tid)))

    async def test_required_missing_and_invalid_codes_do_not_create_users(self):
        await self.require_referral()
        for code in (None, "BAD12345", "not a code", ""):
            with self.assertRaises(ReferralRequired):
                await get_or_create_user(self.session, 200, referred_by_code=code)
            await self.assert_no_registration(200)

    async def test_turning_requirement_off_allows_retry_without_code(self):
        await self.require_referral()
        with self.assertRaises(ReferralRequired):
            await get_or_create_user(self.session, 200)
        await set_setting(self.session, "referral_required", "0")
        user = await get_or_create_user(self.session, 200)
        self.assertIsNone(user.referred_by_id)

    async def test_valid_code_registers_once_and_credits_referral_once(self):
        from app.services.loyalty import ensure_loyalty_defaults

        referrer = await self.user()
        await self.require_referral()
        await ensure_loyalty_defaults(self.session)
        rule = await self.session.scalar(select(PointsRule).where(PointsRule.event_key == "referral_signup"))
        rule.amount = 25
        await self.session.commit()
        user = await get_or_create_user(self.session, 200, referred_by_code=" ref_ref12345 ")
        self.assertEqual(user.referred_by_id, referrer.id)
        again = await get_or_create_user(self.session, 200, referred_by_code="REF12345")
        self.assertEqual(again.id, user.id)
        self.assertEqual(await self.session.scalar(select(func.count()).select_from(ReferralEvent)), 1)
        self.assertEqual(referrer.points_balance, 25)

    async def test_existing_user_is_exempt_and_attribution_is_not_rewritten(self):
        referrer = await self.user()
        existing = await self.user(200, "OLD12345")
        await self.require_referral()
        same = await get_or_create_user(self.session, 200, referred_by_code=referrer.referral_code)
        self.assertEqual(same.id, existing.id)
        self.assertIsNone(same.referred_by_id)

    async def test_platform_admin_can_register_without_referral(self):
        self.settings.admin_ids = [200]
        await self.require_referral()
        user = await get_or_create_user(self.session, 200)
        self.assertEqual(user.role, Role.ADMIN.value)

    async def test_blocked_referrer_is_rejected(self):
        await self.user(is_blocked=True)
        await self.require_referral()
        with self.assertRaises(ReferralRequired):
            await get_or_create_user(self.session, 200, referred_by_code="REF12345")
        await self.assert_no_registration(200)

    async def test_shop_settings_do_not_inherit_platform_requirement(self):
        owner = await self.user(role=Role.RESELLER.value)
        await self.require_referral()
        user = await get_or_create_user(self.session, 200, reseller_owner_id=owner.id)
        self.assertEqual(user.reseller_id, owner.id)
        self.assertIsNone(user.referred_by_id)

    async def test_shop_requirement_does_not_affect_platform(self):
        owner = await self.user(role=Role.RESELLER.value)
        await self.require_referral(scope=owner.id)
        with self.assertRaises(ReferralRequired):
            await get_or_create_user(self.session, 200, reseller_owner_id=owner.id)
        user = await get_or_create_user(self.session, 201)
        self.assertIsNone(user.referred_by_id)

    async def test_shop_accepts_own_customer_and_owner_but_rejects_foreign_codes(self):
        owner = await self.user(role=Role.RESELLER.value)
        customer = await self.user(101, "SHOP1234", reseller_id=owner.id)
        foreign = await self.user(102, "OTHER123", reseller_id=999)
        platform = await self.user(103, "MAIN1234")
        await self.require_referral(scope=owner.id)
        for code in (foreign.referral_code, platform.referral_code):
            with self.assertRaises(ReferralRequired):
                await get_or_create_user(self.session, 200, referred_by_code=code, reseller_owner_id=owner.id)
            await self.assert_no_registration(200)
        for tid, referrer in ((200, customer), (201, owner)):
            user = await get_or_create_user(self.session, tid, referred_by_code=referrer.referral_code, reseller_owner_id=owner.id)
            self.assertEqual(user.referred_by_id, referrer.id)
            self.assertEqual(user.reseller_id, owner.id)

    async def test_platform_rejects_shop_referral_and_self_referral(self):
        from app.services.referral_registration import find_registration_referrer

        referrer = await self.user(reseller_id=999)
        await self.require_referral()
        with self.assertRaises(ReferralRequired):
            await get_or_create_user(self.session, 200, referred_by_code=referrer.referral_code)
        self.assertIsNone(await find_registration_referrer(self.session, referrer.referral_code, telegram_id=100, reseller_owner_id=999))

    def message(self, text, tid=200):
        return Message(
            message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=tid, type="private"),
            from_user=User(id=tid, is_bot=False, first_name="New"), text=text,
        )

    async def dispatch(self, event, handler=None, state=None):
        from app.bot.middlewares import UserMiddleware

        handler = handler or AsyncMock()
        data = {"session": self.session, "state": state or AsyncMock()}
        with patch("app.bot.middlewares.get_settings", return_value=self.settings):
            await UserMiddleware()(handler, event, data)
        return handler, data

    async def test_start_and_other_commands_display_custom_prompt_without_registration(self):
        await self.require_referral()
        await set_setting(self.session, "referral_required_text", "Get a code from a friend.")
        for text in ("/start", "/shop", "/start ref_BAD12345"):
            with patch.object(Message, "answer", new=AsyncMock()) as answer:
                handler, _ = await self.dispatch(Update(update_id=1, message=self.message(text)))
            handler.assert_not_awaited()
            self.assertIn("Get a code from a friend.", answer.await_args.args[0])
            self.assertIsInstance(answer.await_args.kwargs["reply_markup"], ReplyKeyboardRemove)
            await self.assert_no_registration(200)

    async def test_deeplink_valid_code_passes_to_start_handler(self):
        referrer = await self.user()
        await self.require_referral()
        handler, data = await self.dispatch(Update(update_id=1, message=self.message("/start ref_ref12345")))
        handler.assert_awaited_once()
        self.assertEqual(data["db_user"].referred_by_id, referrer.id)

    async def test_plain_code_after_prompt_continues_welcome_flow(self):
        await self.user()
        await self.require_referral()
        state = AsyncMock()
        with (
            patch.object(Message, "answer", new=AsyncMock()) as answer,
            patch("app.services.reseller_access.effective_menu_role", new=AsyncMock(return_value="user")),
            patch("app.bot.handlers.start.render_home", new=AsyncMock()) as home,
        ):
            handler, _ = await self.dispatch(Update(update_id=1, message=self.message("ref12345")), state=state)
        handler.assert_not_awaited()
        state.clear.assert_awaited_once()
        home.assert_awaited_once()
        self.assertIn("کد دعوت ثبت شد", answer.await_args.args[0])

    async def test_plain_code_still_respects_terms(self):
        await self.user()
        await self.require_referral()
        await set_setting(self.session, "terms_entry_enabled", "1")
        await set_setting(self.session, "terms_entry_text", "Accept these rules.")
        with (
            patch.object(Message, "answer", new=AsyncMock()),
            patch("app.services.reseller_access.effective_menu_role", new=AsyncMock(return_value="user")),
            patch("app.bot.handlers.terms.show_terms_prompt", new=AsyncMock()) as terms,
            patch("app.bot.handlers.start.render_home", new=AsyncMock()) as home,
        ):
            await self.dispatch(self.message("REF12345"))
        terms.assert_awaited_once()
        home.assert_not_awaited()

    async def test_plain_code_still_respects_force_join(self):
        await self.user()
        await self.require_referral()
        await set_setting(self.session, "force_join_enabled", "1")
        await set_setting(self.session, "force_join_channel", "@required_channel")
        with (
            patch.object(Message, "answer", new=AsyncMock()) as answer,
            patch("app.services.reseller_access.effective_menu_role", new=AsyncMock(return_value="user")),
            patch("app.bot.middlewares.check_force_join_all", new=AsyncMock(return_value=(["@required_channel"], []))) as check,
            patch("app.bot.handlers.start.render_home", new=AsyncMock()) as home,
        ):
            await self.dispatch(self.message("REF12345"))
        check.assert_awaited_once()
        home.assert_not_awaited()
        self.assertIsNotNone(answer.await_args.kwargs["reply_markup"])

    async def test_existing_users_code_messages_keep_their_normal_handler(self):
        await self.user()
        await self.user(200, "USER1234")
        await self.require_referral()
        with patch("app.bot.handlers.start.cmd_start", new=AsyncMock()) as start:
            handler, data = await self.dispatch(self.message("REF12345"))
        handler.assert_awaited_once()
        start.assert_not_awaited()
        self.assertIsNone(data["db_user"].referred_by_id)

    async def test_panel_and_shop_forms_save_requirement_and_can_disable_it(self):
        from starlette.requests import Request

        from app.api.app import create_api_app
        from app.services.users import get_all_settings

        owner = await self.user(role=Role.RESELLER.value)
        self.session.add(ResellerProfile(user_id=owner.id, is_active=True))
        await self.session.commit()
        with tempfile.TemporaryDirectory() as tmpdir, patch("app.api.app.DATA_DIR", Path(tmpdir)):
            app = create_api_app()
            for path, staff, scope in (
                ("/settings", {"role": "admin"}, None),
                ("/shop-settings", {"role": "reseller", "bot_user_id": owner.id}, owner.id),
            ):
                save = next(route.endpoint for route in app.routes if getattr(route, "path", None) == path and "POST" in route.methods)
                for enabled in (True, False):
                    # Required-referral lives on the limits settings tab.
                    limits_form = {
                        "s_referral_required_text": f"Instructions for {path}",
                    }
                    if enabled:
                        limits_form["s_referral_required"] = "1"
                    limits_body = urlencode(limits_form).encode()

                    async def receive_limits(body=limits_body):
                        return {"type": "http.request", "body": body, "more_body": False}

                    limits_request = Request({
                        "type": "http", "method": "POST", "path": path, "query_string": b"tab=limits",
                        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
                    }, receive_limits)
                    response = await save(request=limits_request, staff=staff, session=self.session)
                    self.assertEqual(response.status_code, 303)
                    ui = await get_all_settings(self.session, reseller_id=scope)
                    self.assertEqual(ui["referral_required"], "1" if enabled else "0")
                    self.assertEqual(ui["referral_required_text"], f"Instructions for {path}")

                    # Invite copy stays on the loyalty / referral tab.
                    invite_body = urlencode({"s_referral_text": "Invite {code}"}).encode()

                    async def receive_invite(body=invite_body):
                        return {"type": "http.request", "body": body, "more_body": False}

                    invite_request = Request({
                        "type": "http", "method": "POST", "path": path, "query_string": b"tab=loyalty",
                        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
                    }, receive_invite)
                    response = await save(request=invite_request, staff=staff, session=self.session)
                    self.assertEqual(response.status_code, 303)
                    ui = await get_all_settings(self.session, reseller_id=scope)
                    self.assertEqual(ui["referral_text"], "Invite {code}")

            # The shop's save must leave the platform's message intact.
            ui = await get_all_settings(self.session)
            self.assertEqual(ui["referral_required_text"], "Instructions for /settings")

    async def test_callback_cannot_bypass_required_registration(self):
        await self.require_referral()
        callback = CallbackQuery(
            id="callback", from_user=User(id=200, is_bot=False, first_name="New"),
            chat_instance="test", data="shop:home", message=self.message("old menu"),
        )
        with patch.object(Message, "answer", new=AsyncMock()), patch.object(CallbackQuery, "answer", new=AsyncMock()) as answer:
            handler, _ = await self.dispatch(Update(update_id=1, callback_query=callback))
        handler.assert_not_awaited()
        answer.assert_awaited_once()
        await self.assert_no_registration(200)


class ReferralPromptTests(unittest.TestCase):
    def test_empty_custom_prompt_falls_back_and_invalid_code_explains_retry(self):
        from app.services.users import DEFAULT_SETTINGS

        text, _ = referral_required_outbound({"referral_required_text": "  "}, invalid=True)
        self.assertIn(DEFAULT_SETTINGS["referral_required_text"], text)
        self.assertIn("کد معرف معتبر نیست", text)

    def test_rich_prompt_preserves_entities_when_error_is_appended(self):
        from app.services.rich_text import pack_rich_text

        raw = pack_rich_text("Code required", [MessageEntity(type="bold", offset=0, length=4)])
        text, kwargs = referral_required_outbound({"referral_required_text": raw}, invalid=True)
        self.assertTrue(text.startswith("Code required"))
        self.assertEqual(kwargs["entities"][0].length, 4)
        self.assertIsNone(kwargs["parse_mode"])
