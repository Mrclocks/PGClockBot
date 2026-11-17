"""«دریافت تست» placement: home offer, main keyboard or both, chosen per shop in the trial plan settings."""

from __future__ import annotations

import os
import tempfile
import unittest
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import menu_nav, screen_replay
from app.bot import reply_keyboards as rk
from app.bot.handlers import admin_plans, home_trial, start, trial_menu
from app.db.models import BotUser, Plan, TrialClaim, UserService
from app.services import trial_offer as offer
from app.services.users import (
    DEFAULT_SETTINGS,
    get_all_settings,
    get_setting,
    reset_shop_reseller_id,
    set_settings_bulk,
    set_shop_reseller_id,
)
from tests.test_service_import import ImportCase

TRIAL_LABEL = DEFAULT_SETTINGS["btn_trial"]
SHOP, OTHER_SHOP = 5, 9


def _texts(markup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


class PlacementRuleTests(unittest.TestCase):
    def test_a_missing_or_unknown_placement_reads_as_the_home_offer(self) -> None:
        for ui in (None, {}, {offer.TRIAL_PLACEMENT_KEY: ""}, {offer.TRIAL_PLACEMENT_KEY: "sideways"}):
            with self.subTest(ui=ui):
                self.assertEqual(offer.trial_placement(ui), offer.PLACEMENT_OFFER)

    def test_the_form_value_is_checked_before_it_is_saved(self) -> None:
        self.assertIsNone(offer.parse_trial_placement(None))
        self.assertIsNone(offer.parse_trial_placement("  "))
        for value in offer.PLACEMENT_LABELS:
            self.assertEqual(offer.parse_trial_placement(f" {value} "), value)
        for bad in ("sideways", "OFFER", "offer,keyboard", True, 1, ["keyboard"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                offer.parse_trial_placement(bad)

    def test_each_choice_shows_the_button_in_its_own_place(self) -> None:
        expected = {"offer": (True, False), "keyboard": (False, True), "both": (True, True)}
        for placement, (in_offer, on_keyboard) in expected.items():
            ui = {offer.TRIAL_PLACEMENT_KEY: placement}
            with self.subTest(placement=placement):
                self.assertEqual(offer.shows_trial_in_offer(ui), in_offer)
                self.assertEqual(offer.shows_trial_on_keyboard(ui), on_keyboard)

    def test_the_bot_button_cycles_through_every_choice(self) -> None:
        self.assertEqual(offer.next_trial_placement("offer"), "keyboard")
        self.assertEqual(offer.next_trial_placement("keyboard"), "both")
        self.assertEqual(offer.next_trial_placement("both"), "offer")
        self.assertEqual(offer.next_trial_placement("junk"), "offer")

    def test_choosing_the_keyboard_puts_the_button_first_once(self) -> None:
        self.assertEqual(offer.menu_order_with_trial("shop,wallet", for_shop=False), "trial,shop,wallet")
        self.assertIsNone(offer.menu_order_with_trial("shop,trial,wallet", for_shop=False))
        self.assertEqual(
            offer.menu_order_with_trial("shop,reseller_apply,miniapp,wallet", for_shop=True), "trial,shop,wallet"
        )


class MainKeyboardTests(unittest.TestCase):
    UI = {"menu_order": "shop,trial,wallet", "menu_layout": "classic"}

    def test_the_button_sits_where_the_menu_order_puts_it(self) -> None:
        texts = _texts(rk.main_reply_keyboard("user", ui=self.UI, show_trial=True))
        self.assertEqual(texts[1], TRIAL_LABEL)
        self.assertLess(texts.index(DEFAULT_SETTINGS["btn_shop"]), texts.index(TRIAL_LABEL))

    def test_the_button_is_absent_unless_it_is_available_and_in_the_menu(self) -> None:
        self.assertNotIn(TRIAL_LABEL, _texts(rk.main_reply_keyboard("user", ui=self.UI)))
        removed = {**self.UI, "menu_order": "shop,wallet"}
        self.assertNotIn(TRIAL_LABEL, _texts(rk.main_reply_keyboard("user", ui=removed, show_trial=True)))

    def test_the_button_takes_the_trial_plan_colour_and_the_edited_label(self) -> None:
        ui = {**self.UI, "btn_style_shop_kind_trial": "success", "btn_trial": "🎁 تست مجانی"}
        buttons = [b for row in rk.main_reply_keyboard("user", ui=ui, show_trial=True).keyboard for b in row]
        trial = next(b for b in buttons if b.text == "🎁 تست مجانی")
        self.assertEqual(trial.style, "success")


class PlacementStoreTests(ImportCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add(BotUser(id=OTHER_SHOP, telegram_id=109, referral_code="shop9", role="reseller"))
            await session.commit()
            await set_settings_bulk(session, {"menu_order": "shop,services,wallet"}, reseller_id=SHOP)
            await set_settings_bulk(session, {"menu_order": "shop,wallet"}, reseller_id=OTHER_SHOP)

    async def test_the_platform_choice_changes_the_platform_menu_only(self) -> None:
        async with self.Session() as session:
            await offer.save_trial_placement(session, "keyboard", reseller_id=None)
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY), "keyboard")
            self.assertTrue((await get_setting(session, "menu_order")).startswith("trial,shop,"))
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "offer")
            self.assertEqual(await get_setting(session, "menu_order", reseller_id=SHOP), "shop,services,wallet")

    async def test_a_shop_choice_stays_in_that_shop(self) -> None:
        async with self.Session() as session:
            await offer.save_trial_placement(session, "both", reseller_id=SHOP)
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "both")
            self.assertEqual(await get_setting(session, "menu_order", reseller_id=SHOP), "trial,shop,services,wallet")
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=OTHER_SHOP), "offer")
            self.assertEqual(await get_setting(session, "menu_order", reseller_id=OTHER_SHOP), "shop,wallet")
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY), "")

    async def test_a_shop_without_its_own_menu_never_gets_the_platform_only_items(self) -> None:
        async with self.Session() as session:
            session.add(BotUser(id=10, telegram_id=110, referral_code="shop10", role="reseller"))
            await session.commit()
            await offer.save_trial_placement(session, "keyboard", reseller_id=10)
            order = (await get_setting(session, "menu_order", reseller_id=10)).split(",")
        self.assertEqual(order[0], "trial")
        self.assertNotIn("reseller_apply", order)

    async def test_saving_again_or_choosing_the_offer_keeps_one_button_and_the_admins_order(self) -> None:
        async with self.Session() as session:
            await offer.save_trial_placement(session, "keyboard", reseller_id=SHOP)
            await set_settings_bulk(session, {"menu_order": "shop,trial,wallet"}, reseller_id=SHOP)
            await offer.save_trial_placement(session, "both", reseller_id=SHOP)
            await offer.save_trial_placement(session, "offer", reseller_id=SHOP)
            self.assertEqual(await get_setting(session, "menu_order", reseller_id=SHOP), "shop,trial,wallet")
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "offer")

    async def test_an_unknown_choice_is_refused_and_nothing_is_saved(self) -> None:
        async with self.Session() as session:
            with self.assertRaises(ValueError):
                await offer.save_trial_placement(session, "sideways", reseller_id=SHOP)
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "offer")
            self.assertEqual(await get_setting(session, "menu_order", reseller_id=SHOP), "shop,services,wallet")


