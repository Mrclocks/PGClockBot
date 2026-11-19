"""«⚡ مرکز فرمان»: type any word and open the menu, button or setting it names.

The admin panel and every shop panel have it. Results come from the viewer's own catalogue
(``command_catalog``), so a person only finds what their panel already lets them open, and each
result is the same callback as the real button: its handler checks access again on the tap.
"""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, field

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import is_bot_owner_principal
from app.bot.command_catalog import (
    ADMIN_HOME_CALLBACK,
    ADMIN_SETTINGS_CALLBACK,
    PATH_SEPARATOR,
    ShopViewer,
    admin_command_entries,
    shop_command_entries,
)
from app.bot.handlers import admin_settings, reseller_settings
from app.bot.handlers.reply_nav import open_admin_home, open_reseller_home
from app.bot.handlers.reseller_hub import load_reseller_hub
from app.bot.hub_entries import COMMAND_CENTER_CALLBACK, COMMAND_CENTER_SETTINGS_CALLBACK
from app.bot.list_pages import Page, page_line, page_number, page_of, pager_row
from app.bot.nav_input import (
    CancelEntry,
    ask_text,
    delete_user_message,
    finish_text_step,
    register_cancel_code,
    try_legacy_cancel,
)
from app.bot.reseller_hub_groups import RESELLER_HOME_CALLBACK
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services import bot_feedback_texts as feedback
from app.services.command_data_search import DataHit, DataKind, DataScope, is_subscription_link, search_data
from app.services.command_search import CommandEntry, CommandKind, filter_kind, query_words, search_commands
from app.services.db_safe import rollback_quiet
from app.services.numbers import to_fa_digits
from app.services.resellers import has_bot_perm
from app.services.settings_search import SEARCH_QUERY_MAX_LEN
from app.services.users import get_all_settings

logger = logging.getLogger(__name__)
router = Router(name="command_center")

CANCEL_CODE = "cmd"
RESULTS_PREFIX = "cmd:r:"
# The old «جستجو در تنظیمات» button on messages sent before the command center.
LEGACY_SETTINGS_SEARCH_CALLBACK = "adm:st:find"
PAGE_SIZE = 8
BUTTON_TEXT_MAX = 60
QUERY_KEY = "command_query"
KIND_KEY = "command_kind"
BACK_KEY = "command_back"
DATA_KEY = "command_data"
# Stored instead of a subscription link: the link is a secret and is never kept or shown again.
LINK_QUERY = "\x00link"
LINK_HEADING = "🔗 لینک اشتراک"
# Shop staff need this feature to open a customer's card, so only they find customers.
CUSTOMER_PERMISSION = "dashboard"
CHIPS_PER_ROW = 3
RECENT_KEY = "command_recent"
RECENT_LIMIT = 5
RECENT_PREFIX = "cmd:q:"
RECENT_TEXT_MAX = 24
TITLE = "⚡ مرکز فرمان"
PROMPT = (
    f"<b>{TITLE}</b>\n\n"
    "نام هر منو، دکمه یا تنظیمی را بفرستید تا همان بخش را باز کنید.\n"
    "مثلاً «کارت»، «تست»، «بکاپ» یا «رسید»."
)
TOO_SHORT = "حداقل دو حرف بفرستید."
NOTHING_FOUND = "چیزی پیدا نشد؛ کلمهٔ کوتاه‌تر یا نام دیگری را امتحان کنید، مثلاً «کارت» یا «تست»."
RESULTS_HINT = "روی هر مورد بزنید تا همان بخش باز شود."
SEARCH_AGAIN = "🔎 جستجوی دیگر"
BACK = "⬅️ بازگشت"
ALL_KINDS = "a"
# Filter code in callback data → kind, chip label and the icon shown before results without one.
KIND_CODES: dict[str, CommandKind] = {
    "d": CommandKind.DATA,
    "m": CommandKind.MENU,
    "s": CommandKind.SETTING,
    "l": CommandKind.LABEL,
}
KIND_CHIPS: dict[str, str] = {ALL_KINDS: "همه", "d": "🗂 رکورد", "m": "📋 منو", "s": "⚙️ تنظیم", "l": "✏️ متن دکمه"}
DATA_ICONS: dict[DataKind, str] = {
    DataKind.CUSTOMER: "👤",
    DataKind.RESELLER: "🤝",
    DataKind.LINK_OWNER: "🔗",
    DataKind.ORDER: "🧾",
}
# Card callbacks: the platform owner's and a shop's; a kind missing here is not offered to that viewer.
OWNER_DATA_CARDS: dict[DataKind, str] = {
    DataKind.CUSTOMER: "adm:users:view:{}",
    DataKind.LINK_OWNER: "adm:users:view:{}",
    DataKind.RESELLER: "adm:resellers:view:{}",
    DataKind.ORDER: "adm:order:{}",
}
SHOP_DATA_CARDS: dict[DataKind, str] = {DataKind.CUSTOMER: "res:user:{}", DataKind.LINK_OWNER: "res:user:{}"}
KIND_ICONS: dict[CommandKind, str] = {CommandKind.SETTING: "⚙️", CommandKind.LABEL: "✏️"}


