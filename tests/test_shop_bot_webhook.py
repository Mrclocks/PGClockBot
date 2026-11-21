"""Optional webhook delivery for shop bots: per-shop secrets, polling fallback, a guarded shared route."""

from __future__ import annotations

import asyncio
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import shop_bot_webhook_pages
from app.api.shop_bot_webhook_pages import register_shop_bot_webhook
from app.config import Settings
from app.services import shop_bot_webhook
from app.services.bot_updates import BotUpdateScheduler
from app.services.reseller_bots import ResellerBotManager
from app.services.shop_bot_webhook import SECRET_HEADER, ShopWebhookConfig, shop_webhook_config

CONFIG = ShopWebhookConfig(base_url="https://panel.example", master_secret="master-secret")
UPDATE = {"update_id": 7, "message": {"message_id": 1, "date": 0, "chat": {"id": 500, "type": "private"},
                                       "from": {"id": 500, "is_bot": False, "first_name": "C"}, "text": "hi"}}


class FakeBot:
    def __init__(self, bot_id: int = 11, *, webhook_fails: bool = False) -> None:
        self.id = bot_id
        self.webhook_fails = webhook_fails
        self.set_calls: list[tuple[str, str]] = []
        self.deleted = 0
        self.session = SimpleNamespace(close=AsyncMock())

    async def set_webhook(self, url: str, *, secret_token: str, **_kwargs: object) -> bool:
        if self.webhook_fails:
            raise RuntimeError("Bad Request: bad webhook")
        self.set_calls.append((url, secret_token))
        return True

    async def delete_webhook(self, **_kwargs: object) -> bool:
        self.deleted += 1
        return True

    async def get_updates(self, **_kwargs: object) -> list:
        await asyncio.Event().wait()
        return []


class ConfigTests(unittest.TestCase):
    def test_each_shop_has_its_own_stable_secret_in_telegrams_alphabet(self) -> None:
        one, two = CONFIG.secret_for(1), CONFIG.secret_for(2)
        self.assertNotEqual(one, two)
        self.assertEqual(one, CONFIG.secret_for(1))
        self.assertRegex(one, r"^[A-Za-z0-9_-]{1,256}$")
        self.assertNotIn("master-secret", one)
        self.assertEqual(CONFIG.url_for(5), "https://panel.example/telegram/shop/5")

    def test_only_the_shops_own_secret_is_accepted(self) -> None:
        self.assertTrue(CONFIG.accepts(1, CONFIG.secret_for(1)))
        self.assertFalse(CONFIG.accepts(1, CONFIG.secret_for(2)))
        self.assertFalse(CONFIG.accepts(1, ""))
        self.assertFalse(CONFIG.accepts(1, None))
        self.assertFalse(ShopWebhookConfig("https://panel.example", "other").accepts(1, CONFIG.secret_for(1)))

    def _config_with(self, **env: str) -> ShopWebhookConfig | None:
        settings = Settings(_env_file=None, WEBHOOK_SECRET_TOKEN="from-env", **env)
        with patch.object(shop_bot_webhook, "get_settings", return_value=settings):
            return shop_webhook_config()

    def test_it_is_off_unless_the_operator_opts_in_with_a_public_https_url(self) -> None:
        self.assertIsNone(self._config_with(WEBHOOK_URL="https://panel.example"))
        self.assertIsNone(self._config_with(SHOP_BOT_WEBHOOK="1"))
        self.assertIsNone(self._config_with(SHOP_BOT_WEBHOOK="1", WEBHOOK_URL="http://panel.example"))
        config = self._config_with(SHOP_BOT_WEBHOOK="1", WEBHOOK_URL="https://panel.example/telegram/webhook")
        self.assertEqual((config.base_url, config.master_secret), ("https://panel.example", "from-env"))


class ManagerDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def manager(self, webhook: ShopWebhookConfig | None = CONFIG) -> ResellerBotManager:
        return ResellerBotManager(SimpleNamespace(), scheduler=BotUpdateScheduler(), webhook=webhook)

    async def test_a_registered_webhook_replaces_the_poll_loop(self) -> None:
        manager, bot = self.manager(), FakeBot()
        manager._bots[3] = bot
        await manager._begin_delivery(3, bot)
        self.assertEqual(bot.set_calls, [(CONFIG.url_for(3), CONFIG.secret_for(3))])
        self.assertNotIn(3, manager._tasks)
        self.assertIs(manager.webhook_bot(3), bot)

    async def test_a_refused_webhook_falls_back_to_polling(self) -> None:
        manager, bot = self.manager(), FakeBot(webhook_fails=True)
        manager._bots[3] = bot
        await manager._begin_delivery(3, bot)
        self.assertIn(3, manager._tasks)
        self.assertIsNone(manager.webhook_bot(3))
        await manager.stop_all()

    async def test_without_the_option_shop_bots_poll_as_before(self) -> None:
        manager, bot = self.manager(webhook=None), FakeBot()
        manager._bots[3] = bot
        await manager._begin_delivery(3, bot)
        self.assertEqual(bot.set_calls, [])
        self.assertIn(3, manager._tasks)
        await manager.stop_all()

    async def test_stopping_a_shop_removes_its_webhook_but_shutdown_keeps_it(self) -> None:
        manager, stopped, kept = self.manager(), FakeBot(1), FakeBot(2)
        for pid, bot in ((1, stopped), (2, kept)):
            manager._bots[pid] = bot
            await manager._begin_delivery(pid, bot)
        await manager.stop_reseller(1)
        await manager.stop_all()
        self.assertEqual((stopped.deleted, kept.deleted), (1, 0))
        self.assertIsNone(manager.webhook_bot(2))
        stopped.session.close.assert_awaited()
        kept.session.close.assert_awaited()


class RouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = FakeBot()
        self.accepted: list[tuple[object, object]] = []

        async def accept(bot: object, update: object) -> None:
            self.accepted.append((bot, update))

        self.manager = SimpleNamespace(webhook_bot=lambda pid: self.bot if pid == 3 else None,
                                       accept_webhook_update=accept)
        app = FastAPI()
        register_shop_bot_webhook(app, config=CONFIG)
        patcher = patch.object(shop_bot_webhook_pages, "get_reseller_bot_manager", return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = TestClient(app)

    def post(self, pid: int, secret: str | None, body: object = UPDATE) -> int:
        headers = {SECRET_HEADER: secret} if secret is not None else {}
        return self.client.post(f"/telegram/shop/{pid}", content=json.dumps(body), headers=headers).status_code

    def test_the_shops_update_is_queued_for_its_own_bot(self) -> None:
        self.assertEqual(self.post(3, CONFIG.secret_for(3)), 200)
        ((bot, update),) = self.accepted
        self.assertIs(bot, self.bot)
        self.assertEqual(update.update_id, 7)

    def test_a_missing_wrong_or_other_shops_secret_is_refused(self) -> None:
        for secret in (None, "", "guess", CONFIG.secret_for(4)):
            with self.subTest(secret=secret):
                self.assertEqual(self.post(3, secret), 403)
        self.assertEqual(self.accepted, [])

    def test_a_shop_not_running_here_asks_telegram_to_retry(self) -> None:
        self.assertEqual(self.post(9, CONFIG.secret_for(9)), 503)

    def test_out_of_range_ids_oversized_and_malformed_bodies_are_refused(self) -> None:
        self.assertEqual(self.post(0, CONFIG.secret_for(0)), 403)
        self.assertEqual(self.post(3, CONFIG.secret_for(3), body={"pad": "x" * 2_000_000}), 413)
        self.assertEqual(self.client.post("/telegram/shop/3", content=b"{", headers={SECRET_HEADER: CONFIG.secret_for(3)}).status_code, 400)
        self.assertEqual(self.post(3, CONFIG.secret_for(3), body={"update_id": "x"}), 400)
        self.assertEqual(self.accepted, [])

    def test_the_log_never_holds_a_secret(self) -> None:
        with self.assertLogs(shop_bot_webhook_pages.logger, level="WARNING") as logs:
            self.post(3, "guess-secret-value")
        self.assertFalse(any(re.search("guess-secret-value|" + CONFIG.secret_for(3), line) for line in logs.output))


if __name__ == "__main__":
    unittest.main()
