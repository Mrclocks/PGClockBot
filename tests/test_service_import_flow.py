"""An imported service renews and buys packs, but is never cancelled, refunded or deleted from the panel."""

from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from sqlalchemy import select

from app.bot import service_import_ui as ui_mod
from app.bot.handlers import service_import as flow
from app.bot.handlers import services as services_handlers
from app.bot.nav_inline import services_list_keyboard
from app.db.models import BotUser, Plan, ServiceAddonPack, UserService
from app.services.service_addons import create_addon_order
from app.services.service_cancellations import owned_cancellation_service
from app.services.service_import import (
    NOT_ENABLED,
    RATE_LIMITED,
    ImportOutcome,
    ImportResult,
    ServiceImportError,
)
from app.services.service_renewals import validate_renewal_selection
from app.services.users import reset_shop_reseller_id, set_shop_reseller_id
from tests.test_service_import import ImportCase, PLATFORM_ADMIN  # noqa: F401  (shared fake panel)


def _callback(data: str):
    message = AsyncMock()
    message.from_user = SimpleNamespace(is_bot=True)
    return SimpleNamespace(data=data, message=message, answer=AsyncMock())


def _state():
    state = AsyncMock()
    state.get_data = AsyncMock(return_value={})
    return state


class DownstreamBehaviourTests(ImportCase):
    async def _imported(self):
        result = await self.run_import(1, "tok-plat-0000001")
        return result.service.id

    async def test_an_imported_service_can_be_renewed_with_a_plan_the_customer_picks(self):
        service_id = await self._imported()
        token = set_shop_reseller_id(None)
        try:
            async with self.Session() as session:
                session.add(Plan(id=1, name="renew", price=1000, duration_days=30, data_limit_gb=10, is_active=True))
                await session.commit()
                service, plan = await session.get(UserService, service_id), await session.get(Plan, 1)
                terms = await validate_renewal_selection(session, user_id=1, service=service, plan=plan, shop_id=None)
                self.assertEqual(terms["duration_seconds"], 30 * 86400)
        finally:
            reset_shop_reseller_id(token)

    async def test_an_imported_service_can_buy_a_general_pack_but_not_a_category_pack(self):
        service_id = await self._imported()
        token = set_shop_reseller_id(None)
        try:
            async with self.Session() as session:
                session.add_all([
                    ServiceAddonPack(id=1, name="G", kind="volume", amount=5, price=800, is_general=True),
                    ServiceAddonPack(id=2, name="VIP", kind="volume", amount=5, price=900, category_id=1, is_general=False),
                ])
                await session.commit()
                service = await session.get(UserService, service_id)
                order = await create_addon_order(session, user_id=1, service=service, pack=await session.get(ServiceAddonPack, 1))
                self.assertEqual(order.amount, 800)
                with self.assertRaises(ValueError):
                    await create_addon_order(session, user_id=1, service=service, pack=await session.get(ServiceAddonPack, 2))
        finally:
            reset_shop_reseller_id(token)

    async def test_an_imported_or_linked_service_can_never_be_cancelled_for_a_refund(self):
        service_id = await self._imported()
        async with self.Session() as session:
            session.add(UserService(id=900, bot_user_id=1, pg_user_id=900, pg_username="old", remark="linked"))
            session.add(UserService(id=901, bot_user_id=1, pg_user_id=901, pg_username="sold", remark="order:1"))
            await session.commit()
            user = await session.get(BotUser, 1)
            for blocked in (service_id, 900):
                with self.assertRaises(ValueError):
                    await owned_cancellation_service(session, user, blocked, None)

    async def test_deleting_an_external_service_only_detaches_it(self):
        service_id = await self._imported()
        async with self.Session() as session:
            session.add(UserService(id=900, bot_user_id=1, pg_user_id=900, pg_username="old", remark="linked"))
            session.add(UserService(id=901, bot_user_id=1, pg_user_id=901, pg_username="sold", remark="order:1"))
            await session.commit()
            user = await session.get(BotUser, 1)
            for sid, expect_pg_delete in ((service_id, False), (900, False), (901, True)):
                with patch("app.services.bot_user_admin.admin_delete_service", AsyncMock()) as delete:
                    await services_handlers.svc_delete(_callback(f"svc:del:{sid}"), session, user, _state())
                self.assertEqual(delete.await_args.kwargs["delete_pg"], expect_pg_delete, sid)

    async def test_nobody_can_delete_someone_elses_service_from_the_list(self):
        service_id = await self._imported()
        async with self.Session() as session:
            other = await session.get(BotUser, 2)
            with patch("app.services.bot_user_admin.admin_delete_service", AsyncMock()) as delete:
                await services_handlers.svc_delete(_callback(f"svc:del:{service_id}"), session, other, _state())
            delete.assert_not_awaited()

    async def test_after_detaching_the_service_can_be_added_again_by_the_next_owner(self):
        service_id = await self._imported()
        async with self.Session() as session:
            await session.delete(await session.get(UserService, service_id))
            await session.commit()
        self.assertEqual((await self.run_import(2, "tok-plat-0000001")).outcome, ImportOutcome.ADDED)


