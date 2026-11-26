"""Frequently asked questions: one button per question under «پشتیبانی», per shop.

Every read and write takes the shop as ``reseller_id`` (``None`` is the platform bot) and filters on
it, so an id from a callback or a form can never reach another shop's question. Answers are
Telegram HTML: the bot stores ``message.html_text`` (bold, links and premium emoji survive) and the
web panel's text is passed through ``normalize_telegram_html`` so a stray ``<`` can never make
Telegram reject the answer. Writes that depend on how many questions a shop has (the cap, the next
position, the one-time import) take a per-shop advisory lock, so two staff saving at once cannot
pass the cap or share a position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from sqlalchemy import ColumnElement, delete, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FaqItem
from app.services.button_icons import clean_icon_id
from app.services.rich_text import rich_plain_text
from app.services.telegram_html import normalize_telegram_html
from app.services.users import current_shop_reseller_id

MAX_FAQ_ITEMS = 40
QUESTION_MAX = 120
ANSWER_MAX = 3500
BUTTON_LABEL_MAX = 64
# Stored ``button_style`` values; ``None`` inherits the «هر سوال متداول» color.
INHERIT_STYLE = "inherit"
BUTTON_STYLES = frozenset({"", "primary", "success", "danger"})
LEGACY_QUESTION_MARK = "❓"
LEGACY_TITLE = "سوالات متداول"
# Namespace for ``pg_advisory_xact_lock(namespace, shop)``; any constant unique to this module.
_LOCK_NAMESPACE = 7_301_142
_WHITESPACE = re.compile(r"\s+")

QUESTION_REQUIRED = "متن سوال را بنویسید."
QUESTION_TOO_LONG = f"سوال حداکثر {QUESTION_MAX} نویسه است."
ANSWER_REQUIRED = "جواب سوال را بنویسید."
ANSWER_TOO_LONG = f"جواب حداکثر {ANSWER_MAX} نویسه است."
STYLE_INVALID = "رنگ دکمه معتبر نیست."
ICON_INVALID = "شناسهٔ اموجی پریمیوم فقط عدد است."
LIMIT_REACHED = f"حداکثر {MAX_FAQ_ITEMS} سوال می‌توانید داشته باشید."
NOT_FOUND = "این سوال پیدا نشد؛ شاید حذف شده باشد."
NOTHING_TO_IMPORT = "متن قبلی سوالات متداول خالی است."
ALREADY_HAS_ITEMS = "سوال‌ها قبلاً ساخته شده‌اند؛ متن قبلی فقط وقتی فهرست خالی است وارد می‌شود."


class FaqError(ValueError):
    """A question or answer that breaks a rule; the message is safe to show to staff."""


@dataclass(slots=True)
class FaqDraft:
    """What staff submit for one question, before it is checked."""

    question: str
    answer: str
    button_style: str | None = None
    icon_custom_emoji_id: str | None = None
    is_active: bool = True


@dataclass(frozen=True, slots=True)
class FaqButton:
    """What the customer keyboard needs for one question."""

    item_id: int
    label: str
    button_style: str | None
    icon_custom_emoji_id: str | None


def _shop(reseller_id: int | None) -> ColumnElement[bool]:
    if reseller_id is None:
        return FaqItem.reseller_id.is_(None)
    return FaqItem.reseller_id == int(reseller_id)


async def _lock_shop(session: AsyncSession, reseller_id: int | None) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :shop)"),
        {"namespace": _LOCK_NAMESPACE, "shop": int(reseller_id or 0)},
    )


def clean_question(raw: str | None) -> str:
    """One line of text for a button: newlines and repeated spaces become one space."""
    question = _WHITESPACE.sub(" ", raw or "").strip()
    if not question:
        raise FaqError(QUESTION_REQUIRED)
    if len(question) > QUESTION_MAX:
        raise FaqError(QUESTION_TOO_LONG)
    return question


def clean_answer(raw: str | None) -> str:
    """Telegram-safe HTML answer; ``FaqError`` when it is empty, too long or has broken tags."""
    try:
        answer = normalize_telegram_html((raw or "").replace("\r\n", "\n").strip())
    except ValueError as exc:
        raise FaqError(str(exc)) from None
    if not rich_plain_text(answer).strip():
        raise FaqError(ANSWER_REQUIRED)
    if len(answer) > ANSWER_MAX:
        raise FaqError(ANSWER_TOO_LONG)
    return answer


def clean_button_style(raw: object) -> str | None:
    """``None`` (inherit), ``""`` (client default) or a Telegram color; anything else is refused."""
    if isinstance(raw, bool):
        raise FaqError(STYLE_INVALID)
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if value == INHERIT_STYLE:
        return None
    if value not in BUTTON_STYLES:
        raise FaqError(STYLE_INVALID)
    return value


def clean_icon(raw: object) -> str | None:
    """A premium emoji id, ``None`` when blank; a non-blank value that is not an id is refused."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    icon = clean_icon_id(raw)
    if icon is None:
        raise FaqError(ICON_INVALID)
    return icon