class CommandCenterStates(StatesGroup):
    query = State()


@dataclass(frozen=True, slots=True)
class CommandViewer:
    entries: list[CommandEntry]
    home_callback: str
    # What records this viewer may find; None searches menus and settings only.
    data_scope: DataScope | None = None
    data_cards: dict[DataKind, str] = field(default_factory=dict)


async def load_command_viewer(
    session: AsyncSession, db_user: BotUser, *, is_reseller_bot: bool, reseller_owner_id: int | None
) -> CommandViewer | None:
    """The catalogue of the platform owner on the platform bot or of shop staff on their shop bot; else None."""
    if is_reseller_bot:
        hub = await load_reseller_hub(session, db_user, reseller_owner_id=reseller_owner_id)
        if hub is None:
            return None
        viewer = ShopViewer(hub.profile, hub.ui, hub.can_add_representative, hub.show_owner_creds)
        entries = shop_command_entries(viewer, reseller_settings.SECTIONS, reseller_settings.SUB_PERMISSIONS)
        scope = DataScope(
            shop_id=int(hub.profile.user_id), customers=has_bot_perm(hub.profile, CUSTOMER_PERMISSION)
        )
        return CommandViewer(entries, RESELLER_HOME_CALLBACK, scope, SHOP_DATA_CARDS)
    if not await is_bot_owner_principal(session, db_user, is_reseller_bot=False):
        return None
    ui = await get_all_settings(session)
    scope = DataScope(shop_id=None, customers=True, resellers=True, orders=True)
    return CommandViewer(admin_command_entries(ui, admin_settings.SECTIONS), ADMIN_HOME_CALLBACK, scope, OWNER_DATA_CARDS)


def data_entries(viewer: CommandViewer, hits: list[DataHit]) -> list[CommandEntry]:
    """Found records as results that open their existing cards; kinds without a card are dropped."""
    entries = []
    for hit in hits:
        card = viewer.data_cards.get(hit.kind)
        if card is not None:
            label = f"{DATA_ICONS[hit.kind]} {hit.label}"
            entries.append(CommandEntry(label, hit.detail, card.format(hit.entity_id), CommandKind.DATA))
    return entries


async def find_data_entries(session: AsyncSession, viewer: CommandViewer, query: str) -> list[CommandEntry]:
    if viewer.data_scope is None:
        return []
    try:
        hits = await search_data(session, query, viewer.data_scope)
    except SQLAlchemyError:  # records are a bonus; menus and settings are still found
        logger.warning("command center data search failed", exc_info=True)
        await rollback_quiet(session)
        return []
    return data_entries(viewer, hits)


def _stored_entries(data: dict) -> list[CommandEntry]:
    rows = data.get(DATA_KEY) or []
    return [CommandEntry(label, path, target, CommandKind.DATA) for label, path, target in rows]


def _kind_code(raw: str | None) -> str:
    return raw if raw in KIND_CODES else ALL_KINDS


def _stored_back(viewer: CommandViewer, data: dict) -> str:
    """The back target saved when the search was opened, if it is one this viewer may use."""
    back = data.get(BACK_KEY)
    return back if back in {viewer.home_callback, _back_for(viewer, None)} else viewer.home_callback


def results_callback(kind_code: str) -> str:
    return f"{RESULTS_PREFIX}{kind_code}"


def _result_text(entry: CommandEntry) -> str:
    place = entry.path.rsplit(PATH_SEPARATOR, 1)[-1]
    icon = KIND_ICONS.get(entry.kind)
    text = f"{icon} {entry.label} · {place}" if icon else f"{entry.label} · {place}"
    return text if len(text) <= BUTTON_TEXT_MAX else text[: BUTTON_TEXT_MAX - 1] + "…"


