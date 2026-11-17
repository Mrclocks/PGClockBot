"""Buttons staff put under a broadcast or a direct message, and the saved button sets (presets).

Staff write the buttons as lines: one line is one row, ``|`` separates buttons in a row, and each button
is ``label - target``. A target is a bot screen keyword (``@shop`` …), ``@renew:<service id>`` in a
direct message, or an ``https://`` link. A line ``@preset:<name>`` inserts a saved set. Everything is
validated when written and again when read, so a stored set can never produce a button Telegram
refuses or one that opens a screen outside the list below.

Screen buttons open under the message (``notice_links``), so the message stays readable. Links are
opened by the customer's Telegram app; a shop may add them only with the ``broadcast_links``
permission, so a shop's customers are not sent to arbitrary sites without the owner allowing it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageButtonPreset, ResellerProfile
from app.services.notice_links import notice_open_data
from app.services.resellers import has_bot_perm
from app.services.service_scope import list_customer_services

KIND_SCREEN = "screen"
KIND_URL = "url"
KIND_RENEW = "renew"
LINKS_PERMISSION = "broadcast_links"
ROWS_MAX = 8
PER_ROW_MAX = 3
LABEL_MAX = 40
URL_MAX = 256
PRESET_NAME_MAX = 40
PRESETS_PER_SHOP = 30
SEPARATOR_LINE = "---"
# One line for bot prompts; the web form shows ``help_lines`` instead.
BUTTONS_HINT = "دکمه زیر پیام (اختیاری): بعد از متن یک خط --- بگذارید و در خطوط بعد مثلاً «🛒 خرید - @shop» بنویسید."
# keyword -> (callback of the screen, description shown in the help)
SCREENS: dict[str, tuple[str, str]] = {
    "shop": ("shop:list", "خرید سرویس"),
    "services": ("svc:list", "سرویس‌های من"),
    "wallet": ("nv:w:home", "کیف پول"),
    "support": ("nv:s:home", "پشتیبانی"),
    "club": ("nv:loy:home", "باشگاه مشتریان"),
}
_RENEW_RE = re.compile(r"@renew:(\d{1,10})")
_PRESET_RE = re.compile(r"@preset:\s*(.+)")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    kind: str
    label: str
    target: str


Rows = list[list[ButtonSpec]]


@dataclass(frozen=True, slots=True)
class ButtonRules:
    """What a sender may use: links (owner, or a shop with permission) and renewal of these services."""

    allow_links: bool = False
    renew_service_ids: frozenset[int] = frozenset()


def _fail(line_no: int, reason: str) -> ValueError:
    return ValueError(f"دکمه‌ها، خط {line_no}: {reason}")


def _clean_label(raw: str, line_no: int) -> str:
    label = _CONTROL_RE.sub("", raw).strip()
    if not label:
        raise _fail(line_no, "متن دکمه خالی است")
    if len(label) > LABEL_MAX:
        raise _fail(line_no, f"متن دکمه حداکثر {LABEL_MAX} نویسه است")
    return label


def _valid_url(raw: str) -> bool:
    if len(raw) > URL_MAX or _CONTROL_RE.search(raw) or " " in raw:
        return False
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and "." in host and not parts.username and not parts.password


def _target(raw: str, rules: ButtonRules, line_no: int) -> tuple[str, str]:
    """(kind, target) for what follows the last `` - `` of a button."""
    target = raw.strip()
    keyword = target[1:].lower() if target.startswith("@") else ""
    if keyword in SCREENS:
        return KIND_SCREEN, keyword
    renew = _RENEW_RE.fullmatch(target)
    if renew:
        if int(renew.group(1)) not in rules.renew_service_ids:
            raise _fail(line_no, "تمدید فقط برای سرویس‌های همین مشتری در پیام شخصی مجاز است")
        return KIND_RENEW, renew.group(1)
    if target.startswith("@"):
        raise _fail(line_no, f"«{target}» شناخته نشد؛ یکی از: {', '.join('@' + k for k in SCREENS)}")
    if not _valid_url(target):
        raise _fail(line_no, "لینک باید با https:// شروع شود و فاصله نداشته باشد")
    if not rules.allow_links:
        raise _fail(line_no, "اجازهٔ دکمهٔ لینک بیرونی را ندارید؛ فقط دکمه‌های ربات (@shop و …) مجاز است")
    return KIND_URL, target


def _parse_button(raw: str, rules: ButtonRules, line_no: int) -> ButtonSpec:
    label, sep, target = raw.rpartition(" - ")
    if not sep:
        raise _fail(line_no, "هر دکمه به شکل «متن - مقصد» نوشته شود")
    kind, clean_target = _target(target, rules, line_no)
    return ButtonSpec(kind, _clean_label(label, line_no), clean_target)


def parse_buttons(raw: str, rules: ButtonRules) -> Rows:
    """Rows written as text; ``ValueError`` naming the line for anything not allowed."""
    rows: Rows = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        if not line.strip():
            continue
        parts = [part for part in line.split("|") if part.strip()]
        if len(parts) > PER_ROW_MAX:
            raise _fail(line_no, f"حداکثر {PER_ROW_MAX} دکمه در یک ردیف")
        rows.append([_parse_button(part, rules, line_no) for part in parts])
    if len(rows) > ROWS_MAX:
        raise ValueError(f"حداکثر {ROWS_MAX} ردیف دکمه مجاز است")
    return rows


def split_message(raw: str) -> tuple[str, str]:
    """(text, buttons) of a bot message: buttons follow a line holding only ``---``."""
    lines = (raw or "").splitlines()
    for index, line in enumerate(lines):
        if line.strip() == SEPARATOR_LINE:
            return "\n".join(lines[:index]).strip(), "\n".join(lines[index + 1:])
    return (raw or "").strip(), ""


def rows_to_text(rows: Rows) -> str:
    """The rows written back in the same format, e.g. to show a preset."""
    def target(spec: ButtonSpec) -> str:
        return {KIND_SCREEN: f"@{spec.target}", KIND_RENEW: f"@renew:{spec.target}"}.get(spec.kind, spec.target)

    return "\n".join(" | ".join(f"{spec.label} - {target(spec)}" for spec in row) for row in rows)


def dump_rows(rows: Rows) -> str | None:
    if not rows:
        return None
    return json.dumps([[{"k": b.kind, "l": b.label, "t": b.target} for b in row] for row in rows], ensure_ascii=False)


def load_rows(stored: str | None, rules: ButtonRules) -> Rows:
    """Stored rows, validated again; anything no longer allowed is dropped rather than sent."""
    try:
        data = json.loads(stored) if stored else []
    except ValueError:
        return []
    rows: Rows = []
    for row in data if isinstance(data, list) else []:
        clean = []
        for item in row if isinstance(row, list) else []:
            try:
                spec = _from_stored(item, rules)
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
            clean.append(spec)
        if clean:
            rows.append(clean[:PER_ROW_MAX])
    return rows[:ROWS_MAX]


def _from_stored(item: dict, rules: ButtonRules) -> ButtonSpec:
    kind, label, target = str(item["k"]), _clean_label(str(item["l"]), 0), str(item["t"])
    written = {KIND_SCREEN: f"@{target}", KIND_RENEW: f"@renew:{target}", KIND_URL: target}[kind]
    checked_kind, checked_target = _target(written, rules, 0)
    return ButtonSpec(checked_kind, label, checked_target)


def build_keyboard(rows: Rows) -> list[list[InlineKeyboardButton]]:
    """Telegram buttons for validated rows."""
    def button(spec: ButtonSpec) -> InlineKeyboardButton:
        if spec.kind == KIND_URL:
            return InlineKeyboardButton(text=spec.label, url=spec.target)
        data = SCREENS[spec.target][0] if spec.kind == KIND_SCREEN else f"svc:renew:{int(spec.target)}"
        return InlineKeyboardButton(text=spec.label, callback_data=notice_open_data(data))

    return [[button(spec) for spec in row] for row in rows]


def help_lines(rules: ButtonRules) -> list[str]:
    """How to write buttons, for the bot prompt and the web field help."""
    lines = [
        "هر خط یک ردیف؛ دکمه‌های یک ردیف را با | جدا کنید؛ هر دکمه: «متن - مقصد».",
        "مقصدها: " + "، ".join(f"@{key} ({title})" for key, (_cb, title) in SCREENS.items()),
    ]
    if rules.renew_service_ids:
        lines.append("تمدید یک سرویس همین مشتری: @renew:شمارهٔ سرویس")
    if rules.allow_links:
        lines.append("لینک: آدرسی که با https:// شروع شود.")
    lines.append("قالب ذخیره‌شده: یک خط ‎@preset:نام‌قالب")
    return lines


async def shop_may_link(session: AsyncSession, shop_id: int | None) -> bool:
    """The platform always may; a shop only with the ``broadcast_links`` permission (deny by default)."""
    if shop_id is None:
        return True
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    return bool(profile is not None and profile.is_active and has_bot_perm(profile, LINKS_PERMISSION))


async def stored_keyboard(session: AsyncSession, stored: str | None, *, shop_id: int | None) -> list[list[InlineKeyboardButton]]:
    """Buttons of a queued message, checked against what the shop may send now (a revoked link is dropped)."""
    if not stored:
        return []
    return build_keyboard(load_rows(stored, ButtonRules(allow_links=await shop_may_link(session, shop_id))))


async def direct_message_rules(session: AsyncSession, user_id: int, shop_id: int | None) -> ButtonRules:
    """A direct message may also renew the customer's own services sold by this shop."""
    services = await list_customer_services(session, int(user_id), shop_id)
    return ButtonRules(
        allow_links=await shop_may_link(session, shop_id),
        renew_service_ids=frozenset(int(service.id) for service in services),
    )


