"""Shop / bot terms (rules) gates — entry + purchase, scoped per shop."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, TermsAcceptance
from app.services.rich_text import content_fingerprint, unpack_rich_text
from app.services.users import on

logger = logging.getLogger(__name__)

GateId = Literal["entry", "buy_user", "buy_reseller"]

GATES: tuple[GateId, ...] = ("entry", "buy_user", "buy_reseller")

GATE_META: dict[GateId, dict[str, str]] = {
    "entry": {
        "title": "ورود به ربات",
        "enabled": "terms_entry_enabled",
        "text": "terms_entry_text",
        "btn": "terms_entry_btn",
        "style": "terms_entry",
        "reaccept": "terms_entry_reaccept",
    },
    "buy_user": {
        "title": "خرید پلن کاربر",
        "enabled": "terms_buy_user_enabled",
        "text": "terms_buy_user_text",
        "btn": "terms_buy_user_btn",
        "style": "terms_buy_user",
        "reaccept": "terms_buy_user_reaccept",
    },
    "buy_reseller": {
        "title": "خرید پلن نماینده",
        "enabled": "terms_buy_reseller_enabled",
        "text": "terms_buy_reseller_text",
        "btn": "terms_buy_reseller_btn",
        "style": "terms_buy_reseller",
        "reaccept": "terms_buy_reseller_reaccept",
    },
}


def shop_scope_id(*, reseller_owner_id: int | None = None) -> int:
    """0 = platform bot; else shop owner bot_user id."""
    return int(reseller_owner_id or 0)


@dataclass(frozen=True)
class TermsPrompt:
    gate: GateId
    text: str
    entities: list | None
    btn_label: str
    style_id: str


def gate_enabled(ui: dict, gate: GateId) -> bool:
    meta = GATE_META[gate]
    return on(ui.get(meta["enabled"]))


def build_prompt(ui: dict, gate: GateId) -> TermsPrompt | None:
    if not gate_enabled(ui, gate):
        return None
    meta = GATE_META[gate]
    raw = ui.get(meta["text"]) or ""
    text, entities = unpack_rich_text(raw)
    text = (text or "").strip()
    if not text:
        return None
    btn_raw = ui.get(meta["btn"]) or "موافقم"
    btn_label, _ = unpack_rich_text(btn_raw)
    btn_label = (btn_label or "موافقم").strip() or "موافقم"
    return TermsPrompt(
        gate=gate,
        text=text,
        entities=entities,
        btn_label=btn_label[:64],
        style_id=meta["style"],
    )


def current_hash(ui: dict, gate: GateId) -> str:
    meta = GATE_META[gate]
    return content_fingerprint(ui.get(meta["text"]))


def reaccept_on_change(ui: dict, gate: GateId) -> bool:
    meta = GATE_META[gate]
    # Default ON when key missing — safer for legal text changes.
    raw = ui.get(meta["reaccept"])
    if raw is None or str(raw).strip() == "":
        return True
    return on(raw)


async def has_accepted(
    session: AsyncSession,
    *,
    bot_user_id: int,
    shop_owner_id: int,
    gate: GateId,
    ui: dict,
) -> bool:
    if not gate_enabled(ui, gate):
        return True
    prompt = build_prompt(ui, gate)
    if prompt is None:
        return True
    want = current_hash(ui, gate)
    row = await session.scalar(
        select(TermsAcceptance).where(
            TermsAcceptance.bot_user_id == int(bot_user_id),
            TermsAcceptance.shop_owner_id == int(shop_owner_id),
            TermsAcceptance.gate == gate,
        )
    )
    if row is None:
        return False
    if reaccept_on_change(ui, gate) and (row.content_hash or "") != want:
        return False
    return True


async def record_acceptance(
    session: AsyncSession,
    *,
    bot_user_id: int,
    shop_owner_id: int,
    gate: GateId,
    ui: dict,
) -> None:
    """Record that this person accepted the current text of this gate; one row per person, shop and gate.

    Safe to run twice or concurrently: the row is refreshed with an atomic UPDATE, and a
    first acceptance is inserted inside a savepoint. Losing a concurrent insert rolls back
    only that savepoint, so the caller's loaded objects (the BotUser) are not expired.
    """
    if gate not in GATE_META:
        raise ValueError("گیت نامعتبر")
    want = current_hash(ui, gate)
    now = datetime.now(timezone.utc)
    key = (int(bot_user_id), int(shop_owner_id), gate)
    if not await _refresh_acceptance(session, key, content_hash=want, accepted_at=now):
        try:
            async with session.begin_nested():
                session.add(
                    TermsAcceptance(
                        bot_user_id=key[0],
                        shop_owner_id=key[1],
                        gate=gate,
                        content_hash=want,
                        accepted_at=now,
                    )
                )
        except IntegrityError:
            # A concurrent tap inserted the row first: refresh it instead.
            await _refresh_acceptance(session, key, content_hash=want, accepted_at=now)
    await session.commit()


async def _refresh_acceptance(
    session: AsyncSession,
    key: tuple[int, int, GateId],
    *,
    content_hash: str,
    accepted_at: datetime,
) -> bool:
    """Update this person's acceptance row in place; whether a row existed."""
    bot_user_id, shop_owner_id, gate = key
    result = await session.execute(
        update(TermsAcceptance)
        .where(
            TermsAcceptance.bot_user_id == bot_user_id,
            TermsAcceptance.shop_owner_id == shop_owner_id,
            TermsAcceptance.gate == gate,
        )
        .values(content_hash=content_hash, accepted_at=accepted_at)
    )
    return bool(result.rowcount)


async def needs_entry_gate(
    session: AsyncSession,
    db_user: BotUser,
    ui: dict,
    *,
    menu_role: str,
    reseller_owner_id: int | None = None,
) -> TermsPrompt | None:
    """Entry terms only for end-users (same audience as force-join)."""
    if menu_role != "user":
        return None
    prompt = build_prompt(ui, "entry")
    if prompt is None:
        return None
    ok = await has_accepted(
        session,
        bot_user_id=int(db_user.id),
        shop_owner_id=shop_scope_id(reseller_owner_id=reseller_owner_id),
        gate="entry",
        ui=ui,
    )
    return None if ok else prompt


async def needs_purchase_gate(
    session: AsyncSession,
    db_user: BotUser,
    ui: dict,
    gate: GateId,
    *,
    reseller_owner_id: int | None = None,
) -> TermsPrompt | None:
    """Return prompt when purchase terms are required and not yet accepted."""
    if gate not in ("buy_user", "buy_reseller"):
        return None
    prompt = build_prompt(ui, gate)
    if prompt is None:
        return None
    ok = await has_accepted(
        session,
        bot_user_id=int(db_user.id),
        shop_owner_id=shop_scope_id(reseller_owner_id=reseller_owner_id),
        gate=gate,
        ui=ui,
    )
    return None if ok else prompt
