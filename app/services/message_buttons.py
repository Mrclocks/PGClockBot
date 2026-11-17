"""Buttons staff put under a broadcast or a direct message (one to four), and saved button sets.

A button opens a bot screen from a fixed list, renews the recipient's service (direct messages only)
or opens an ``https://`` link. Staff pick them from a checklist in the bot or add rows (target, label,
colour) on the web; a saved set (preset) is made on the web and can be picked in both. Everything is
validated when chosen and again when read, so a stored set can never produce a button Telegram
refuses or one that opens a screen outside the list.

Screen buttons open under the message (``notice_links``), so the message stays readable. Links are
opened by the customer's Telegram app; a shop may add them only with the ``broadcast_links``
permission, so a shop's customers are not sent to arbitrary sites without the owner allowing it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from aiogram.types import InlineKeyboardButton
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageButtonPreset, ResellerProfile
from app.services.button_styles import STYLE_OPTIONS, normalize_style
from app.services.notice_links import notice_open_data
from app.services.resellers import has_bot_perm

KIND_SCREEN = "screen"
KIND_URL = "url"
KIND_RENEW = "renew"
TARGET_LINK = "link"
TARGET_RENEW = "renew"
LINKS_PERMISSION = "broadcast_links"
BUTTONS_MAX = 4
PER_ROW = 2
LABEL_MAX = 40
URL_MAX = 256
PRESET_NAME_MAX = 40
PRESETS_PER_SHOP = 30
RENEW_LABEL = "🔄 تمدید سرویس"
LINK_LABEL = "🔗 لینک"
# key -> (screen callback, default label)
SCREENS: dict[str, tuple[str, str]] = {
    "shop": ("shop:list", "🛒 خرید سرویس"),
    "services": ("svc:list", "📦 سرویس‌های من"),
    "wallet": ("nv:w:home", "👛 کیف پول"),
    "support": ("nv:s:home", "✉️ پشتیبانی"),
    "club": ("nv:loy:home", "⭐ باشگاه مشتریان"),
}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ButtonInput:
    """One button as staff chose it: a target (screen key, ``renew`` or ``link``), label, URL and colour."""

    target: str
    label: str = ""
    url: str = ""
    style: str = ""


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    kind: str
    label: str
    target: str
    style: str = ""


@dataclass(frozen=True, slots=True)
class ButtonRules:
    """What a sender may use: links (owner, or a shop with permission) and renewal of this one service."""

    allow_links: bool = False
    renew_service_id: int | None = None


def _clean_label(raw: str, default: str) -> str:
    label = _CONTROL_RE.sub("", raw or "").strip() or default
    if not label:
        raise ValueError("متن دکمه را بنویسید")
    if len(label) > LABEL_MAX:
        raise ValueError(f"متن دکمه حداکثر {LABEL_MAX} نویسه است")
    return label


def _valid_url(raw: str) -> bool:
    if not raw or len(raw) > URL_MAX or _CONTROL_RE.search(raw) or " " in raw:
        return False
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and "." in host and not parts.username and not parts.password


def make_button(item: ButtonInput, rules: ButtonRules) -> ButtonSpec:
    """A validated button; ``ValueError`` with a message staff can act on."""
    target = (item.target or "").strip().lower()
    style = normalize_style(item.style)
    if target in SCREENS:
        return ButtonSpec(KIND_SCREEN, _clean_label(item.label, SCREENS[target][1]), target, style)
    if target == TARGET_RENEW:
        if rules.renew_service_id is None:
            raise ValueError("دکمهٔ تمدید فقط در پیام شخصی به مشتریِ دارای سرویس مجاز است")
        return ButtonSpec(KIND_RENEW, _clean_label(item.label, RENEW_LABEL), str(int(rules.renew_service_id)), style)
    if target == TARGET_LINK:
        url = (item.url or "").strip()
        if not _valid_url(url):
            raise ValueError("لینک دکمه باید با https:// شروع شود و فاصله نداشته باشد")
        if not rules.allow_links:
            raise ValueError("اجازهٔ دکمهٔ لینک بیرونی را ندارید؛ فقط دکمه‌های ربات مجاز است")
        return ButtonSpec(KIND_URL, _clean_label(item.label, ""), url, style)
    raise ValueError("مقصد دکمه نامعتبر است")


def make_buttons(items: Iterable[ButtonInput], rules: ButtonRules) -> list[ButtonSpec]:
    """Validated buttons in order; rows left without a target are skipped. At most ``BUTTONS_MAX``."""
    chosen = [item for item in items if (item.target or "").strip()]
    if len(chosen) > BUTTONS_MAX:
        raise ValueError(f"حداکثر {BUTTONS_MAX} دکمه زیر هر پیام")
    return [make_button(item, rules) for item in chosen]


def dump_buttons(buttons: list[ButtonSpec]) -> str | None:
    if not buttons:
        return None
    return json.dumps([{"k": b.kind, "l": b.label, "t": b.target, "s": b.style} for b in buttons], ensure_ascii=False)


def _stored_input(item: Mapping) -> ButtonInput:
    kind, target = str(item["k"]), str(item["t"])
    if kind == KIND_URL:
        return ButtonInput(TARGET_LINK, str(item["l"]), target, str(item.get("s") or ""))
    if kind == KIND_RENEW:
        return ButtonInput(TARGET_RENEW, str(item["l"]), "", str(item.get("s") or ""))
    return ButtonInput(target, str(item["l"]), "", str(item.get("s") or ""))


def stored_inputs(stored: str | None) -> list[ButtonInput]:
    """Stored buttons as inputs again (for a preset in the form); malformed entries are dropped."""
    try:
        data = json.loads(stored) if stored else []
    except ValueError:
        return []
    out = []
    for item in data if isinstance(data, list) else []:
        try:
            out.append(_stored_input(item))
        except (TypeError, KeyError, AttributeError):
            continue
    return out[:BUTTONS_MAX]


def load_buttons(stored: str | None, rules: ButtonRules) -> list[ButtonSpec]:
    """Stored buttons, validated again; anything no longer allowed is dropped rather than sent."""
    try:
        data = json.loads(stored) if stored else []
    except ValueError:
        return []
    out: list[ButtonSpec] = []
    for item in data if isinstance(data, list) else []:
        try:
            spec = make_button(_stored_input(item), rules)
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        # A stored renewal keeps the service it was made for.
        out.append(ButtonSpec(KIND_RENEW, spec.label, str(int(item["t"])), spec.style) if spec.kind == KIND_RENEW else spec)
    return out[:BUTTONS_MAX]


def build_keyboard(buttons: list[ButtonSpec]) -> list[list[InlineKeyboardButton]]:
    """Telegram buttons, two to a row."""
    def button(spec: ButtonSpec) -> InlineKeyboardButton:
        extra = {"style": spec.style} if spec.style else {}
        if spec.kind == KIND_URL:
            return InlineKeyboardButton(text=spec.label, url=spec.target, **extra)
        data = SCREENS[spec.target][0] if spec.kind == KIND_SCREEN else f"svc:renew:{int(spec.target)}"
        return InlineKeyboardButton(text=spec.label, callback_data=notice_open_data(data), **extra)

    keyboard = [button(spec) for spec in buttons]
    return [keyboard[i:i + PER_ROW] for i in range(0, len(keyboard), PER_ROW)]


async def shop_may_link(session: AsyncSession, shop_id: int | None) -> bool:
    """The platform always may; a shop only with the ``broadcast_links`` permission (deny by default)."""
    if shop_id is None:
        return True
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    return bool(profile is not None and profile.is_active and has_bot_perm(profile, LINKS_PERMISSION))


async def stored_keyboard(session: AsyncSession, stored: str | None, *, shop_id: int | None) -> list[list[InlineKeyboardButton]]:
    """Buttons of a queued broadcast, checked against what the shop may send now (a revoked link is dropped)."""
    if not stored:
        return []
    return build_keyboard(load_buttons(stored, ButtonRules(allow_links=await shop_may_link(session, shop_id))))


def preview_keyboard(stored: str | None) -> list[list[InlineKeyboardButton]]:
    """Buttons this request has just validated, for a preview."""
    items = json.loads(stored) if stored else []
    return build_keyboard([ButtonSpec(str(i["k"]), str(i["l"]), str(i["t"]), str(i.get("s") or "")) for i in items])


async def broadcast_buttons(session: AsyncSession, items: list[ButtonInput], *, shop_id: int | None) -> str | None:
    """Stored buttons for a broadcast of ``shop_id`` (no renewal: a broadcast has no single service)."""
    rules = ButtonRules(allow_links=await shop_may_link(session, shop_id))
    return dump_buttons(make_buttons(items, rules))


def form_inputs(form: Mapping) -> list[ButtonInput]:
    """The rows of a web form's button list (``btn_target``, ``btn_label``, ``btn_url``, ``btn_style``)."""
    def values(name: str) -> list[str]:
        getter = getattr(form, "getlist", None)
        return [str(v) for v in getter(name)] if getter else []

    targets, labels, urls, styles = (values(n) for n in ("btn_target", "btn_label", "btn_url", "btn_style"))
    pick = lambda seq, i: seq[i] if i < len(seq) else ""  # noqa: E731 - index with a default
    return [ButtonInput(t, pick(labels, i), pick(urls, i), pick(styles, i)) for i, t in enumerate(targets)]