class PurgeTests(ImportCase):
    async def test_deleting_a_customer_keeps_the_panel_users_of_external_services_and_clears_the_log(self):
        from app.db.models import ServiceImportAttempt
        from app.services.users import delete_bot_user

        await self.run_import(1, "tok-plat-0000001")
        await self.refused(1, "tok-plat-nothere-1")
        async with self.Session() as session:
            session.add(UserService(id=901, bot_user_id=1, pg_user_id=901, pg_username="sold", remark="order:1"))
            session.add(UserService(id=902, bot_user_id=1, pg_user_id=902, pg_username="old", remark="linked"))
            await session.commit()
            removed = AsyncMock(return_value="deleted")
            with (
                patch("app.services.pasarguard.get_pg", return_value=AsyncMock()),
                patch("app.services.bot_user_admin.ensure_pg_user_removed_or_absent", removed),
            ):
                await delete_bot_user(session, 1)
            self.assertEqual([call.args[1] for call in removed.await_args_list], [901])
            self.assertIsNone(await session.get(BotUser, 1))
            self.assertEqual((await session.scalars(select(ServiceImportAttempt))).all(), [])
            self.assertEqual((await session.scalars(select(UserService))).all(), [])


class EntryPointTests(unittest.TestCase):
    def test_the_button_only_appears_when_the_shop_enabled_the_feature(self):
        service = SimpleNamespace(id=1, pg_username="vpn1")
        on = {c.callback_data for row in services_list_keyboard([service], {}, allow_import=True).inline_keyboard for c in row}
        off = {c.callback_data for row in services_list_keyboard([service], {}).inline_keyboard for c in row}
        self.assertIn(ui_mod.ADD_CALLBACK, on)
        self.assertNotIn(ui_mod.ADD_CALLBACK, off)
        self.assertEqual(on - {ui_mod.ADD_CALLBACK}, off)

    def test_the_setting_defaults_to_off(self):
        from app.services.users import DEFAULT_SETTINGS

        self.assertFalse(ui_mod.import_enabled(DEFAULT_SETTINGS))
        self.assertFalse(ui_mod.import_enabled({}))
        self.assertFalse(ui_mod.import_enabled(None))
        self.assertTrue(ui_mod.import_enabled({"service_import_enabled": "1"}))

    def test_the_empty_list_offers_buying_and_connecting(self):
        data = [b.callback_data for row in ui_mod.empty_services_keyboard({}).inline_keyboard for b in row]
        self.assertEqual(data, ["shop:list", ui_mod.ADD_CALLBACK, "menu:home"])

    def test_callback_data_fits_telegram_limit(self):
        self.assertLessEqual(len(ui_mod.ADD_CALLBACK.encode()), 64)


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def _message(self, text: str):
        message = AsyncMock()
        message.text = text
        message.chat = SimpleNamespace(id=1)
        message.from_user = SimpleNamespace(is_bot=False)
        return message

    async def _run(self, text: str, *, enabled: bool = True, result=None, error: Exception | None = None, **kw):
        ui = {"service_import_enabled": "1" if enabled else "0"}
        importer = AsyncMock(return_value=result, side_effect=error)
        message = self._message(text)
        with (
            patch.object(flow, "get_all_settings", AsyncMock(return_value=ui)),
            patch.object(flow, "import_existing_service", importer),
            patch.object(flow, "delete_user_message", AsyncMock()) as forget,
            patch.object(flow, "finish_text_step", AsyncMock()) as finish,
        ):
            await flow.svc_import_link(message, AsyncMock(), BotUser(id=1, telegram_id=1), _state(), **kw)
        return message, importer, forget, finish

    async def test_the_message_carrying_the_link_is_deleted_and_the_prompt_becomes_the_card(self):
        service = SimpleNamespace(id=7, remark="imported")
        result = ImportResult(ImportOutcome.ADDED, service, {"username": "vpn1", "status": "active"})
        message, importer, forget, finish = await self._run("https://p.example/sub/abc12345", result=result)
        forget.assert_awaited_once()
        importer.assert_awaited_once()
        self.assertEqual(importer.await_args.kwargs["shop_id"], None)
        finish.assert_awaited_once()
        self.assertIn("✅", finish.await_args.kwargs["text"])
        data = [b.callback_data for row in finish.await_args.kwargs["inline"].inline_keyboard for b in row]
        self.assertIn("svc:more:7", data)
        message.answer.assert_not_awaited()

    async def test_a_shop_bot_passes_its_shop_to_the_checks(self):
        result = ImportResult(ImportOutcome.ADDED, SimpleNamespace(id=7, remark="imported"), {"username": "vpn1"})
        _, importer, _, _ = await self._run("abc12345XYZ", result=result, is_reseller_bot=True, reseller_owner_id=5)
        self.assertEqual(importer.await_args.kwargs["shop_id"], 5)

    async def test_a_refusal_keeps_the_customer_in_the_flow_with_a_cancel_button(self):
        message, _, _, finish = await self._run("abc12345XYZ", error=ServiceImportError("generic refusal"))
        message.answer.assert_awaited_once()
        self.assertIn("generic refusal", message.answer.await_args.args[0])
        data = [b.callback_data for row in message.answer.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual(data, [f"nv:cancel:{flow.CANCEL_CODE}"])
        finish.assert_not_awaited()

    async def test_rate_limit_ends_the_flow(self):
        message, _, _, finish = await self._run("abc12345XYZ", error=ServiceImportError(RATE_LIMITED))
        finish.assert_awaited_once()
        message.answer.assert_not_awaited()

    async def test_turning_the_feature_off_mid_flow_adds_nothing(self):
        message, importer, forget, _ = await self._run("abc12345XYZ", enabled=False)
        importer.assert_not_awaited()
        forget.assert_awaited_once()
        message.answer.assert_awaited_once_with(NOT_ENABLED)

    async def test_blank_input_asks_again_without_calling_the_service(self):
        message, importer, forget, _ = await self._run("   ")
        importer.assert_not_awaited()
        forget.assert_not_awaited()
        message.answer.assert_awaited_once()

    async def test_the_add_button_is_refused_when_the_shop_has_it_off(self):
        callback = _callback(ui_mod.ADD_CALLBACK)
        with (
            patch.object(flow, "get_all_settings", AsyncMock(return_value={"service_import_enabled": "0"})),
            patch.object(flow, "ask_text", AsyncMock()) as ask,
        ):
            await flow.svc_add_existing(callback, AsyncMock(), BotUser(id=1, telegram_id=1), _state())
        ask.assert_not_awaited()
        callback.answer.assert_awaited_once_with(NOT_ENABLED, show_alert=True)

    async def test_the_add_button_prompts_in_place_when_enabled(self):
        callback = _callback(ui_mod.ADD_CALLBACK)
        with (
            patch.object(flow, "get_all_settings", AsyncMock(return_value={"service_import_enabled": "1"})),
            patch.object(flow, "ask_text", AsyncMock()) as ask,
        ):
            await flow.svc_add_existing(callback, AsyncMock(), BotUser(id=1, telegram_id=1), _state())
        self.assertTrue(ask.await_args.kwargs["edit"])
        self.assertEqual(ask.await_args.kwargs["cancel_code"], flow.CANCEL_CODE)


class DeepLinkTests(ImportCase):
    async def _link(self, user_id: int, token: str, *, enabled: bool, **kw):
        from app.bot.handlers import start

        message = AsyncMock()
        async with self.Session() as session:
            user = await session.get(BotUser, user_id)
            with (
                patch.object(start, "get_all_settings", AsyncMock(return_value={"service_import_enabled": "1" if enabled else "0"})),
                patch.object(start, "render_home", AsyncMock()) as home,
            ):
                await start._link_subscription(message, session, user, token, **kw)
        return message, home

    async def test_without_the_feature_the_link_stays_view_only_but_is_now_checked(self):
        message, _ = await self._link(1, "tok-plat-0000001", enabled=False)
        rows = await self.rows()
        self.assertEqual([r.remark for r in rows], ["linked"])
        message.answer.assert_awaited_once()

    async def test_with_the_feature_the_link_is_imported(self):
        await self._link(1, "tok-plat-0000001", enabled=True)
        self.assertEqual([r.remark for r in await self.rows()], ["imported"])

    async def test_a_service_of_another_shop_is_refused_with_the_generic_message(self):
        message, home = await self._link(1, "tok-shop-0000001", enabled=True)
        self.assertEqual(await self.rows(), [])
        self.assertIn("قابل افزودن نیست", message.answer.await_args.args[0])
        home.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()


class AdminDeleteChoiceTests(ImportCase):
    """Operators detach external services by default; removing the panel account is an explicit choice."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add_all([
                UserService(id=901, bot_user_id=1, pg_user_id=901, pg_username="sold", remark="order:1"),
                UserService(id=902, bot_user_id=1, pg_user_id=902, pg_username="old", remark="linked"),
                UserService(id=903, bot_user_id=1, pg_user_id=903, pg_username="new", remark="imported"),
            ])
            await session.commit()

    def test_the_rule_for_each_kind_of_service(self):
        from app.services.service_import import admin_delete_pg_flag

        for remark, explicit, expected in (
            ("order:1", False, True), ("order:1", True, True), (None, False, True),
            ("linked", False, False), ("linked", True, True),
            ("imported", False, False), ("imported", True, True),
        ):
            self.assertEqual(admin_delete_pg_flag(SimpleNamespace(remark=remark), explicit=explicit), expected, (remark, explicit))

    async def _web_delete(self, service_id: int, **form):
        import httpx
        from fastapi import FastAPI

        from app.api.user_pages import register_user_pages

        async def get_db():
            async with self.Session() as session:
                yield session

        owner = {"role": "admin", "permissions": [], "org_principal_id": 1, "org_depth": 0,
                 "org_parent_id": None, "org_status": "active"}
        app = FastAPI()
        register_user_pages(app, render=lambda *a, **k: None, require_admin=lambda: owner, get_db=get_db)
        with patch("app.services.bot_user_admin.admin_delete_service", AsyncMock(return_value={})) as delete:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(f"/users/1/services/{service_id}/delete", data=form)
        return delete, response

    async def test_web_delete_of_a_sold_service_still_removes_the_panel_user(self):
        delete, _ = await self._web_delete(901)
        self.assertTrue(delete.await_args.kwargs["delete_pg"])

    async def test_web_delete_of_an_external_service_detaches_unless_the_operator_chose_otherwise(self):
        for service_id in (902, 903):
            delete, response = await self._web_delete(service_id)
            self.assertFalse(delete.await_args.kwargs["delete_pg"], service_id)
            self.assertEqual(response.status_code, 303)
            explicit, _ = await self._web_delete(service_id, delete_pg="1")
            self.assertTrue(explicit.await_args.kwargs["delete_pg"], service_id)
            for junk in ("0", "true", "yes", ""):
                delete, _ = await self._web_delete(service_id, delete_pg=junk)
                self.assertFalse(delete.await_args.kwargs["delete_pg"], junk)

    async def test_the_user_page_offers_two_separate_actions_for_external_services_only(self):
        from app.api.app import render

        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            services = (await session.scalars(select(UserService))).all()
            rows = [
                SimpleNamespace(service=SimpleNamespace(id=s.id, pg_username=s.pg_username, remark=s.remark, plan=None), info=None, error=None)
                for s in services
            ]
        html = _render_user_page(render, user, rows)
        self.assertEqual(html.count('name="delete_pg" value="1"'), 2)
        self.assertEqual(html.count("جدا کردن از مشتری"), 2)
        self.assertEqual(html.count(">حذف سرویس<"), 1)

    async def _bot(self, callback_data: str, handler):
        from app.bot.handlers import admin

        callback = _callback(callback_data)
        user = BotUser(id=99, telegram_id=99, role="admin")
        with patch("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=True)):
            async with self.Session() as session:
                with patch("app.services.bot_user_admin.admin_delete_service", AsyncMock(return_value={})) as delete:
                    await getattr(admin, handler)(callback, session, user)
        return callback, delete

    async def test_bot_confirmation_offers_the_two_choices_for_external_services(self):
        callback, _ = await self._bot("adm:users:svcdelask:1:903", "adm_users_service_delete_ask")
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(data, ["adm:users:svcdel:1:903", "adm:users:svcdel:1:903:pg", "adm:users:svc:1:903"])
        self.assertTrue(all(len(d.encode()) <= 64 for d in data))

    async def test_bot_confirmation_for_a_sold_service_is_unchanged(self):
        callback, _ = await self._bot("adm:users:svcdelask:1:901", "adm_users_service_delete_ask")
        markup = callback.message.edit_text.await_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertEqual(data, ["adm:users:svcdel:1:901", "adm:users:svc:1:901"])

    async def test_bot_delete_detaches_by_default_and_removes_the_panel_user_only_on_request(self):
        for sid, data, expected in (
            (901, "adm:users:svcdel:1:901", True),
            (902, "adm:users:svcdel:1:902", False),
            (903, "adm:users:svcdel:1:903", False),
            (903, "adm:users:svcdel:1:903:pg", True),
            (903, "adm:users:svcdel:1:903:xx", False),
        ):
            _, delete = await self._bot(data, "adm_users_service_delete")
            self.assertEqual(delete.await_args.kwargs["delete_pg"], expected, data)


def _render_user_page(render, user, rows) -> str:
    from starlette.requests import Request

    request = Request({
        "type": "http", "asgi": {"version": "3"}, "http_version": "1.1", "method": "GET", "scheme": "https",
        "path": "/users/1", "raw_path": b"/users/1", "query_string": b"", "headers": [(b"host", b"x.test")],
        "client": ("127.0.0.1", 1), "server": ("x.test", 443), "state": {},
    })
    template = render.__globals__["templates"].env.get_template("_user_edit_body.html")
    return template.render(request=request, user=user, services=rows, can_pg_delete=True)