async def broadcast_buttons(session: AsyncSession, raw: str, *, shop_id: int | None) -> str | None:
    """Stored rows for a broadcast of ``shop_id`` (no per-customer renewal); None when there are none."""
    if not (raw or "").strip():
        return None
    rules = ButtonRules(allow_links=await shop_may_link(session, shop_id))
    return dump_rows(await resolve_buttons(session, raw, shop_id=shop_id, rules=rules))


def stored_markup_rows(stored: str | None) -> list[list[InlineKeyboardButton]]:
    """Buttons of rows this request has just validated, for a preview."""
    return build_keyboard(load_rows(stored, ButtonRules(allow_links=True)))


async def buttons_form_context(session: AsyncSession, shop_id: int | None, *, renew: bool = False) -> dict[str, object]:
    """What a web form with a buttons field shows: the help and this shop's presets."""
    allow_links = await shop_may_link(session, shop_id)
    presets = await list_presets(session, shop_id)
    rules = ButtonRules(allow_links=allow_links, renew_service_ids=frozenset({0}) if renew else frozenset())
    return {
        "buttons_help": help_lines(rules),
        "button_presets": [
            {"id": p.id, "name": p.name, "text": rows_to_text(load_rows(p.buttons, ButtonRules(allow_links=allow_links)))}
            for p in presets
        ],
        "button_preset_limit": PRESETS_PER_SHOP,
    }


