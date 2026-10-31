"""A private chat with the real bot: the dispatcher, its middlewares and handlers run unchanged.

Telegram is replaced by a fake session that keeps the chat as the person sees it: which messages
are still on screen, which one carries the menu keyboard, and what that keyboard shows. Tests
type text, tap reply-keyboard buttons and inline buttons, then assert on the visible chat.
"""

from __future__ import annotations

import itertools
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    DeleteMessages,
    EditMessageCaption,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    User,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

BOT_ID = 777000
TOKEN = "123456:TEST"
SHOP_TOKEN = "654321:SHOP"
_SEND_TYPES = (SendMessage, SendPhoto, SendDocument)


@dataclass
class Shown:
    """One message in the chat as the person sees it."""

    message_id: int
    from_bot: bool
    text: str
    inline: list[list[tuple[str, str]]] = field(default_factory=list)
    keyboard: list[list[str]] | None = None
    deleted: bool = False

    def buttons(self) -> list[str]:
        return [text for row in self.inline for text, _ in row]


class FakeTelegram(BaseSession):
    """Answers the bot's requests like Telegram and keeps the visible chat."""

    def __init__(self, chat_id: int) -> None:
        super().__init__()
        self.chat_id = chat_id
        self.messages: dict[int, Shown] = {}
        self.ids = itertools.count(1)
        self.keyboard: list[list[str]] | None = None
        self.alerts: list[str] = []
        self.requests: list[TelegramMethod[Any]] = []
        # Bot messages sent or edited since begin_turn(): what the latest update showed.
        self.last_turn: set[int] = set()

    def begin_turn(self) -> None:
        self.last_turn = set()

    def _message(self, shown: Shown, *, user: User, bot: Bot | None = None) -> Message:
        message = Message(
            message_id=shown.message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=self.chat_id, type="private"),
            from_user=user,
            text=shown.text or None,
            reply_markup=_inline_markup(shown.inline),
        )
        return message.as_(bot) if bot is not None else message

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:  # noqa: ASYNC109
        self.requests.append(method)
        bot_user = User(id=BOT_ID, is_bot=True, first_name="bot")
        if isinstance(method, _SEND_TYPES):
            text = getattr(method, "text", None) or getattr(method, "caption", None) or ""
            shown = Shown(next(self.ids), True, str(text), _inline_rows(method.reply_markup))
            if isinstance(method.reply_markup, ReplyKeyboardMarkup):
                shown.keyboard = [[b.text for b in row] for row in method.reply_markup.keyboard]
                self.keyboard = shown.keyboard
            elif isinstance(method.reply_markup, ReplyKeyboardRemove):
                self.keyboard = None
            self.messages[shown.message_id] = shown
            self.last_turn.add(shown.message_id)
            return self._message(shown, user=bot_user, bot=bot)
        if isinstance(method, (EditMessageText, EditMessageCaption)):
            shown = self._live(method.message_id)
            shown.text = str(getattr(method, "text", None) or getattr(method, "caption", None) or "")
            shown.inline = _inline_rows(method.reply_markup)
            self.last_turn.add(shown.message_id)
            return self._message(shown, user=bot_user, bot=bot)
        if isinstance(method, EditMessageReplyMarkup):
            shown = self._live(method.message_id)
            shown.inline = _inline_rows(method.reply_markup)
            return self._message(shown, user=bot_user, bot=bot)
        if isinstance(method, DeleteMessages):
            for message_id in method.message_ids:
                self._delete(message_id)
            return True
        if isinstance(method, DeleteMessage):
            self._delete(method.message_id)
            return True
        if isinstance(method, GetMe):
            return User(id=BOT_ID, is_bot=True, first_name="bot", username="pgclock_test_bot")
        if isinstance(method, AnswerCallbackQuery):
            if method.text:
                self.alerts.append(method.text)
            return True
        return True

    def _live(self, message_id: int) -> Shown:
        shown = self.messages.get(int(message_id))
        if shown is None or shown.deleted:
            from aiogram.exceptions import TelegramBadRequest

            raise TelegramBadRequest(method=None, message="Bad Request: message to edit not found")  # type: ignore[arg-type]
        return shown

    def _delete(self, message_id: int) -> None:
        shown = self.messages.get(int(message_id))
        if shown is not None:
            shown.deleted = True

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover - unused
        yield b""

    async def close(self) -> None:
        return None

    # What the person sees -------------------------------------------------------------
    def visible(self) -> list[Shown]:
        return [m for m in sorted(self.messages.values(), key=lambda m: m.message_id) if not m.deleted]

    def keyboard_carriers(self) -> list[Shown]:
        return [m for m in self.visible() if m.keyboard is not None]


def _inline_rows(markup: Any) -> list[list[tuple[str, str]]]:
    if not isinstance(markup, InlineKeyboardMarkup):
        return []
    return [[(b.text, b.callback_data or b.url or "") for b in row] for row in markup.inline_keyboard]


def _inline_markup(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup | None:
    from aiogram.types import InlineKeyboardButton

    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d or "x") for t, d in row] for row in rows]
    )


