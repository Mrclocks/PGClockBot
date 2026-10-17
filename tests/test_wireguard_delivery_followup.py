"""WireGuard files: detected from the panel, retried once, never dropped silently, re-sent on demand."""

from __future__ import annotations

import io
import unittest
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch

from app.bot.nav_inline import service_card_keyboard
from app.services import wireguard_send
from app.services.pasarguard import WireGuardResponse
from app.services.wireguard_delivery import (
    MAX_WIREGUARD_FILES,
    REASON_DISABLED,
    REASON_NONE,
    REASON_OK,
    REASON_UNAVAILABLE,
    WireGuardFetch,
    WireGuardFile,
    fetch_wireguard,
    has_wireguard,
    parse_wireguard_archive,
)

CONF = "[Interface]\nPrivateKey = abc\nAddress = 10.0.0.2/32\n\n[Peer]\nPublicKey = xyz\nEndpoint = h:51820\n"
SUB_URL = "https://pg.example/sub/tok123456"
PLATFORM_TOKEN = "111:platform"


def _zip(count: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for index in range(count):
            zf.writestr(f"host-{index:02d}.conf", CONF)
    return buf.getvalue()


def _fetched(count: int, *, total: int | None = None) -> WireGuardFetch:
    files = [WireGuardFile(filename=f"h{i}.conf", content=CONF.encode()) for i in range(count)]
    return WireGuardFetch(files=files, total=count if total is None else total, reason=REASON_OK if count else REASON_NONE)


def _panel(response: WireGuardResponse) -> MagicMock:
    client = MagicMock()
    client.subscription_wireguard_bytes = AsyncMock(return_value=response)
    return client


class DetectionAndParsingTests(unittest.TestCase):
    def test_a_service_with_a_peer_address_has_wireguard(self) -> None:
        self.assertTrue(has_wireguard({"proxy_settings": {"wireguard": {"peer_ips": ["10.0.0.2/32"]}}}))

    def test_a_service_without_a_peer_address_has_none(self) -> None:
        for info in (None, {}, {"proxy_settings": {"wireguard": {"peer_ips": []}}}, {"proxy_settings": "x"}):
            with self.subTest(info=info):
                self.assertFalse(has_wireguard(info))

    def test_an_archive_beyond_the_album_limit_reports_every_config(self) -> None:
        files, total = parse_wireguard_archive(_zip(MAX_WIREGUARD_FILES + 2))
        self.assertEqual(len(files), MAX_WIREGUARD_FILES)
        self.assertEqual(total, MAX_WIREGUARD_FILES + 2)


class FetchReasonTests(unittest.IsolatedAsyncioTestCase):
    async def _reason(self, response: WireGuardResponse) -> WireGuardFetch:
        with patch("app.services.wireguard_delivery.get_pg", return_value=_panel(response)):
            return await fetch_wireguard(SUB_URL, username="u1")

    async def test_the_reason_says_why_no_file_came_back(self) -> None:
        cases = {
            REASON_DISABLED: WireGuardResponse(status=406, data=None),
            REASON_UNAVAILABLE: WireGuardResponse(status=None, data=None),
            REASON_NONE: WireGuardResponse(status=404, data=None),
        }
        for reason, response in cases.items():
            with self.subTest(reason=reason):
                fetched = await self._reason(response)
                self.assertEqual((fetched.files, fetched.reason), ([], reason))

    async def test_an_empty_archive_means_no_config(self) -> None:
        fetched = await self._reason(WireGuardResponse(status=200, data=_zip(0) + b"padding-bytes"))
        self.assertEqual(fetched.reason, REASON_NONE)

    async def test_configs_come_back_with_their_count(self) -> None:
        fetched = await self._reason(WireGuardResponse(status=200, data=_zip(2)))
        self.assertEqual((len(fetched.files), fetched.total, fetched.reason), (2, 2, REASON_OK))


class DeliverWireGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        wireguard_send._last_admin_alert.clear()
        self.addCleanup(wireguard_send._last_admin_alert.clear)
        self.bot = AsyncMock()
        self.bot.token = PLATFORM_TOKEN
        settings = MagicMock(bot_token=PLATFORM_TOKEN)
        for target, value in (
            ("app.services.wireguard_send.get_settings", MagicMock(return_value=settings)),
            ("app.services.wireguard_send.asyncio.sleep", AsyncMock()),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.alert = AsyncMock(return_value=1)
        patcher = patch("app.services.wireguard_send.notify_wireguard_missing", self.alert)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def _deliver(self, *results: WireGuardFetch, expected: bool) -> int:
        with patch("app.services.wireguard_send.fetch_wireguard", AsyncMock(side_effect=list(results))):
            return await wireguard_send.deliver_wireguard(
                self.bot, 100, MagicMock(), sub_url=SUB_URL, username="u1", expected=expected, ui={}
            )

    def _texts(self) -> list[str]:
        return [call.args[1] for call in self.bot.send_message.await_args_list]

    async def test_a_late_config_is_picked_up_by_the_retry_and_sent_as_one_album(self) -> None:
        sent = await self._deliver(_fetched(0), _fetched(3), expected=True)
        self.assertEqual(sent, 3)
        self.bot.send_media_group.assert_awaited_once()
        self.assertEqual(len(self.bot.send_media_group.await_args.kwargs["media"]), 3)
        self.assertEqual(self._texts(), [])
        self.alert.assert_not_awaited()

    async def test_a_missing_config_tells_the_customer_and_warns_the_admin_once(self) -> None:
        missing = WireGuardFetch(files=[], total=0, reason=REASON_DISABLED)
        await self._deliver(missing, missing, expected=True)
        await self._deliver(missing, missing, expected=True)
        texts = self._texts()
        self.assertEqual(len(texts), 2)
        self.assertIn("🔐 فایل WireGuard", texts[0])
        self.alert.assert_awaited_once()
        self.assertIn("خاموش", self.alert.await_args.args[2])

    async def test_a_shop_bot_does_not_try_to_reach_the_platform_admin(self) -> None:
        self.bot.token = "222:shop"
        await self._deliver(_fetched(0), _fetched(0), expected=True)
        self.assertEqual(len(self._texts()), 1)
        self.alert.assert_not_awaited()

    async def test_a_service_without_wireguard_gets_no_notice(self) -> None:
        sent = await self._deliver(_fetched(0), expected=False)
        self.assertEqual(sent, 0)
        self.bot.send_message.assert_not_awaited()
        self.bot.send_document.assert_not_awaited()

    async def test_configs_beyond_the_album_limit_are_announced(self) -> None:
        await self._deliver(_fetched(MAX_WIREGUARD_FILES, total=MAX_WIREGUARD_FILES + 2), expected=True)
        self.assertEqual(len(self._texts()), 1)
        self.assertIn(str(MAX_WIREGUARD_FILES + 2), self._texts()[0])

    async def test_a_hostile_username_is_escaped_in_the_admin_alert(self) -> None:
        empty = WireGuardFetch(files=[], total=0, reason=REASON_NONE)
        with patch("app.services.wireguard_send.fetch_wireguard", AsyncMock(side_effect=[empty, empty])):
            await wireguard_send.deliver_wireguard(
                self.bot, 100, MagicMock(), sub_url=SUB_URL, username="<b>x</b>", expected=True, ui={}
            )
        detail = self.alert.await_args.args[2]
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", detail)
        self.assertNotIn("<b>x</b>", detail)


class ServicePageTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _callbacks(markup) -> list[str]:
        return [button.callback_data for row in markup.inline_keyboard for button in row]

    def test_the_button_shows_only_for_a_wireguard_service(self) -> None:
        self.assertIn("svc:wg:7", self._callbacks(service_card_keyboard(7, {}, wireguard=True)))
        self.assertNotIn("svc:wg:7", self._callbacks(service_card_keyboard(7, {})))

    async def _tap(self, *, may_use: bool, fetched: WireGuardFetch) -> tuple[AsyncMock, AsyncMock]:
        from app.bot.handlers import services

        callback = MagicMock(data="svc:wg:7", bot=AsyncMock())
        callback.answer = AsyncMock()
        session = MagicMock(get=AsyncMock(return_value=MagicMock(subscription_url=SUB_URL, pg_username="u1")))
        fetch = AsyncMock(return_value=fetched)
        with (
            patch.object(services, "customer_may_use_service", AsyncMock(return_value=may_use)),
            patch.object(services, "fetch_wireguard", fetch),
        ):
            await services.svc_wireguard(callback, session, MagicMock(id=1, telegram_id=100))
        return callback, fetch

    async def test_another_customer_cannot_pull_the_files(self) -> None:
        callback, fetch = await self._tap(may_use=False, fetched=_fetched(1))
        fetch.assert_not_awaited()
        callback.bot.send_document.assert_not_awaited()

    async def test_the_owner_gets_the_files_again(self) -> None:
        callback, _fetch = await self._tap(may_use=True, fetched=_fetched(1))
        callback.bot.send_document.assert_awaited_once()
        self.assertEqual(callback.bot.send_document.await_args.args[0], 100)

    async def test_a_disabled_panel_format_is_explained(self) -> None:
        callback, _fetch = await self._tap(
            may_use=True, fetched=WireGuardFetch(files=[], total=0, reason=REASON_DISABLED)
        )
        self.assertIn("خاموش", callback.answer.await_args.args[0])
        callback.bot.send_document.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