class TrialCase(ImportCase):
    KEYBOARD_UI = {"trial_enabled": "1", "trial_button_placement": "keyboard", "menu_order": "trial,shop,wallet"}

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        async with self.Session() as session:
            session.add_all([
                Plan(id=900, name="تست", is_trial=True, is_active=True, price=0, duration_days=1),
                Plan(id=901, name="تست فروشگاه", is_trial=True, is_active=True, price=0, duration_days=1,
                     owner_reseller_id=SHOP),
            ])
            await session.commit()

    async def _claim(self, user_id: int = 1, shop_key: str = "platform") -> None:
        async with self.Session() as session:
            session.add(TrialClaim(user_id=user_id, shop_key=shop_key))
            await session.commit()


class CustomerTrialPlansTests(TrialCase):
    async def test_a_new_customer_gets_this_shops_trial_only(self) -> None:
        async with self.Session() as session:
            self.assertEqual([p.id for p in await offer.customer_trial_plans(session, self.KEYBOARD_UI, 1)], [900])
            token = set_shop_reseller_id(SHOP)
            try:
                plans = await offer.customer_trial_plans(session, self.KEYBOARD_UI, 6)
            finally:
                reset_shop_reseller_id(token)
        self.assertEqual([p.id for p in plans], [901])

    async def test_no_trial_once_trials_are_off_taken_or_a_service_is_owned(self) -> None:
        async with self.Session() as session:
            self.assertEqual(await offer.customer_trial_plans(session, {"trial_enabled": "0"}, 1), [])
        await self._claim()
        async with self.Session() as session:
            self.assertEqual(await offer.customer_trial_plans(session, self.KEYBOARD_UI, 1), [])
            session.add(UserService(id=801, bot_user_id=2, pg_user_id=801, pg_username="mine", remark="order:1"))
            await session.commit()
            self.assertEqual(await offer.customer_trial_plans(session, self.KEYBOARD_UI, 2), [])