def shop_key(shop_id: int | None) -> int:
    return int(shop_id) if shop_id else 0


async def list_presets(session: AsyncSession, shop_id: int | None) -> list[MessageButtonPreset]:
    return list(
        await session.scalars(
            select(MessageButtonPreset)
            .where(MessageButtonPreset.shop_key == shop_key(shop_id))
            .order_by(MessageButtonPreset.name)
            .limit(PRESETS_PER_SHOP)
        )
    )


def _clean_preset_name(raw: str) -> str:
    name = _CONTROL_RE.sub("", raw or "").strip()
    if not name or len(name) > PRESET_NAME_MAX:
        raise ValueError(f"نام قالب ۱ تا {PRESET_NAME_MAX} نویسه است")
    return name


async def save_preset(session: AsyncSession, shop_id: int | None, *, name: str, buttons: str, rules: ButtonRules) -> MessageButtonPreset:
    """Create or replace one shop's preset; presets never hold a per-customer renewal."""
    clean_name = _clean_preset_name(name)
    rows = parse_buttons(buttons, ButtonRules(allow_links=rules.allow_links))
    if not rows:
        raise ValueError("قالب بدون دکمه ذخیره نمی‌شود")
    key = shop_key(shop_id)
    row = await session.scalar(
        select(MessageButtonPreset).where(MessageButtonPreset.shop_key == key, MessageButtonPreset.name == clean_name)
    )
    if row is None:
        count = len(await list_presets(session, shop_id))
        if count >= PRESETS_PER_SHOP:
            raise ValueError(f"حداکثر {PRESETS_PER_SHOP} قالب دکمه مجاز است")
        row = MessageButtonPreset(shop_key=key, name=clean_name)
        session.add(row)
    row.buttons = dump_rows(rows) or "[]"
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ValueError("قالبی با همین نام هم‌زمان ذخیره شد؛ دوباره تلاش کنید") from None
    return row


async def delete_preset(session: AsyncSession, shop_id: int | None, preset_id: int) -> bool:
    """Delete a preset of this shop only; False when it is not this shop's."""
    result = await session.execute(
        delete(MessageButtonPreset).where(
            MessageButtonPreset.id == int(preset_id), MessageButtonPreset.shop_key == shop_key(shop_id)
        )
    )
    await session.commit()
    return bool(result.rowcount)


async def resolve_buttons(session: AsyncSession, raw: str, *, shop_id: int | None, rules: ButtonRules) -> Rows:
    """Parse ``raw`` with ``@preset:`` lines replaced by that shop's saved rows."""
    presets = {p.name: p for p in await list_presets(session, shop_id)}
    expanded: list[str] = []
    for line_no, line in enumerate((raw or "").splitlines(), start=1):
        match = _PRESET_RE.fullmatch(line.strip())
        if not match:
            expanded.append(line)
            continue
        preset = presets.get(match.group(1).strip())
        if preset is None:
            raise _fail(line_no, f"قالب «{match.group(1).strip()}» پیدا نشد")
        expanded.append(rows_to_text(load_rows(preset.buttons, ButtonRules(allow_links=rules.allow_links))))
    return parse_buttons("\n".join(expanded), rules)
