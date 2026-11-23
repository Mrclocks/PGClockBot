"""«افزودن سرویس قبلی» from the admin and shop bots: same checks as the web, bound to the previewed service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch


from app.bot import keyboards as kb
from app.bot.handlers import reseller_customer_services as shop_services
from app.bot.handlers import service_import_assign_bot as bot
from app.bot.handlers.action_reason import KEY_DONE_FOR, KEY_REASON
from app.db.models import BotUser, CustomerNotice
from app.services import bot_feedback_texts as feedback
from app.services.service_import_assign import ServicePreview
from tests.test_service_import_assign import OWNER, PLAT_LINK, SHOP, SHOP_LINK, AssignCase

REASON = "مشتری لینک را در پشتیبانی فرستاد"
ADMIN = BotUser(id=50, telegram_id=500, referral_code="adm", role="admin", username="boss")


class FakeState:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.state: Any = None

    async def get_data(self) -> dict[str, Any]:
        return dict(self.data)

    async def update_data(self, **values: Any) -> dict[str, Any]:
        self.data.update(values)
        return dict(self.data)

    async def set_data(self, data: dict[str, Any]) -> None:
        self.data = dict(data)

    async def set_state(self, state: Any = None) -> None:
        self.state = state

    async def clear(self) -> None:
        self.data.clear()


def callback(data: str) -> MagicMock:
    return MagicMock(data=data, answer=AsyncMock(), message=MagicMock())


def message(text: str) -> MagicMock:
    return MagicMock(text=text, answer=AsyncMock())


class BotFlowCase(AssignCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.state = FakeState()
        self.staff: dict | None = OWNER
        self.shop_profile = SimpleNamespace(user_id=5, id=1)
        self.screens: list[tuple[str, Any]] = []
        self.deleted: list[Any] = []

        async def bridge(_session, _user, *, is_reseller_bot):
            return SimpleNamespace(staff=self.staff) if self.staff else None

        async def shop_actor(_session, _user, *, is_reseller_bot, reseller_owner_id):
            return (5, self.shop_profile) if self.staff else (None, None)

        async def shop_staff(_session, *, profile, db_user):
            return self.staff

        async def edit(_message, text, reply_markup=None, **_kw):
            self.screens.append((text, reply_markup))

        async def finish(_message, _state, *, text, inline=None, **_kw):
            self.screens.append((text, inline))

        async def asked(_target, state, *, prompt, fsm_state, **_kw):
            await state.set_state(fsm_state)
            self.screens.append((prompt, None))

        async def delete(msg):
            self.deleted.append(msg)

        self.ask_reason = AsyncMock()
        for target, value in (
            ("resolve_bot_principal_bridge", bridge),
            ("load_reseller_actor", shop_actor),
            ("shop_bot_panel_staff", shop_staff),
            ("safe_edit_text", edit),
            ("finish_text_step", finish),
            ("ask_text", asked),
            ("delete_typed_link", delete),
            ("ask_reason", self.ask_reason),
        ):
            patcher = patch.object(bot, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def bot_kw(self, *, shop: bool) -> dict:
        return {"is_reseller_bot": shop, "reseller_owner_id": 5 if shop else None}

    async def call(self, handler, event, *, shop: bool = False) -> None:
        async with self.Session() as session:
            await handler(event, self.state, session, ADMIN, **self.bot_kw(shop=shop))

    async def preview(self, user_id: int, value: str, *, kind: str = "l", shop: bool = False) -> None:
        await self.call(bot.sia_pick_kind, callback(f"sia:k:{user_id}:{kind}"), shop=shop)
        await self.call(bot.sia_value_entered, message(value), shop=shop)

    async def confirm(self, user_id: int, *, notify: str = "0", shop: bool = False, with_reason: bool = True) -> MagicMock:
        event = callback(f"sia:y:{user_id}:{notify}")
        if with_reason:
            await self.state.update_data(**{KEY_REASON: REASON, KEY_DONE_FOR: event.data})
        await self.call(bot.sia_confirm, event, shop=shop)
        return event


class PlatformBotTests(BotFlowCase):
    async def test_the_owner_previews_confirms_with_a_reason_and_the_service_is_attached(self):
        await self.preview(1, PLAT_LINK)
        text, markup = self.screens[-1]
        self.assertIn("plat-one", text)
        self.assertNotIn("tok-plat", text)
        self.assertEqual(len(self.deleted), 1, "the typed link is removed from the chat")
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("sia:y:1:1", buttons)
        self.assertEqual(await self.rows(), [], "the preview attaches nothing")

        first = await self.confirm(1, with_reason=False)
        self.ask_reason.assert_awaited_once()
        self.assertIs(self.ask_reason.await_args.args[0], first)
        self.assertEqual(await self.rows(), [])

        await self.confirm(1, notify="1")
        (row,) = await self.rows()
        self.assertEqual((row.bot_user_id, row.pg_user_id, row.remark, row.shop_id), (1, 11, "imported", None))
        (log,) = await self.log_rows()
        self.assertEqual((log.reason, log.shop_id, log.actor_label), (REASON, None, "ربات: @boss"))
        (notice,) = await self.rows(CustomerNotice)
        self.assertEqual((notice.bot_user_id, notice.shop_id), (1, None))
        self.assertIn("plat-one", self.screens[-1][0])
        self.assertEqual(self.state.data.get(bot.KEY_VALUE), None, "the link is forgotten after the step")

    async def test_a_panel_username_works_and_the_message_is_kept(self):
        await self.preview(1, "plat-one", kind="u")
        self.assertEqual(self.deleted, [])
        await self.confirm(1)
        self.assertEqual([r.pg_user_id for r in await self.rows()], [11])
        self.assertEqual(await self.rows(CustomerNotice), [])

    async def test_confirming_without_a_preview_or_for_another_customer_attaches_nothing(self):
        event = await self.confirm(1)
        event.answer.assert_awaited_with(bot.EXPIRED_STEP, show_alert=True)
        await self.preview(1, PLAT_LINK)
        event = await self.confirm(2)
        event.answer.assert_awaited_with(bot.EXPIRED_STEP, show_alert=True)
        self.assertEqual(await self.rows(), [])

    async def test_a_refusal_is_shown_and_nothing_is_attached(self):
        await self.preview(1, SHOP_LINK)
        self.assertIn("مجاز نیست", self.screens[-1][0])
        event = await self.confirm(1)
        event.answer.assert_awaited_with(bot.EXPIRED_STEP, show_alert=True)
        self.assertEqual(await self.rows(), [])

    async def test_staff_who_are_not_the_owner_are_turned_away(self):
        self.staff = {"role": "admin", "permissions": []}
        event = callback("sia:o:1")
        await self.call(bot.sia_open, event)
        event.answer.assert_awaited_with(feedback.NO_ACCESS, show_alert=True)
        self.staff = None
        event = callback("sia:k:1:l")
        await self.call(bot.sia_pick_kind, event)
        event.answer.assert_awaited_with(feedback.NO_ACCESS, show_alert=True)


class ShopBotTests(BotFlowCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.staff = SHOP

    async def test_a_shop_operator_attaches_its_own_admins_service_to_its_customer(self):
        await self.preview(6, SHOP_LINK, shop=True)
        await self.confirm(6, notify="1", shop=True)
        (row,) = await self.rows()
        self.assertEqual((row.bot_user_id, row.shop_id), (6, 5))
        (log,) = await self.log_rows()
        self.assertEqual(log.shop_id, 5)
        (notice,) = await self.rows(CustomerNotice)
        self.assertEqual(notice.shop_id, 5)

    async def test_a_shop_cannot_reach_a_platform_or_foreign_customer(self):
        for user_id in (1, 10):
            with self.subTest(user_id=user_id):
                await self.preview(user_id, SHOP_LINK, shop=True)
                await self.confirm(user_id, shop=True)
        self.assertEqual(await self.rows(), [])

    async def test_without_the_permission_the_shop_bot_refuses(self):
        self.staff = {**SHOP, "permissions": ["customer_services"]}
        for handler, data in ((bot.sia_open, "sia:o:6"), (bot.sia_pick_kind, "sia:k:6:l"), (bot.sia_confirm, "sia:y:6:0")):
            event = callback(data)
            await self.call(handler, event, shop=True)
            event.answer.assert_awaited_with(feedback.NO_ACCESS, show_alert=True)
        self.assertEqual(await self.rows(), [])


class ButtonTests(AssignCase):
    def test_the_owners_services_screen_offers_the_button(self):
        markup = kb.admin_user_services_keyboard(1, [3])
        self.assertIn("sia:o:1", [b.callback_data for row in markup.inline_keyboard for b in row])

    def test_the_shop_services_screen_offers_it_only_with_the_permission(self):
        def callbacks(can_import: bool) -> list[str]:
            markup = shop_services._services_keyboard(6, [3], can_import=can_import)
            return [b.callback_data for row in markup.inline_keyboard for b in row]

        self.assertIn("sia:o:6", callbacks(True))
        self.assertNotIn("sia:o:6", callbacks(False))

    def test_callback_data_fits_telegram(self):
        for data in ("sia:o:", "sia:k::l", "sia:y::1"):
            self.assertLessEqual(len(data) + len(str(2**63)), 64)

    def test_the_preview_card_escapes_panel_text(self):
        preview = ServicePreview(11, "<b>x</b>", "<i>", "1", "2", "3", already_yours=False)
        text = bot._preview_text(preview)
        self.assertNotIn("<b>x</b>", text)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
