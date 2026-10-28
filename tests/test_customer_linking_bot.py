"""Telegram adapters use the authenticated sender and keep opening separate from consent."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.bot.handlers.customer_linking import confirm_challenge, open_challenge
from app.services.customer_linking import read_link_challenge, start_link_challenge
from test_customer_linking import linking_database


def test_open_does_not_consent_and_another_callback_sender_cannot_confirm():
    async def scenario():
        async with linking_database() as (sessions, integration, key, customer), sessions() as session:
            challenge = await start_link_challenge(session, key.bearer, customer.id, idempotency_key="create-challenge-1")
            message = SimpleNamespace(from_user=SimpleNamespace(id=10011), chat=SimpleNamespace(type="private"), answer=AsyncMock())
            with patch("app.bot.handlers.customer_linking.current_shop_reseller_id", return_value=11):
                await open_challenge(message, SimpleNamespace(args="il_"+challenge.start_token), session)
                assert (await read_link_challenge(session, key.bearer, customer.id, challenge.challenge_id)).state == "awaiting_telegram"
                callback = SimpleNamespace(data="il:c:"+challenge.challenge_id[4:], from_user=SimpleNamespace(id=10012),
                                           answer=AsyncMock(), message=None)
                await confirm_challenge(callback, session)
                callback.answer.assert_awaited_once()
                assert callback.answer.call_args.kwargs["show_alert"] is True
                assert (await read_link_challenge(session, key.bearer, customer.id, challenge.challenge_id)).state == "awaiting_telegram"
                callback.from_user.id = 10011
                with patch("app.bot.handlers.customer_linking.safe_edit_text", AsyncMock()):
                    await confirm_challenge(callback, session)
            assert (await read_link_challenge(session, key.bearer, customer.id, challenge.challenge_id)).state == "awaiting_website"
            assert challenge.start_token not in str(message.answer.call_args)
    asyncio.run(scenario())