def _filter_rows(hits: list[CommandEntry], kind_code: str) -> list[list[InlineKeyboardButton]]:
    """«همه» and one chip per kind found; none when every result is of one kind."""
    counts = {code: len(filter_kind(hits, kind)) for code, kind in KIND_CODES.items()}
    present = [code for code, count in counts.items() if count]
    if len(present) < 2:
        return []
    counts[ALL_KINDS] = len(hits)
    chips = []
    for code in (ALL_KINDS, *present):
        mark = "• " if code == kind_code else ""
        chips.append(
            InlineKeyboardButton(
                text=f"{mark}{KIND_CHIPS[code]} {to_fa_digits(counts[code])}", callback_data=results_callback(code)
            )
        )
    return [chips[i : i + CHIPS_PER_ROW] for i in range(0, len(chips), CHIPS_PER_ROW)]


def _footer(home_callback: str) -> list[InlineKeyboardButton]:
    return [
        InlineKeyboardButton(text=SEARCH_AGAIN, callback_data=COMMAND_CENTER_CALLBACK),
        InlineKeyboardButton(text=BACK, callback_data=home_callback),
    ]


def results_panel(
    viewer: CommandViewer,
    query: str,
    *,
    kind_code: str = ALL_KINDS,
    page_index: int = 0,
    back_callback: str | None = None,
    records: list[CommandEntry] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """One page of results (``records`` first) as an inline panel; the query is escaped, never trusted."""
    is_link = query == LINK_QUERY
    heading = LINK_HEADING if is_link else f"🔎 <b>«{html.escape(query[:SEARCH_QUERY_MAX_LEN])}»</b>"
    footer = _footer(back_callback or viewer.home_callback)
    searchable = not is_link and bool(query_words(query))
    hits = [*(records or []), *(search_commands(viewer.entries, query) if searchable else [])]
    if not hits:
        reason = NOTHING_FOUND if searchable or is_link or records else TOO_SHORT
        return f"{heading}\n\n{reason}", InlineKeyboardMarkup(inline_keyboard=[footer])
    kind = KIND_CODES.get(kind_code)
    shown = filter_kind(hits, kind) or hits
    page: Page[CommandEntry] = page_of(shown, page_index, PAGE_SIZE)
    rows: list[list[InlineKeyboardButton]] = []
    rows.extend(_filter_rows(hits, kind_code if kind else ALL_KINDS))
    rows.extend([InlineKeyboardButton(text=_result_text(entry), callback_data=entry.target)] for entry in page.items)
    pager = pager_row(page, results_callback(kind_code))
    if pager:
        rows.append(pager)
    rows.append(footer)
    body = f"{heading} · {to_fa_digits(len(hits))} مورد\n{RESULTS_HINT}{page_line(page)}"
    return body, InlineKeyboardMarkup(inline_keyboard=rows)


def _back_for(viewer: CommandViewer, opened_with: str | None) -> str:
    """Back leads where the search was opened: the owner's settings hub or the panel home."""
    if opened_with != COMMAND_CENTER_CALLBACK and viewer.home_callback == ADMIN_HOME_CALLBACK:
        return ADMIN_SETTINGS_CALLBACK
    return viewer.home_callback


async def ask_command_query(
    callback: CallbackQuery,
    state: FSMContext,
    *,
    kind_code: str,
    back_callback: str,
    prompt: str = PROMPT,
    extra_inline: InlineKeyboardMarkup | None = None,
) -> None:
    """Show ``prompt`` and read the next typed text as a search; «انصراف» goes to ``back_callback``."""
    await state.update_data(**{KIND_KEY: kind_code, BACK_KEY: back_callback})
    await ask_text(
        callback,
        state,
        prompt=prompt,
        cancel_code=CANCEL_CODE,
        fsm_state=CommandCenterStates.query,
        extra_inline=extra_inline,
        edit=True,
        return_to=back_callback,
    )


# «⚡ مرکز فرمان» itself opens the status panel (``command_switches``); these open the plain search.
@router.callback_query(F.data.in_({COMMAND_CENTER_SETTINGS_CALLBACK, LEGACY_SETTINGS_SEARCH_CALLBACK}))
async def open_settings_search(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await load_command_viewer(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    await callback.answer()
    await ask_command_query(callback, state, kind_code="s", back_callback=_back_for(viewer, callback.data))


@router.message(CommandCenterStates.query)
async def run_command_search(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    text = (message.text or "").strip()
    if kb.is_cancel_text(text) and await try_legacy_cancel(
        message, state, session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    ):
        return
    viewer = await load_command_viewer(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        await state.clear()
        await message.answer(feedback.NO_ACCESS_REPLY)
        return
    body, markup = await _search_and_keep(session, state, viewer, text)
    await finish_text_step(message, state, text=body, inline=markup)
    if is_subscription_link(text):
        await delete_user_message(message)


def remembered(recent: list[str], query: str) -> list[str]:
    """``recent`` with ``query`` first; a link (stored as a marker) or a too-short query is not kept."""
    if query == LINK_QUERY or not query_words(query):
        return recent
    return [query, *(q for q in recent if q != query)][:RECENT_LIMIT]


async def _search_and_keep(
    session: AsyncSession, state: FSMContext, viewer: CommandViewer, text: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Search ``text`` and keep the query, the records and the recent list for the pages that follow."""
    # Checked on the whole text: a link is longer than a query and is matched in full.
    is_link = is_subscription_link(text)
    query = LINK_QUERY if is_link else text[:SEARCH_QUERY_MAX_LEN]
    records = await find_data_entries(session, viewer, text if is_link else query)
    data = await state.get_data()
    recent = [q for q in data.get(RECENT_KEY) or [] if isinstance(q, str)]
    # Leave the input step but keep the query and the records found, so result pages can be turned.
    await state.set_state(None)
    await state.update_data(
        **{
            QUERY_KEY: query,
            DATA_KEY: [[e.label, e.path, e.target] for e in records],
            RECENT_KEY: remembered(recent, query),
        }
    )
    return results_panel(
        viewer,
        query,
        kind_code=_kind_code(data.get(KIND_KEY)),
        back_callback=_stored_back(viewer, data),
        records=records,
    )


def recent_rows(data: dict) -> list[list[InlineKeyboardButton]]:
    """One button per recent search, two per row, for the command center panel."""
    recent = [q for q in data.get(RECENT_KEY) or [] if isinstance(q, str)][:RECENT_LIMIT]
    buttons = [
        InlineKeyboardButton(text=f"🕘 {query[:RECENT_TEXT_MAX]}", callback_data=f"{RECENT_PREFIX}{index}")
        for index, query in enumerate(recent)
    ]
    return [buttons[i : i + 2] for i in range(0, len(buttons), 2)]


@router.callback_query(F.data.startswith("cmd:q:"))
async def repeat_recent_search(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await load_command_viewer(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    raw_index = (callback.data or "")[len(RECENT_PREFIX):]
    recent = (await state.get_data()).get(RECENT_KEY) or []
    if not raw_index.isdigit() or int(raw_index) >= len(recent) or not isinstance(recent[int(raw_index)], str):
        await callback.answer(feedback.INVALID, show_alert=True)
        return
    await callback.answer()
    body, markup = await _search_and_keep(session, state, viewer, recent[int(raw_index)])
    await safe_edit_text(callback.message, body, reply_markup=markup)


# A literal prefix keeps the handler visible to the callback contract check.
@router.callback_query(F.data.startswith("cmd:r:"))
async def turn_results_page(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    viewer = await load_command_viewer(
        session, db_user, is_reseller_bot=is_reseller_bot, reseller_owner_id=reseller_owner_id
    )
    if viewer is None:
        await callback.answer(feedback.NO_ACCESS, show_alert=True)
        return
    data = await state.get_data()
    query = data.get(QUERY_KEY)
    if not isinstance(query, str) or not query:
        # The stored query is gone (another screen cleared it): ask again instead of showing nothing.
        await callback.answer()
        await ask_command_query(callback, state, kind_code=ALL_KINDS, back_callback=viewer.home_callback)
        return
    await callback.answer()
    raw = callback.data or ""
    body, markup = results_panel(
        viewer,
        query,
        kind_code=_kind_code(raw[len(RESULTS_PREFIX):].split(":", 1)[0]),
        page_index=page_number(raw),
        back_callback=_stored_back(viewer, data),
        records=_stored_entries(data),
    )
    await safe_edit_text(callback.message, body, reply_markup=markup)


async def _reopen_home(
    message: Message,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    *,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
) -> None:
    """Typed «انصراف» without a stored return target: back to the panel the search was opened from."""
    if is_reseller_bot:
        await open_reseller_home(
            message, session, db_user, state, is_reseller_bot=True, reseller_owner_id=reseller_owner_id, push=False
        )
        return
    await open_admin_home(message, session, db_user, state, push=False, is_reseller_bot=False)


register_cancel_code(CANCEL_CODE, CancelEntry(reopen=_reopen_home))