class KeyboardVisibilityTests(TrialCase):
    async def test_the_home_offer_placement_costs_no_query_and_shows_no_button(self) -> None:
        async with self.Session() as session:
            with patch.object(menu_nav, "customer_trial_plans", AsyncMock()) as plans:
                for ui in ({**self.KEYBOARD_UI, "trial_button_placement": "offer"},
                           {**self.KEYBOARD_UI, "menu_order": "shop,wallet"}):
                    self.assertFalse(await menu_nav.keyboard_shows_trial(session, ui, 1))
        plans.assert_not_awaited()

    async def test_a_new_customer_sees_it_until_the_trial_is_taken(self) -> None:
        async with self.Session() as session:
            self.assertTrue(await menu_nav.keyboard_shows_trial(session, self.KEYBOARD_UI, 1))
        await self._claim()
        async with self.Session() as session:
            self.assertFalse(await menu_nav.keyboard_shows_trial(session, self.KEYBOARD_UI, 1))

    async def test_the_customer_menu_has_it_and_the_admin_menu_does_not(self) -> None:
        async with self.Session() as session:
            await set_settings_bulk(session, self.KEYBOARD_UI)
            session.add(BotUser(id=3, telegram_id=103, referral_code="adm", role="admin"))
            await session.commit()
            for user_id, role, expected in ((1, "user", True), (3, "admin", False)):
                user = await session.get(BotUser, user_id)
                with patch("app.services.reseller_access.effective_menu_role", AsyncMock(return_value=role)):
                    markup, _, _ = await menu_nav.build_main_reply_keyboard(
                        session, user, is_reseller_bot=False, reseller_owner_id=None
                    )
                with self.subTest(role=role):
                    self.assertEqual(TRIAL_LABEL in _texts(markup), expected)


class HomeOfferPlacementTests(TrialCase):
    async def _offer(self, placement: str) -> AsyncMock:
        message = AsyncMock()
        message.chat = SimpleNamespace(id=1, type="private")
        message.answer = AsyncMock(return_value=SimpleNamespace(message_id=1, chat=message.chat, bot=message.bot))
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            ui = {**(await get_all_settings(session)), **self.KEYBOARD_UI,
                  "service_import_enabled": "0", "trial_button_placement": placement}
            with (
                patch.object(start, "replace_home", AsyncMock()),
                patch.object(start, "send_import_prompt", AsyncMock()) as sent,
            ):
                await start.render_home(message, session, user, ui=ui)
        return sent

    async def test_the_keyboard_only_placement_sends_no_trial_offer(self) -> None:
        (await self._offer("keyboard")).assert_not_awaited()

    async def test_the_offer_and_both_placements_send_the_trial_offer(self) -> None:
        for placement in ("offer", "both"):
            sent = await self._offer(placement)
            with self.subTest(placement=placement):
                sent.assert_awaited_once()
                self.assertTrue(sent.await_args.kwargs["trial_available"])


