"""Shops and the Owner can edit the notices customers get about their service, safely.

A shop's text is used only for the notices its own bot sends; values are escaped; an empty text,
an unknown placeholder or a text Telegram refuses falls back to the built-in message.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch


from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot.handlers import admin_settings, reseller_settings
from app.db.models import CustomerNotice, ResellerSetting
from app.services.bot_setting_values import wants_default_text
from app.services.customer_action_notices import ACTION_NOTICE_TITLES, CLEANUP_NOTICE_TEXTS, custom_notice_text, deliver_notice
from app.services.customer_notice_queue import OUTCOME_FAILED, OUTCOME_SENT
from app.services.customer_notice_texts import NOTICE_TEXT_KEYS, notice_text_key
from app.services.users import SETTING_GROUPS, TAB_SETTING_GROUPS, clear_settings_cache, keys_for_tab
from tests.postgres_support import add_shop_users
from tests.test_customer_services_web_permission import CUSTOMER_ID, SHOP_ID, CustomerServicesWebPermissionTests
from tests.test_update_owner_only import OWNER_SESSION, SHOP_SESSION, RoutesHarness

WEB_SHOP_ID = 5  # the shop of SHOP_SESSION in RoutesHarness


class CoverageTests(unittest.TestCase):
    def test_every_notice_has_an_editable_text(self) -> None:
        for action in (*ACTION_NOTICE_TITLES, *CLEANUP_NOTICE_TEXTS):
            self.assertIn(notice_text_key(action), NOTICE_TEXT_KEYS, action)

    def test_texts_are_on_the_notifications_tab_and_in_both_bots(self) -> None:
        tab_keys = keys_for_tab("notifications")
        for key in NOTICE_TEXT_KEYS:
            with self.subTest(key=key):
                self.assertIn(key, tab_keys)
                self.assertIn(key, admin_settings.FIELDS)
                self.assertIn(key, reseller_settings.FIELDS)
        groups = TAB_SETTING_GROUPS["notifications"]
        self.assertTrue(all(name in SETTING_GROUPS for name in groups))


class RenderTests(unittest.TestCase):
    def test_empty_text_keeps_the_built_in_notice(self) -> None:
        self.assertIsNone(custom_notice_text({}, "service_delete", service_name="a", reason="b"))
        self.assertIsNone(custom_notice_text({notice_text_key("service_delete"): "   "}, "service_delete", service_name="a", reason="b"))

    def test_values_are_escaped_and_only_known_placeholders_resolve(self) -> None:
        ui = {
            notice_text_key("service_delete"): "<b>حذف</b> {service_name} — {reason} — {shop_title} {delete_date} {secret}",
            "shop_title": "<i>فروشگاه</i>",
        }
        text = custom_notice_text(ui, "service_delete", service_name="<script>x</script>", reason="a & b")
        self.assertIn("<b>حذف</b>", text)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;", text)
        self.assertIn("a &amp; b", text)
        self.assertIn("&lt;i&gt;فروشگاه&lt;/i&gt;", text)
        self.assertIn("{secret}", text)
        self.assertNotIn("<script>", text)

    def test_cleanup_notice_takes_the_date_from_the_reason(self) -> None:
        ui = {notice_text_key("cleanup_warning"): "{service_name} تا {delete_date} حذف می‌شود {reason}"}
        text = custom_notice_text(ui, "cleanup_warning", service_name="svc", reason="۱۴۰۵/۹/۲")
        self.assertEqual(text, "svc تا ۱۴۰۵/۹/۲ حذف می‌شود ")

    def test_format_injection_is_never_evaluated(self) -> None:
        ui = {notice_text_key("service_renew"): "{service_name.__class__} {reason[0]} {reason!r}"}
        text = custom_notice_text(ui, "service_renew", service_name="svc", reason="r")
        self.assertEqual(text, "{service_name.__class__} {reason[0]} {reason!r}")


class DeliveryTests(CustomerServicesWebPermissionTests):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        # A second shop, for its own notice text.
        await add_shop_users(async_sessionmaker(self.engine), SHOP_ID + 1)
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    def notice(self, **kw) -> CustomerNotice:
        values = dict(id=1, shop_id=SHOP_ID, bot_user_id=CUSTOMER_ID, telegram_id=107, action="service_delete",
                      service_name="svc12", reason="درخواست مشتری")
        return CustomerNotice(**{**values, **kw})

    async def set_shop_text(self, shop_id: int, text: str) -> None:
        async with AsyncSession(bind=self.engine) as session:
            session.add(ResellerSetting(reseller_user_id=shop_id, key=notice_text_key("service_delete"), value=text))
            await session.commit()
        clear_settings_cache()

    async def deliver(self, notice: CustomerNotice, bot: MagicMock):
        async def bot_for(_shop_id):
            return bot

        async with AsyncSession(bind=self.engine) as session:
            return await deliver_notice(session, bot_for, notice)

    async def test_the_shops_text_is_sent_by_its_bot(self) -> None:
        await self.set_shop_text(SHOP_ID, "سرویس {service_name} حذف شد؛ {reason}")
        bot = MagicMock(send_message=AsyncMock())
        outcome = await self.deliver(self.notice(), bot)
        self.assertEqual(outcome.kind, OUTCOME_SENT)
        self.assertEqual(bot.send_message.await_args.args[1], "سرویس svc12 حذف شد؛ درخواست مشتری")

    async def test_another_shops_text_is_never_used(self) -> None:
        await self.set_shop_text(SHOP_ID + 1, "متن فروشگاه دیگر")
        bot = MagicMock(send_message=AsyncMock())
        await self.deliver(self.notice(), bot)
        sent = bot.send_message.await_args.args[1]
        self.assertNotIn("متن فروشگاه دیگر", sent)
        self.assertIn(ACTION_NOTICE_TITLES["service_delete"], sent)

    async def test_a_refused_custom_text_falls_back_to_the_built_in_notice(self) -> None:
        await self.set_shop_text(SHOP_ID, "<b>باز مانده {service_name}")
        refused = TelegramBadRequest(method=MagicMock(), message="can't parse entities")
        bot = MagicMock(send_message=AsyncMock(side_effect=[refused, None]))
        outcome = await self.deliver(self.notice(), bot)
        self.assertEqual(outcome.kind, OUTCOME_SENT)
        self.assertEqual(bot.send_message.await_count, 2)
        self.assertIn(ACTION_NOTICE_TITLES["service_delete"], bot.send_message.await_args.args[1])

    async def test_a_refused_built_in_notice_is_not_resent(self) -> None:
        refused = TelegramBadRequest(method=MagicMock(), message="bad")
        bot = MagicMock(send_message=AsyncMock(side_effect=refused))
        outcome = await self.deliver(self.notice(), bot)
        self.assertEqual(outcome.kind, OUTCOME_FAILED)
        self.assertEqual(bot.send_message.await_count, 1)


class BotEditTests(unittest.IsolatedAsyncioTestCase):
    def test_default_word_only_clears_texts_with_a_built_in_fallback(self) -> None:
        self.assertTrue(wants_default_text(notice_text_key("service_renew"), " پیش‌فرض "))
        self.assertTrue(wants_default_text("user_alert_time_text", "default"))
        self.assertFalse(wants_default_text("welcome_text", "پیش‌فرض"))
        self.assertFalse(wants_default_text(notice_text_key("service_renew"), "متن تازه"))

    async def test_shop_bot_restores_the_default_for_its_own_copy(self) -> None:
        key = notice_text_key("pg_user_disable")
        state = AsyncMock(get_data=AsyncMock(return_value={"edit_key": key}))
        message = MagicMock(text="پیش‌فرض", entities=None, answer=AsyncMock(), delete=AsyncMock())
        with (
            patch.object(reseller_settings, "_gate", AsyncMock(return_value=(SimpleNamespace(user_id=SHOP_ID), None))),
            patch.object(reseller_settings, "set_setting", AsyncMock()) as saved,
            patch.object(reseller_settings, "_close_input_to_location", AsyncMock()),
        ):
            await reseller_settings.settings_edit_save(message, state, MagicMock(), SimpleNamespace(id=1))
        self.assertEqual(saved.await_args.args[1:], (key, ""))
        self.assertEqual(saved.await_args.kwargs["reseller_id"], SHOP_ID)

    async def test_owner_prompt_lists_the_placeholders(self) -> None:
        actor = patch.object(admin_settings, "_ensure_settings_actor", AsyncMock(return_value=True))
        actor.start()
        self.addCleanup(actor.stop)
        callback = MagicMock(data=f"adm:st:edit:{notice_text_key('cleanup_warning')}", answer=AsyncMock(), message=MagicMock())
        with (
            patch.object(admin_settings, "_is_admin", return_value=True),
            patch.object(admin_settings, "get_setting", AsyncMock(return_value="")),
            patch("app.bot.nav_input.ask_text", AsyncMock()) as asked,
        ):
            await admin_settings.settings_edit_ask(callback, AsyncMock(), MagicMock(), SimpleNamespace(id=1))
        prompt = asked.await_args.kwargs["prompt"]
        self.assertIn("{delete_date}", prompt)
        self.assertIn("پیش‌فرض", prompt)
        self.assertLessEqual(len(callback.data.encode()), 64)


if __name__ == "__main__":
    unittest.main()



class WebTabTests(RoutesHarness):
    """The texts are on the owner's and the shop's notifications tab; a shop saves only its own copy."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        clear_settings_cache()
        self.addCleanup(clear_settings_cache)

    async def test_both_pages_render_the_text_fields(self) -> None:
        field = f'name="s_{notice_text_key("pg_user_revoke_sub")}"'
        for session, path in ((OWNER_SESSION, "/settings?tab=notifications"), (SHOP_SESSION, "/shop-settings?tab=notifications")):
            with self.subTest(path=path):
                response = await self.call(session, "GET", path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.text)

    async def test_shop_save_stores_only_the_shops_copy(self) -> None:
        key = notice_text_key("service_renew")
        cookie = f"session={self.signer.dumps(SHOP_SESSION)}; csrf=token"
        headers = {"cookie": cookie, "x-csrf-token": "token", "origin": "http://panel.test"}
        response = await self.client.post(
            "/shop-settings?tab=notifications", headers=headers, data={f"s_{key}": "تمدید شد: {service_name}"}
        )
        self.assertEqual(response.status_code, 303)
        async with async_sessionmaker(self.engine, expire_on_commit=False)() as session:
            rows = (
                await session.execute(
                    select(ResellerSetting.reseller_user_id, ResellerSetting.value).where(ResellerSetting.key == key)
                )
            ).all()
        self.assertEqual([(int(r[0]), r[1]) for r in rows], [(WEB_SHOP_ID, "تمدید شد: {service_name}")])
