"""Main bot delivery: the webhook watch repairs drift, the webhook follows a domain change, and a new token
is checked before it is written."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramNetworkError

from app.db.models import ResellerProfile
from app.jobs.webhook_watch import run_webhook_watch
from app.services import webhook_watch as watch
from app.services.secret_box import hash_bot_token
from app.services.setup_wizard import resolve_bot_update_mode
from app.services.ssl_certs import _public_address_keys
from app.services.webhook_address import PORT_NOT_ALLOWED, telegram_port_error, webhook_after_domain_change
from tests.postgres_support import PostgresTestCase, add_shop_users

TOKEN = "123456789:" + "A" * 35
EXPECTED = "https://bot.example.com/telegram/webhook"


def _settings(url: str = "https://bot.example.com", path: str = "/telegram/webhook", admins=(7, 8)):
    return SimpleNamespace(webhook_url=url, webhook_path=path, webhook_secret_token="secret", admin_ids=list(admins))


def _bot(url: str = "", *, error_age: timedelta | None = None, pending: int = 0, fail: bool = False) -> AsyncMock:
    bot = AsyncMock()
    info = SimpleNamespace(
        url=url,
        pending_update_count=pending,
        last_error_date=(datetime.now(timezone.utc) - error_age) if error_age is not None else None,
    )
    bot.get_webhook_info = AsyncMock(side_effect=TelegramNetworkError(method=None, message="x") if fail else None,
                                     return_value=info)
    return bot


class WatchCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.addCleanup(setattr, watch, "_served", watch._ServedMode())
        self.addCleanup(watch._last_alert.clear)
        patcher = patch.object(watch, "get_settings", return_value=_settings())
        self.settings = patcher.start()
        self.addCleanup(patcher.stop)


class CheckTests(WatchCase):
    async def test_nothing_is_checked_before_startup_says_what_it_serves(self) -> None:
        bot = _bot("https://elsewhere/x")
        self.assertIsNone(await watch.check_webhook(bot))
        bot.get_webhook_info.assert_not_awaited()

    async def test_a_drifted_webhook_is_set_back_keeping_pending_updates(self) -> None:
        watch.mark_served_webhook("/telegram/webhook")
        bot = _bot("https://old-domain.example.com/telegram/webhook", pending=12)
        health = await watch.check_webhook(bot)
        self.assertEqual((health.result, health.pending_updates), (watch.REPAIRED, 12))
        bot.set_webhook.assert_awaited_once()
        args, kwargs = bot.set_webhook.await_args
        self.assertEqual(args[0], EXPECTED)
        self.assertEqual((kwargs["secret_token"], kwargs["drop_pending_updates"]), ("secret", False))
        self.assertIs(watch.last_webhook_health(), health)

    async def test_a_matching_webhook_is_left_alone(self) -> None:
        watch.mark_served_webhook("/telegram/webhook")
        bot = _bot(EXPECTED, error_age=timedelta(hours=2))
        self.assertEqual((await watch.check_webhook(bot)).result, watch.OK)
        bot.set_webhook.assert_not_awaited()

    async def test_a_recent_delivery_error_is_reported_not_repaired(self) -> None:
        watch.mark_served_webhook("/telegram/webhook")
        bot = _bot(EXPECTED, error_age=timedelta(minutes=2))
        self.assertEqual((await watch.check_webhook(bot)).result, watch.DELIVERY_ERROR)
        bot.set_webhook.assert_not_awaited()

    async def test_a_webhook_on_a_polling_bot_is_removed(self) -> None:
        watch.mark_served_polling()
        bot = _bot("https://someone-else.example.com/hook")
        self.assertEqual((await watch.check_webhook(bot)).result, watch.REMOVED)
        bot.delete_webhook.assert_awaited_once()
        self.assertFalse(bot.delete_webhook.await_args.kwargs["drop_pending_updates"])

    async def test_a_changed_mode_or_path_waits_for_a_restart(self) -> None:
        watch.mark_served_webhook("/old/path")
        bot = _bot("https://bot.example.com/old/path")
        self.assertEqual((await watch.check_webhook(bot)).result, watch.RESTART_NEEDED)
        self.settings.return_value = _settings(url="")
        watch.mark_served_webhook("/telegram/webhook")
        self.assertEqual((await watch.check_webhook(bot)).result, watch.RESTART_NEEDED)
        bot.set_webhook.assert_not_awaited()
        bot.delete_webhook.assert_not_awaited()

    async def test_telegram_down_is_recorded_without_changes(self) -> None:
        watch.mark_served_webhook("/telegram/webhook")
        bot = _bot(fail=True)
        health = await watch.check_webhook(bot)
        self.assertEqual(health.result, watch.UNREACHABLE)
        self.assertFalse(health.is_ok)
        bot.set_webhook.assert_not_awaited()

    def test_alerts_repeat_at_most_once_an_hour_per_kind(self) -> None:
        health = watch.WebhookHealth(watch.REPAIRED, "webhook", EXPECTED, "", 0, datetime.now(timezone.utc))
        self.assertTrue(watch.should_alert(health, now=100.0))
        self.assertFalse(watch.should_alert(health, now=200.0))
        self.assertTrue(watch.should_alert(health, now=100.0 + watch.ALERT_REPEAT_SECONDS))
        ok = watch.WebhookHealth(watch.OK, "webhook", EXPECTED, EXPECTED, 0, datetime.now(timezone.utc))
        self.assertFalse(watch.should_alert(ok, now=0.0))


class JobTests(WatchCase):
    async def test_admins_hear_about_a_repair_once_and_a_blocked_admin_does_not_stop_others(self) -> None:
        from aiogram.exceptions import TelegramForbiddenError

        watch.mark_served_webhook("/telegram/webhook")
        bot = _bot("https://old.example.com/telegram/webhook")
        bot.send_message = AsyncMock(side_effect=[TelegramForbiddenError(method=None, message="blocked"), None])
        with patch("app.jobs.webhook_watch.get_settings", return_value=_settings()):
            await run_webhook_watch(bot)
            await run_webhook_watch(bot)
        self.assertEqual([c.args[0] for c in bot.send_message.await_args_list], [7, 8])


class AddressTests(unittest.TestCase):
    def test_telegram_only_delivers_to_four_ports(self) -> None:
        for url in ("https://a.example.com", "https://a.example.com:8443", "https://a.example.com:88"):
            self.assertIsNone(telegram_port_error(url), url)
        for url in ("https://a.example.com:9000", "https://a.example.com:99999"):
            self.assertEqual(telegram_port_error(url), PORT_NOT_ALLOWED, url)

    def test_explicit_webhook_on_a_wrong_port_is_refused_but_a_derived_one_means_polling(self) -> None:
        with self.assertRaises(ValueError):
            resolve_bot_update_mode(mode="webhook", webhook_url="https://a.example.com:9000")
        mode, base, _path = resolve_bot_update_mode(mode=None, webhook_url="", public_base_url="https://a.example.com:9000")
        self.assertEqual((mode, base), ("polling", ""))

    def test_the_webhook_follows_the_panel_only_when_it_rode_the_old_address(self) -> None:
        follow = webhook_after_domain_change
        self.assertEqual(follow(old_public="https://old.example.com", old_webhook="https://old.example.com",
                               new_public="https://new.example.com"), "https://new.example.com")
        self.assertIsNone(follow(old_public="https://old.example.com", old_webhook="https://proxy.example.net",
                                 new_public="https://new.example.com"))
        self.assertIsNone(follow(old_public="https://old.example.com", old_webhook="",
                                 new_public="https://new.example.com"))
        self.assertIsNone(follow(old_public="https://old.example.com", old_webhook="https://old.example.com",
                                 new_public="https://new.example.com:9000"))
        self.assertIsNone(follow(old_public="https://old.example.com", old_webhook="https://old.example.com",
                                 new_public="https://old.example.com"))

    def test_enabling_https_moves_a_riding_webhook_with_the_panel(self) -> None:
        keys = _public_address_keys({"PUBLIC_BASE_URL": "https://old.example.com", "WEBHOOK_URL": "https://old.example.com"},
                                    "https://new.example.com")
        self.assertEqual(keys, {"PUBLIC_BASE_URL": "https://new.example.com", "WEBHOOK_URL": "https://new.example.com"})
        keys = _public_address_keys({"PUBLIC_BASE_URL": "https://old.example.com", "WEBHOOK_URL": "https://hook.example.net"},
                                    "https://new.example.com")
        self.assertEqual(keys, {"PUBLIC_BASE_URL": "https://new.example.com"})


class SettingsCardTests(unittest.TestCase):
    def render(self, **ctx) -> str:
        from pathlib import Path

        from jinja2 import ChoiceLoader, DictLoader, Environment, FileSystemLoader, select_autoescape

        env = Environment(
            loader=ChoiceLoader([
                DictLoader({"base.html": "{% block content %}{% endblock %}"}),
                FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app/web/templates")),
            ]),
            autoescape=select_autoescape(),
        )
        return env.get_template("_settings_bot.html").render(
            request=SimpleNamespace(query_params={}), env_values={}, bot_status=None, bot_update_mode="webhook",
            csrf_token="t", webhook_check_minutes="۵", **ctx,
        )

    def test_the_card_shows_the_last_check_and_a_way_to_run_it_now(self) -> None:
        health = watch.WebhookHealth(watch.DELIVERY_ERROR, "webhook", EXPECTED, EXPECTED, 3, datetime.now(timezone.utc))
        html = self.render(webhook_health=health)
        self.assertIn(watch.RESULT_TEXT[watch.DELIVERY_ERROR], html)
        self.assertIn("۳ پیام در صف تلگرام", html)
        self.assertIn('action="/settings/bot/webhook-check"', html)
        self.assertNotIn("رسیدن پیام‌ها", self.render(webhook_health=None))


class TokenTests(PostgresTestCase):
    async def test_a_malformed_token_is_refused_without_calling_telegram(self) -> None:
        with patch.object(watch, "check_bot_connection") as probe:
            async with self.Session() as session:
                for bad in ("", "abc", "123:short", TOKEN + " x"):
                    with self.assertRaisesRegex(ValueError, "قالب توکن"):
                        await watch.verify_main_bot_token(session, bad)
        probe.assert_not_called()

    async def test_a_shop_bot_token_cannot_become_the_main_bot(self) -> None:
        await add_shop_users(self.Session, 41)
        async with self.Session() as session:
            session.add(ResellerProfile(user_id=41, bot_token_hash=hash_bot_token(TOKEN)))
            await session.commit()
            with self.assertRaisesRegex(ValueError, "فروشگاه"):
                await watch.verify_main_bot_token(session, TOKEN)

    async def test_telegram_decides_and_the_username_comes_from_it(self) -> None:
        async with self.Session() as session:
            with patch.object(watch, "check_bot_connection", AsyncMock(return_value={"ok": False, "error": "x"})):
                with self.assertRaisesRegex(ValueError, "نپذیرفت"):
                    await watch.verify_main_bot_token(session, TOKEN)
            with patch.object(watch, "check_bot_connection", AsyncMock(return_value={"ok": True, "username": "RealBot"})):
                self.assertEqual(await watch.verify_main_bot_token(session, TOKEN), "RealBot")


if __name__ == "__main__":
    unittest.main()