class KeyboardTapTests(TrialCase):
    def _tap(self, text: str = TRIAL_LABEL) -> tuple[AsyncMock, AsyncMock]:
        screen = AsyncMock()
        screen.chat = SimpleNamespace(id=101, type="private")
        screen.message_id = 77
        message = AsyncMock()
        message.text = text
        message.chat = SimpleNamespace(id=101, type="private")
        message.from_user = SimpleNamespace(id=101)
        message.bot = MagicMock()
        message.answer = AsyncMock(return_value=screen)
        return message, screen

    async def _run(self, message: AsyncMock, *, ui: dict | None = None, replay=None):
        replay = replay or AsyncMock(return_value=True)
        async with self.Session() as session:
            await set_settings_bulk(session, ui or self.KEYBOARD_UI)
            user = await session.get(BotUser, 1)
            with (
                patch.object(trial_menu, "replay_screen", replay),
                patch.object(trial_menu, "heal_main_reply", AsyncMock()) as heal,
                patch.object(trial_menu, "safe_edit_text", AsyncMock()) as edit,
            ):
                await trial_menu.keyboard_get_trial(message, session, user, AsyncMock())
        return replay, heal, edit

    async def test_the_label_matches_now_and_after_it_is_renamed(self) -> None:
        self.assertIn(TRIAL_LABEL, trial_menu.trial_menu_labels({}))
        renamed = trial_menu.trial_menu_labels({"btn_trial": "تست مجانی"})
        self.assertTrue({"تست مجانی", TRIAL_LABEL} <= renamed)
        message, _ = self._tap()
        async with self.Session() as session:
            self.assertTrue(await trial_menu.TrialMenuLabel()(message, session))
            message.text = "خرید"
            self.assertFalse(await trial_menu.TrialMenuLabel()(message, session))

    async def test_a_tap_starts_the_shop_purchase_of_the_single_trial_plan(self) -> None:
        message, screen = self._tap()
        replay, heal, edit = await self._run(message)
        self.assertEqual(replay.await_args.kwargs["data"], "shop:buy:900")
        self.assertIs(replay.await_args.kwargs["message"], screen)
        heal.assert_not_awaited()
        edit.assert_not_awaited()
        # The purchase answered elsewhere (a contact step): the placeholder goes.
        screen.delete.assert_awaited_once()

    async def test_a_refusal_of_the_purchase_is_shown_escaped_in_its_message(self) -> None:
        async def refuse(*_args, **_kwargs) -> bool:
            for log in screen_replay._replay_logs.get():
                log.answers.append(screen_replay.ReplayAnswer(text="<b>نه</b>", show_alert=True))
            return True

        message, screen = self._tap()
        _, _, edit = await self._run(message, replay=AsyncMock(side_effect=refuse))
        self.assertEqual(edit.await_args.args, (screen, "&lt;b&gt;نه&lt;/b&gt;"))
        screen.delete.assert_not_awaited()

    async def test_a_purchase_that_edited_its_message_is_left_alone(self) -> None:
        async def deliver(*_args, **_kwargs) -> bool:
            for log in screen_replay._replay_logs.get():
                log.edited.add((101, 77))
            return True

        message, screen = self._tap()
        _, _, edit = await self._run(message, replay=AsyncMock(side_effect=deliver))
        edit.assert_not_awaited()
        screen.delete.assert_not_awaited()

    async def test_an_unhandled_purchase_says_it_did_not_start(self) -> None:
        message, screen = self._tap()
        _, _, edit = await self._run(message, replay=AsyncMock(return_value=False))
        self.assertEqual(edit.await_args.args, (screen, trial_menu.TRIAL_NOT_STARTED))

    async def test_a_stale_button_is_refused_and_the_current_menu_is_sent(self) -> None:
        await self._claim()
        for ui in (self.KEYBOARD_UI, {**self.KEYBOARD_UI, "trial_button_placement": "offer"}):
            message, _ = self._tap()
            replay, heal, _ = await self._run(message, ui=ui)
            with self.subTest(ui=ui):
                replay.assert_not_awaited()
                self.assertEqual(heal.await_args.kwargs["text"], home_trial.TRIAL_UNAVAILABLE)

    async def test_with_several_trial_plans_the_customer_picks_one(self) -> None:
        async with self.Session() as session:
            session.add(Plan(id=902, name="تست دوم", is_trial=True, is_active=True, price=0, duration_days=2))
            await session.commit()
        message, _ = self._tap()
        replay, _, _ = await self._run(message)
        replay.assert_not_awaited()
        markup = message.answer.await_args.kwargs["reply_markup"]
        data = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertTrue({"shop:plan:900", "shop:plan:902"} <= set(data))