class ChatSim:
    """One person in one private chat with the platform bot."""

    def __init__(self, telegram_id: int, *, name: str = "Owner") -> None:
        self.telegram_id = telegram_id
        self.person = User(id=telegram_id, is_bot=False, first_name=name)
        self.telegram = FakeTelegram(telegram_id)
        self.update_ids = itertools.count(1)
        self._patches: list[Any] = []

    async def start_shop(self, *, staff: bool) -> None:
        """This chat on a shop's own bot: as its owner (``staff``) or as one of its customers."""
        from app.db.models import BotUser, ResellerProfile

        await self.start(owner=False)
        async with self.Session() as session:
            owner_tg = self.telegram_id if staff else self.telegram_id + 1
            owner = BotUser(id=900, telegram_id=owner_tg, referral_code="shop900", full_name="Shop", role="reseller")
            session.add(owner)
            await session.flush()
            profile = ResellerProfile(id=90, user_id=owner.id, is_active=True, bot_token=SHOP_TOKEN,
                                      bot_permissions="dashboard,orders,payments,tickets,stats,settings,plans")
            session.add(profile)
            await session.commit()
        from app.bot import create_bot

        self.bot = create_bot(SHOP_TOKEN, session=self.telegram)
        self.bot._pgclock_reseller = {"profile_id": 90, "user_id": 900}
        self.shop_owner_id = 900

    async def start(self, *, owner: bool = True) -> None:
        from app.bot import create_dispatcher
        from app.db.models import Base
        from app.services.users import clear_settings_cache

        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._tmp.name) / 'sim.db'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.Session = async_sessionmaker(self.engine, expire_on_commit=False)
        clear_settings_cache()
        from app.config import get_settings

        env = patch.dict(os.environ, {"BOT_TOKEN": TOKEN, "ADMIN_IDS": str(self.telegram_id) if owner else ""})
        env.start()
        self._patches.append(env)
        get_settings.cache_clear()
        for target, value in (
            ("app.bot.middlewares.SessionLocal", self.Session),
            ("app.services.chat_message_ledger.SessionLocal", self.Session),
            ("app.bot.auth.is_bot_owner_principal", AsyncMock(return_value=owner)),
        ):
            p = patch(target, value)
            p.start()
            self._patches.append(p)
        from app.services import chat_message_ledger

        chat_message_ledger.writer.sessions = self.Session
        from app.bot import create_bot

        self.bot = create_bot(TOKEN, session=self.telegram)
        _detach_routers()  # routers are module-level; another simulated chat may hold them
        self.dispatcher = create_dispatcher()

    async def stop(self) -> None:
        from app.config import get_settings
        from app.services.users import clear_settings_cache

        _detach_routers()
        for p in reversed(self._patches):
            p.stop()
        get_settings.cache_clear()
        clear_settings_cache()
        await self.engine.dispose()
        self._tmp.cleanup()

    async def _feed(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def type(self, text: str) -> None:
        """The person sends text (typed, or a reply-keyboard button tap)."""
        shown = Shown(next(self.telegram.ids), False, text)
        self.telegram.messages[shown.message_id] = shown
        message = self.telegram._message(shown, user=self.person, bot=self.bot)
        await self._feed(Update(update_id=next(self.update_ids), message=message))

    async def send_photo(self) -> None:
        """The person sends a photo (a payment receipt)."""
        from aiogram.types import PhotoSize

        shown = Shown(next(self.telegram.ids), False, "[photo]")
        self.telegram.messages[shown.message_id] = shown
        message = Message(
            message_id=shown.message_id,
            date=datetime.now(timezone.utc),
            chat=Chat(id=self.telegram.chat_id, type="private"),
            from_user=self.person,
            photo=[PhotoSize(file_id=f"photo{shown.message_id}", file_unique_id=f"u{shown.message_id}", width=10, height=10)],
        ).as_(self.bot)
        await self._feed(Update(update_id=next(self.update_ids), message=message))

    async def tap(self, label: str) -> None:
        """The person taps the inline button labelled ``label`` on the newest message showing it."""
        for shown in reversed(self.telegram.visible()):
            for row in shown.inline:
                for text, data in row:
                    if label in text:
                        await self._tap(shown, data)
                        return
        raise AssertionError(f"no visible inline button {label!r}; screen: {self.screen()}")

    async def _tap(self, shown: Shown, data: str) -> None:
        message = self.telegram._message(shown, user=User(id=BOT_ID, is_bot=True, first_name="bot"), bot=self.bot)
        callback = CallbackQuery(id=str(next(self.update_ids)), from_user=self.person, chat_instance="c",
                                 message=message, data=data)
        await self._feed(Update(update_id=next(self.update_ids), callback_query=callback))

    def screen(self) -> list[str]:
        """The visible chat, one short line per message, oldest first."""
        out = []
        for m in self.telegram.visible():
            who = "bot" if m.from_bot else "me"
            mark = " [⌨]" if m.keyboard is not None else ""
            out.append(f"{who}: {m.text[:40]!r}{mark}")
        return out


def _detach_routers() -> None:
    from app.bot import bot_routers

    for router in bot_routers():
        router._parent_router = None