def checked_draft(draft: FaqDraft) -> FaqDraft:
    """``draft`` with every field cleaned; ``FaqError`` on the first rule it breaks."""
    return FaqDraft(
        question=clean_question(draft.question),
        answer=clean_answer(draft.answer),
        button_style=clean_button_style(draft.button_style),
        icon_custom_emoji_id=clean_icon(draft.icon_custom_emoji_id),
        is_active=bool(draft.is_active),
    )


def button_label(question: str) -> str:
    """The question cut to what fits on a Telegram button."""
    if len(question) <= BUTTON_LABEL_MAX:
        return question
    return question[: BUTTON_LABEL_MAX - 1].rstrip() + "…"


async def list_faq_items(
    session: AsyncSession, *, reseller_id: int | None, active_only: bool = False
) -> list[FaqItem]:
    """The shop's questions in the order customers see them."""
    stmt = select(FaqItem).where(_shop(reseller_id))
    if active_only:
        stmt = stmt.where(FaqItem.is_active.is_(True))
    stmt = stmt.order_by(FaqItem.sort_order, FaqItem.id).limit(MAX_FAQ_ITEMS)
    return list(await session.scalars(stmt))


async def faq_buttons(session: AsyncSession, *, reseller_id: int | None) -> list[FaqButton]:
    """Active questions as keyboard rows, loading only the columns a button needs."""
    rows = await session.execute(
        select(FaqItem.id, FaqItem.question, FaqItem.button_style, FaqItem.icon_custom_emoji_id)
        .where(_shop(reseller_id), FaqItem.is_active.is_(True))
        .order_by(FaqItem.sort_order, FaqItem.id)
        .limit(MAX_FAQ_ITEMS)
    )
    return [
        FaqButton(int(row.id), button_label(row.question), row.button_style, clean_icon_id(row.icon_custom_emoji_id))
        for row in rows
    ]


async def has_active_faq(session: AsyncSession, *, reseller_id: int | None) -> bool:
    """True when the shop has at least one question to show; one indexed lookup."""
    found = await session.scalar(
        select(exists().where(_shop(reseller_id), FaqItem.is_active.is_(True)))
    )
    return bool(found)


async def current_shop_has_faq(session: AsyncSession) -> bool:
    """``has_active_faq`` for the shop whose bot received the current update."""
    return await has_active_faq(session, reseller_id=current_shop_reseller_id())


async def get_faq_item(session: AsyncSession, item_id: int, *, reseller_id: int | None) -> FaqItem:
    """The shop's question ``item_id``; ``FaqError`` when it belongs to no such shop."""
    item = await session.scalar(select(FaqItem).where(FaqItem.id == int(item_id), _shop(reseller_id)))
    if item is None:
        raise FaqError(NOT_FOUND)
    return item


async def create_faq_item(session: AsyncSession, draft: FaqDraft, *, reseller_id: int | None) -> FaqItem:
    """Add a question at the end of the shop's list."""
    clean = checked_draft(draft)
    await _lock_shop(session, reseller_id)
    count, last = (
        await session.execute(
            select(func.count(FaqItem.id), func.max(FaqItem.sort_order)).where(_shop(reseller_id))
        )
    ).one()
    if int(count or 0) >= MAX_FAQ_ITEMS:
        await session.rollback()
        raise FaqError(LIMIT_REACHED)
    item = FaqItem(
        reseller_id=reseller_id,
        question=clean.question,
        answer=clean.answer,
        button_style=clean.button_style,
        icon_custom_emoji_id=clean.icon_custom_emoji_id,
        is_active=clean.is_active,
        sort_order=int(last or 0) + 1,
    )
    session.add(item)
    await session.commit()
    return item


async def update_faq_item(
    session: AsyncSession, item_id: int, draft: FaqDraft, *, reseller_id: int | None
) -> FaqItem:
    """Replace a question's text, answer, color, icon and visibility."""
    clean = checked_draft(draft)
    item = await get_faq_item(session, item_id, reseller_id=reseller_id)
    item.question = clean.question
    item.answer = clean.answer
    item.button_style = clean.button_style
    item.icon_custom_emoji_id = clean.icon_custom_emoji_id
    item.is_active = clean.is_active
    await session.commit()
    return item


def draft_of(item: FaqItem) -> FaqDraft:
    """The stored question as a draft, to change one field and save."""
    return FaqDraft(
        question=item.question,
        answer=item.answer,
        button_style=item.button_style,
        icon_custom_emoji_id=item.icon_custom_emoji_id,
        is_active=bool(item.is_active),
    )