class BotPlacementButtonTests(ImportCase):
    async def _tap(self, *, owner: bool) -> tuple[AsyncMock, AsyncMock]:
        callback = SimpleNamespace(data=admin_plans.TRIAL_PLACE_CALLBACK, message=AsyncMock(), answer=AsyncMock())
        async with self.Session() as session:
            user = await session.get(BotUser, 1)
            with (
                patch("app.bot.auth.require_bot_owner", AsyncMock(return_value=owner)),
                patch.object(admin_plans, "_is_admin", return_value=True),
                patch.object(admin_plans, "_rerender_plans_screen", AsyncMock()) as rerender,
            ):
                await admin_plans.trial_cycle_placement(callback, session=session, db_user=user, state=AsyncMock())
        return callback, rerender

    async def test_each_tap_moves_the_platform_button_to_the_next_place(self) -> None:
        for expected in ("keyboard", "both", "offer"):
            _, rerender = await self._tap(owner=True)
            async with self.Session() as session:
                self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY), expected)
            rerender.assert_awaited_once()

    async def test_only_the_bot_owner_can_change_it(self) -> None:
        _, rerender = await self._tap(owner=False)
        rerender.assert_not_awaited()
        async with self.Session() as session:
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY), "")


class TrialSettingsRouteTests(ImportCase):
    """The web form saves the placement of the signed-in staff's own shop only."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from app.api.app import create_api_app

        self.staff: dict = {}
        self._data_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._data_dir.cleanup)
        with patch("app.api.app.DATA_DIR", Path(self._data_dir.name)):
            app = create_api_app()
        route = next(r for r in app.routes if getattr(r, "path", None) == "/plans/trial")

        async def get_db() -> AsyncIterator[AsyncSession]:
            async with self.Session() as session:
                yield session

        pending = list(route.dependant.dependencies)
        while pending:
            dependency = pending.pop()
            if dependency.call.__name__ == "_dep":
                app.dependency_overrides[dependency.call] = lambda: self.staff
            elif dependency.call.__name__ == "get_db":
                app.dependency_overrides[dependency.call] = get_db
            pending.extend(dependency.dependencies)
        self.app = app

    async def _post(self, staff: dict, data: dict) -> httpx.Response:
        self.staff.clear()
        self.staff.update(staff)
        form = {"csrf_token": "synthetic-csrf", "name": "تست", "duration_days": "1", "mode": "custom", **data}
        with (
            patch("app.api.app.is_setup_complete", return_value=True),
            patch("app.services.pg_quota.assert_user_plan_within_limits", AsyncMock()),
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test",
                cookies={"session": "synthetic-session", "csrf": "synthetic-csrf"},
            ) as client:
                return await client.post("/plans/trial", data=form, headers={"Origin": "http://test"})

    async def test_a_shop_saves_its_own_placement_and_no_other(self) -> None:
        shop_staff = {"role": "reseller", "permissions": ["plans"], "bot_user_id": SHOP}
        response = await self._post(shop_staff, {"trial_button_placement": "keyboard"})
        self.assertEqual(response.status_code, 303, response.text)
        self.assertNotIn("err=", response.headers["location"])
        async with self.Session() as session:
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "keyboard")
            self.assertTrue((await get_setting(session, "menu_order", reseller_id=SHOP)).startswith("trial,"))
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY), "")

    async def test_an_unknown_placement_is_refused_before_anything_is_saved(self) -> None:
        shop_staff = {"role": "reseller", "permissions": ["plans"], "bot_user_id": SHOP}
        response = await self._post(shop_staff, {"trial_button_placement": "sideways", "trial_enabled": "1"})
        self.assertEqual(response.status_code, 303)
        self.assertIn("err=", response.headers["location"])
        async with self.Session() as session:
            self.assertEqual(await get_setting(session, "trial_enabled", reseller_id=SHOP), "0")
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "offer")

    async def test_an_older_form_without_the_field_keeps_the_saved_choice(self) -> None:
        async with self.Session() as session:
            await offer.save_trial_placement(session, "both", reseller_id=SHOP)
        shop_staff = {"role": "reseller", "permissions": ["plans"], "bot_user_id": SHOP}
        response = await self._post(shop_staff, {})
        self.assertNotIn("err=", response.headers["location"])
        async with self.Session() as session:
            self.assertEqual(await get_setting(session, offer.TRIAL_PLACEMENT_KEY, reseller_id=SHOP), "both")


if __name__ == "__main__":
    unittest.main()