def target_options(*, renew: bool, links: bool) -> list[tuple[str, str]]:
    """(value, label) of the targets a form or checklist offers."""
    options = [(key, label) for key, (_cb, label) in SCREENS.items()]
    if renew:
        options.append((TARGET_RENEW, RENEW_LABEL))
    if links:
        options.append((TARGET_LINK, LINK_LABEL))
    return options


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


async def preset_inputs(session: AsyncSession, shop_id: int | None, preset_id: int) -> list[ButtonInput]:
    """The buttons of one of this shop's presets; another shop's preset reads as empty."""
    row = await session.scalar(
        select(MessageButtonPreset).where(
            MessageButtonPreset.id == int(preset_id), MessageButtonPreset.shop_key == shop_key(shop_id)
        )
    )
    return stored_inputs(row.buttons) if row is not None else []


def _clean_preset_name(raw: str) -> str:
    name = _CONTROL_RE.sub("", raw or "").strip()
    if not name or len(name) > PRESET_NAME_MAX:
        raise ValueError(f"نام قالب ۱ تا {PRESET_NAME_MAX} نویسه است")
    return name


async def save_preset(
    session: AsyncSession, shop_id: int | None, *, name: str, items: list[ButtonInput], rules: ButtonRules
) -> MessageButtonPreset:
    """Create or replace one shop's preset; presets never hold a per-customer renewal."""
    clean_name = _clean_preset_name(name)
    buttons = make_buttons(items, ButtonRules(allow_links=rules.allow_links))
    if not buttons:
        raise ValueError("قالب بدون دکمه ذخیره نمی‌شود")
    key = shop_key(shop_id)
    row = await session.scalar(
        select(MessageButtonPreset).where(MessageButtonPreset.shop_key == key, MessageButtonPreset.name == clean_name)
    )
    if row is None:
        if len(await list_presets(session, shop_id)) >= PRESETS_PER_SHOP:
            raise ValueError(f"حداکثر {PRESETS_PER_SHOP} قالب دکمه مجاز است")
        row = MessageButtonPreset(shop_key=key, name=clean_name)
        session.add(row)
    row.buttons = dump_buttons(buttons) or "[]"
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


async def buttons_form_context(session: AsyncSession, shop_id: int | None) -> dict[str, object]:
    """What a web page with a button list needs: targets, colours and this shop's presets."""
    links = await shop_may_link(session, shop_id)
    presets = await list_presets(session, shop_id)
    return {
        "button_targets": target_options(renew=False, links=links),
        "button_styles": STYLE_OPTIONS,
        "button_max": BUTTONS_MAX,
        "button_presets": [
            {
                "id": p.id,
                "name": p.name,
                "buttons": [{"target": i.target, "label": i.label, "url": i.url, "style": i.style} for i in stored_inputs(p.buttons)],
            }
            for p in presets
        ],
        "button_preset_limit": PRESETS_PER_SHOP,
    }