async def change_faq_item(
    session: AsyncSession, item_id: int, *, reseller_id: int | None, **changes: object
) -> FaqItem:
    """Change only the named ``FaqDraft`` fields (the bot edits one thing at a time)."""
    item = await get_faq_item(session, item_id, reseller_id=reseller_id)
    unknown = set(changes) - set(FaqDraft.__slots__)
    if unknown:
        raise TypeError(f"unknown FAQ fields: {sorted(unknown)}")
    return await update_faq_item(session, item_id, replace(draft_of(item), **changes), reseller_id=reseller_id)


async def delete_faq_item(session: AsyncSession, item_id: int, *, reseller_id: int | None) -> None:
    """Remove the shop's question; ``FaqError`` when it is not the shop's."""
    result = await session.execute(delete(FaqItem).where(FaqItem.id == int(item_id), _shop(reseller_id)))
    if not result.rowcount:
        await session.rollback()
        raise FaqError(NOT_FOUND)
    await session.commit()


async def move_faq_item(session: AsyncSession, item_id: int, *, step: int, reseller_id: int | None) -> FaqItem:
    """Swap the question with its neighbour above (``step`` < 0) or below; the ends stay put."""
    await _lock_shop(session, reseller_id)
    items = await list_faq_items(session, reseller_id=reseller_id)
    index = next((i for i, row in enumerate(items) if row.id == int(item_id)), None)
    if index is None:
        await session.rollback()
        raise FaqError(NOT_FOUND)
    target = index + (-1 if step < 0 else 1)
    if 0 <= target < len(items):
        items[index], items[target] = items[target], items[index]
        # Renumber the whole list: old rows may share a position, and a swap of equal values moves nothing.
        for position, row in enumerate(items, start=1):
            row.sort_order = position
    await session.commit()
    return items[target] if 0 <= target < len(items) else items[index]


async def _bump(session: AsyncSession, item_id: int, column: str, *, reseller_id: int | None) -> FaqItem | None:
    counter = getattr(FaqItem, column)
    row = await session.scalar(
        update(FaqItem)
        .where(FaqItem.id == int(item_id), _shop(reseller_id), FaqItem.is_active.is_(True))
        .values({column: counter + 1})
        .returning(FaqItem)
    )
    await session.commit()
    return row


async def record_faq_view(session: AsyncSession, item_id: int, *, reseller_id: int | None) -> FaqItem | None:
    """Count one opening of an active question and return it; ``None`` when hidden or not the shop's."""
    return await _bump(session, item_id, "view_count", reseller_id=reseller_id)


async def record_faq_ticket(session: AsyncSession, item_id: int, *, reseller_id: int | None) -> FaqItem | None:
    """Count one «جوابم را نگرفتم» on an active question and return it."""
    return await _bump(session, item_id, "ticket_count", reseller_id=reseller_id)


def parse_legacy_faq_text(raw: str | None) -> list[tuple[str, str]]:
    """Split the old one-message FAQ into (question, answer) pairs.

    Blocks are separated by a blank line; a block whose first line starts with «❓» is a question
    and the lines after it its answer. Text that does not follow that shape becomes one question
    titled «سوالات متداول», so nothing the shop wrote is lost.
    """
    body = rich_plain_text(raw).replace("\r\n", "\n").strip()
    if not body:
        return []
    pairs: list[tuple[str, str]] = []
    for block in re.split(r"\n\s*\n", body):
        lines = [line.strip() for line in block.strip().split("\n") if line.strip()]
        if len(lines) < 2 or not lines[0].startswith(LEGACY_QUESTION_MARK):
            return [(LEGACY_TITLE, body)]
        pairs.append((lines[0].removeprefix(LEGACY_QUESTION_MARK).strip(), "\n".join(lines[1:])))
    return pairs


async def import_legacy_faq(session: AsyncSession, raw: str | None, *, reseller_id: int | None) -> int:
    """Turn the old FAQ text into questions, once, while the shop has none; returns how many."""
    pairs = parse_legacy_faq_text(raw)
    if not pairs:
        raise FaqError(NOTHING_TO_IMPORT)
    drafts = [checked_draft(FaqDraft(question=q[:QUESTION_MAX], answer=a)) for q, a in pairs[:MAX_FAQ_ITEMS]]
    await _lock_shop(session, reseller_id)
    if await session.scalar(select(exists().where(_shop(reseller_id)))):
        await session.rollback()
        raise FaqError(ALREADY_HAS_ITEMS)
    session.add_all(
        FaqItem(reseller_id=reseller_id, question=d.question, answer=d.answer, sort_order=position)
        for position, d in enumerate(drafts, start=1)
    )
    await session.commit()
    return len(drafts)
