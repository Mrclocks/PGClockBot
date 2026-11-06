"""Trial users and staff action log in the bot: shop scope, permission and owner-only reading."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bot.handlers import staff_audit_view as audit
from app.bot.handlers import trial_users_view as trial
from app.db.models import Base, BotUser, CampaignRecipient, MarketingPreference, Order, Plan, ShopStaffAction, TargetedCampaign
from sqlalchemy import select
from app.services import bot_feedback_texts as feedback
from tests.test_update_owner_only import OWNER_SESSION

SHOP_STAFF = {"role": "reseller", "bot_user_id": 5}
CTX = {"is_reseller_bot": False, "reseller_owner_id": None}


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TrialMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user"),
            BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5),
            BotUser(id=7, telegram_id=107, referral_code="u7", role="user", reseller_id=5, is_blocked=True),
            BotUser(id=8, telegram_id=108, referral_code="u8", role="user", reseller_id=5),
        ])
        self.session.add(Plan(id=1, name="Trial", price=0, is_trial=True, is_active=True))
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=1, amount=0, status="delivered"),
            Order(id=11, user_id=6, plan_id=1, reseller_id=5, amount=0, status="delivered"),
            Order(id=12, user_id=7, plan_id=1, reseller_id=5, amount=0, status="delivered"),
            Order(id=13, user_id=8, plan_id=1, reseller_id=5, amount=0, status="delivered"),
            MarketingPreference(user_id=8, shop_key=5, enabled=False),
        ])
        await self.session.commit()
        self.state = AsyncMock()
        self.stored: dict = {"tru_shop": 5, "tru_filter": "all"}
        self.state.get_data = AsyncMock(side_effect=lambda: dict(self.stored))
        self.state.update_data = AsyncMock(side_effect=lambda **kw: self.stored.update(kw))

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def type_text(self, text: str, staff: dict | None):
        message = AsyncMock()
        message.text = text
        with patch.object(trial, "resolve_feature_staff", AsyncMock(return_value=staff)), \
                patch.object(trial, "try_legacy_cancel", AsyncMock(return_value=False)), \
                patch.object(trial, "finish_text_step", AsyncMock()) as finish:
            await trial.receive_message_text(message, self.state, session=self.session, db_user=SimpleNamespace(id=9), **CTX)
        return message, finish

    async def confirm(self, staff: dict | None):
        callback = AsyncMock()
        callback.data = "tru:ok"
        callback.message = AsyncMock()
        with patch.object(trial, "resolve_feature_staff", AsyncMock(return_value=staff)), \
                patch.object(trial, "safe_edit_text", AsyncMock()):
            await trial.confirm_message(callback, self.state, session=self.session, db_user=SimpleNamespace(id=9), **CTX)
        return callback

    async def test_audience_is_the_shops_own_trial_takers(self) -> None:
        _, finish = await self.type_text("سلام", SHOP_STAFF)
        self.assertEqual(sorted(self.stored["tru_ids"]), [6, 7, 8])
        self.assertIn("tru:ok", callbacks(finish.await_args.kwargs["inline"]))

    async def test_confirmation_queues_one_campaign_for_eligible_customers_only(self) -> None:
        await self.type_text("سلام", SHOP_STAFF)
        callback = await self.confirm(SHOP_STAFF)
        self.assertIn("کمپین", callback.answer.await_args.args[0])
        recipients = (await self.session.scalars(select(CampaignRecipient.user_id))).all()
        self.assertEqual(sorted(recipients), [6])
        self.assertEqual(len((await self.session.scalars(select(TargetedCampaign.id))).all()), 1)
        again = await self.confirm(SHOP_STAFF)
        again.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)
        self.assertEqual(len((await self.session.scalars(select(TargetedCampaign.id))).all()), 1)

    async def test_a_raced_confirmation_with_the_same_token_reuses_the_one_campaign(self) -> None:
        await self.type_text("سلام", SHOP_STAFF)
        stale = dict(self.stored)
        await self.confirm(SHOP_STAFF)
        self.stored.update(stale)  # the second tap read the state before the first one cleared it
        callback = await self.confirm(SHOP_STAFF)
        self.assertIn("کمپین", callback.answer.await_args.args[0])
        self.assertEqual(len((await self.session.scalars(select(TargetedCampaign.id))).all()), 1)

    async def test_a_new_confirmation_gets_its_own_token_and_campaign(self) -> None:
        await self.type_text("اول", SHOP_STAFF)
        first = self.stored["tru_token"]
        await self.confirm(SHOP_STAFF)
        await self.type_text("دوم", SHOP_STAFF)
        self.assertNotEqual(self.stored["tru_token"], first)
        await self.confirm(SHOP_STAFF)
        self.assertEqual(len((await self.session.scalars(select(TargetedCampaign.id))).all()), 2)

    async def test_without_the_manage_permission_nothing_is_queued(self) -> None:
        message, finish = await self.type_text("سلام", None)
        message.answer.assert_awaited_once_with(feedback.NO_ACCESS_REPLY)
        finish.assert_not_awaited()
        self.stored.update(tru_ids=[6], tru_text="x")
        callback = await self.confirm(None)
        callback.answer.assert_awaited_once_with(feedback.INVALID, show_alert=True)
        self.assertEqual((await self.session.scalars(select(TargetedCampaign.id))).all(), [])

    async def test_another_shops_scope_in_state_is_refused(self) -> None:
        self.stored["tru_shop"] = 99
        message, _ = await self.type_text("سلام", SHOP_STAFF)
        message.answer.assert_awaited_once_with(feedback.NO_ACCESS_REPLY)

    async def test_empty_or_script_text_is_rejected_before_any_audience_lookup(self) -> None:
        for text in ("   ", "<script>x</script>"):
            message, finish = await self.type_text(text, SHOP_STAFF)
            message.answer.assert_awaited_once()
            finish.assert_not_awaited()


class StaffViewsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.session.add_all([
            BotUser(id=5, telegram_id=105, referral_code="s5", role="reseller"),
            BotUser(id=7, telegram_id=107, referral_code="s7", role="reseller"),
            BotUser(id=1, telegram_id=101, referral_code="u1", role="user", full_name="<Platform Guest>"),
            BotUser(id=6, telegram_id=106, referral_code="u6", role="user", reseller_id=5, full_name="Shopper"),
        ])
        self.session.add(Plan(id=1, name="Trial", price=0, is_trial=True, is_active=True))
        await self.session.flush()
        self.session.add_all([
            Order(id=10, user_id=1, plan_id=1, amount=0, status="delivered"),
            Order(id=11, user_id=6, plan_id=1, reseller_id=5, amount=0, status="delivered"),
            ShopStaffAction(shop_id=5, actor_kind="shop_owner", actor_label="<owner>", action="wallet_credit", target_type="user", target_id=6, reason="<b>gift</b>"),
            ShopStaffAction(shop_id=None, actor_kind="platform_owner", actor_label="root", action="wallet_credit", target_type="user", target_id=1),
        ])
        await self.session.commit()

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self.engine.dispose()

    async def trial_view(self, data: str, staff: dict | None):
        callback = AsyncMock()
        callback.data = data
        callback.message = AsyncMock()
        with patch.object(trial, "resolve_feature_staff", AsyncMock(return_value=staff)), \
                patch.object(trial, "safe_edit_text", AsyncMock()) as edit:
            await trial.show_trial_users(callback, session=self.session, db_user=SimpleNamespace(id=9), **CTX)
        return callback, edit

    async def audit_view(self, staff: dict | None, db_user_id: int):
        callback = AsyncMock()
        callback.data = "saud:l:0"
        callback.message = AsyncMock()
        with patch.object(audit, "resolve_feature_staff", AsyncMock(return_value=staff)), \
                patch.object(audit, "safe_edit_text", AsyncMock()) as edit:
            await audit.show_staff_actions(callback, session=self.session, db_user=SimpleNamespace(id=db_user_id), **CTX)
        return callback, edit

    async def test_trial_list_is_limited_to_the_acting_shop(self) -> None:
        _, edit = await self.trial_view("tru:l:all:0", SHOP_STAFF)
        text = edit.await_args.args[1]
        found = callbacks(edit.await_args.kwargs["reply_markup"])
        self.assertIn("Shopper", text)
        self.assertNotIn("Platform Guest", text)
        self.assertIn("res:user:6", found)
        self.assertNotIn("adm:users:view:1", found)

    async def test_platform_owner_sees_platform_trials_with_escaped_names(self) -> None:
        _, edit = await self.trial_view("tru:l:all:0", OWNER_SESSION)
        text = edit.await_args.args[1]
        self.assertIn("&lt;Platform Guest&gt;", text)
        self.assertNotIn("<Platform Guest>", text)
        self.assertNotIn("Shopper", text)

    async def test_trial_list_denied_without_permission(self) -> None:
        callback, edit = await self.trial_view("tru:l:all:0", None)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        edit.assert_not_awaited()

    async def test_unknown_filter_falls_back_to_all(self) -> None:
        self.assertEqual(trial.normalize_filter("../x"), "all")

    async def test_shop_owner_reads_only_the_shops_log_and_text_is_escaped(self) -> None:
        _, edit = await self.audit_view(SHOP_STAFF, 5)
        text = edit.await_args.args[1]
        self.assertIn("&lt;owner&gt;", text)
        self.assertIn("&lt;b&gt;gift&lt;/b&gt;", text)
        self.assertNotIn("root", text)

    async def test_a_shop_bot_admin_who_is_not_the_owner_is_denied(self) -> None:
        callback, edit = await self.audit_view(SHOP_STAFF, 7)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        edit.assert_not_awaited()

    async def test_platform_owner_reads_the_platform_log_only(self) -> None:
        _, edit = await self.audit_view(OWNER_SESSION, 99)
        text = edit.await_args.args[1]
        self.assertIn("root", text)
        self.assertNotIn("&lt;owner&gt;", text)

    async def test_log_is_denied_without_staff(self) -> None:
        callback, edit = await self.audit_view(None, 5)
        callback.answer.assert_awaited_once_with(feedback.NO_ACCESS, show_alert=True)
        edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
