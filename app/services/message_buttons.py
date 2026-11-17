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

from app.db.models import MessageButtonPreset, Plan, ResellerProfile
from app.services.button_styles import STYLE_OPTIONS, normalize_style
from app.services.notice_links import notice_open_data
from app.services.resellers import has_bot_perm

KIND_SCREEN = "screen"
KIND_URL = "url"
KIND_RENEW = "renew"
KIND_PLAN = "plan"
TARGET_LINK = "link"
TARGET_RENEW = "renew"
TARGET_PLAN = "plan"
LINKS_PERMISSION = "broadcast_links"
BUTTONS_MAX = 4
PER_ROW = 2
LABEL_MAX = 40
URL_MAX = 256
PRESET_NAME_MAX = 40
PRESETS_PER_SHOP = 30
PLAN_OPTIONS_LIMIT = 200
RENEW_LABEL = "🔄 تمدید سرویس"
LINK_LABEL = "🔗 لینک"
PLAN_LABEL = "💎 پلن مشخص"
GROUP_BUY = "خرید"
GROUP_ACCOUNT = "حساب"
GROUP_CLUB = "باشگاه"
GROUP_HELP = "راهنما"
GROUP_OTHER = "دیگر"
# key -> (screen callback, default label, group). Every callback is in notice_links.NOTICE_SCREENS, opens
# under the message in a panel of its own, and its handler checks the customer and the shop again.
SCREENS: dict[str, tuple[str, str, str]] = {
    "shop": ("shop:list", "🛒 خرید سرویس", GROUP_BUY),
    "trial": ("shop:kind:trial", "🎁 سرویس تست", GROUP_BUY),
    "custom": ("shop:kind:custom", "🧩 سرویس دلخواه", GROUP_BUY),
    "services": ("svc:list", "📦 سرویس‌های من", GROUP_ACCOUNT),
    "wallet": ("nv:w:home", "👛 کیف پول", GROUP_ACCOUNT),
    "topup": ("nv:w:topup", "➕ شارژ کیف پول", GROUP_ACCOUNT),
    "support": ("nv:s:home", "✉️ پشتیبانی", GROUP_ACCOUNT),
    "club": ("nv:loy:home", "⭐ باشگاه مشتریان", GROUP_CLUB),
    "invite": ("nv:loy:ref", "🤝 دعوت دوستان", GROUP_CLUB),
    "wheel": ("nv:loy:wheel", "🎡 گردونهٔ شانس", GROUP_CLUB),
    "guide": ("help:guide", "📘 راهنما", GROUP_HELP),
    "faq": ("help:faq", "❓ سؤالات متداول", GROUP_HELP),
}
PLAN_CALLBACK_PREFIX = "shop:plan:"
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True, slots=True)
class ButtonInput:
    """One button as staff chose it: a target (screen key, ``renew`` or ``link``), label, URL and colour."""

    target: str
    label: str = ""
    url: str = ""
    style: str = ""
    plan: str = ""


@dataclass(frozen=True, slots=True)
class ButtonSpec:
    kind: str
    label: str
    target: str
    style: str = ""


@dataclass(frozen=True, slots=True)
class ButtonRules:
    """What a sender may use: links (owner, or a shop with permission), renewal of this one service, and the
    sender's own sellable plans (a plan button may open only one of these)."""

    allow_links: bool = False
    renew_service_id: int | None = None
    plan_ids: frozenset[int] = frozenset()


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
    if target == TARGET_PLAN:
        plan_id = int(item.plan) if str(item.plan or "").isdigit() else 0
        if plan_id not in rules.plan_ids:
            raise ValueError("پلن دکمه را از پلن‌های فعال همین فروشگاه انتخاب کنید")
        return ButtonSpec(KIND_PLAN, _clean_label(item.label, PLAN_LABEL), str(plan_id), style)
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
    if kind == KIND_PLAN:
        return ButtonInput(TARGET_PLAN, str(item["l"]), "", str(item.get("s") or ""), target)
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


def _screen_data(spec: ButtonSpec) -> str:
    if spec.kind == KIND_SCREEN:
        return SCREENS[spec.target][0]
    if spec.kind == KIND_PLAN:
        return f"{PLAN_CALLBACK_PREFIX}{int(spec.target)}"
    return f"svc:renew:{int(spec.target)}"


def build_keyboard(buttons: list[ButtonSpec]) -> list[list[InlineKeyboardButton]]:
    """Telegram buttons, two to a row."""
    def button(spec: ButtonSpec) -> InlineKeyboardButton:
        extra = {"style": spec.style} if spec.style else {}
        if spec.kind == KIND_URL:
            return InlineKeyboardButton(text=spec.label, url=spec.target, **extra)
        return InlineKeyboardButton(text=spec.label, callback_data=notice_open_data(_screen_data(spec)), **extra)

    keyboard = [button(spec) for spec in buttons]
    return [keyboard[i:i + PER_ROW] for i in range(0, len(keyboard), PER_ROW)]


async def shop_may_link(session: AsyncSession, shop_id: int | None) -> bool:
    """The platform always may; a shop only with the ``broadcast_links`` permission (deny by default)."""
    if shop_id is None:
        return True
    profile = await session.scalar(select(ResellerProfile).where(ResellerProfile.user_id == int(shop_id)))
    return bool(profile is not None and profile.is_active and has_bot_perm(profile, LINKS_PERMISSION))


async def sellable_plan_ids(session: AsyncSession, shop_id: int | None) -> frozenset[int]:
    """Ids of the sender's own active, paid plans: the only plans a button may open."""
    owner = Plan.owner_reseller_id.is_(None) if shop_id is None else Plan.owner_reseller_id == int(shop_id)
    rows = await session.scalars(
        select(Plan.id).where(owner, Plan.is_active.is_(True), Plan.is_trial.is_(False)).limit(PLAN_OPTIONS_LIMIT)
    )
    return frozenset(int(plan_id) for plan_id in rows)


async def sender_rules(session: AsyncSession, shop_id: int | None) -> ButtonRules:
    """What this sender may put under a group message now."""
    return ButtonRules(allow_links=await shop_may_link(session, shop_id), plan_ids=await sellable_plan_ids(session, shop_id))


async def stored_keyboard(session: AsyncSession, stored: str | None, *, shop_id: int | None) -> list[list[InlineKeyboardButton]]:
    """Buttons of a queued broadcast, checked against what the shop may send now (a revoked link or a plan
    that is no longer sold is dropped)."""
    if not stored:
        return []
    return build_keyboard(load_buttons(stored, await sender_rules(session, shop_id)))


def preview_keyboard(stored: str | None) -> list[list[InlineKeyboardButton]]:
    """Buttons this request has just validated, for a preview."""
    items = json.loads(stored) if stored else []
    return build_keyboard([ButtonSpec(str(i["k"]), str(i["l"]), str(i["t"]), str(i.get("s") or "")) for i in items])


async def broadcast_buttons(session: AsyncSession, items: list[ButtonInput], *, shop_id: int | None) -> str | None:
    """Stored buttons for a broadcast of ``shop_id`` (no renewal: a broadcast has no single service)."""
    return dump_buttons(make_buttons(items, await sender_rules(session, shop_id)))


def form_inputs(form: Mapping) -> list[ButtonInput]:
    """The rows of a web form's button list (``btn_target``, ``btn_label``, ``btn_url``, ``btn_style``)."""
    def values(name: str) -> list[str]:
        getter = getattr(form, "getlist", None)
        return [str(v) for v in getter(name)] if getter else []

    names = ("btn_target", "btn_label", "btn_url", "btn_style", "btn_plan")
    targets, labels, urls, styles, plans = (values(n) for n in names)
    pick = lambda seq, i: seq[i] if i < len(seq) else ""  # noqa: E731 - index with a default
    return [
        ButtonInput(t, pick(labels, i), pick(urls, i), pick(styles, i), pick(plans, i)) for i, t in enumerate(targets)
    ]


def target_options(*, renew: bool, links: bool) -> list[tuple[str, str]]:
    """(value, label) of the targets the bot checklist offers (no plan: it needs a second choice)."""
    options = [(key, label) for key, (_cb, label, _group) in SCREENS.items()]
    if renew:
        options.append((TARGET_RENEW, RENEW_LABEL))
    if links:
        options.append((TARGET_LINK, LINK_LABEL))
    return options


def target_groups(*, links: bool, plans: bool, renew: bool = False) -> list[tuple[str, list[tuple[str, str]]]]:
    """The web editor's targets in groups: (group label, [(value, label)])."""
    groups: dict[str, list[tuple[str, str]]] = {}
    for key, (_cb, label, group) in SCREENS.items():
        groups.setdefault(group, []).append((key, label))
    if plans:
        groups[GROUP_BUY].insert(1, (TARGET_PLAN, PLAN_LABEL))
    if renew:
        groups[GROUP_ACCOUNT].insert(1, (TARGET_RENEW, RENEW_LABEL))
    if links:
        groups.setdefault(GROUP_OTHER, []).append((TARGET_LINK, LINK_LABEL))
    return list(groups.items())


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
    buttons = make_buttons(items, ButtonRules(allow_links=rules.allow_links, plan_ids=rules.plan_ids))
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


async def _plan_options(session: AsyncSession, plan_ids: frozenset[int]) -> list[tuple[int, str]]:
    if not plan_ids:
        return []
    rows = await session.execute(select(Plan.id, Plan.name).where(Plan.id.in_(plan_ids)).order_by(Plan.sort_order, Plan.id))
    return [(int(plan_id), str(name)) for plan_id, name in rows.all()]


async def buttons_form_context(session: AsyncSession, shop_id: int | None) -> dict[str, object]:
    """What a web page with a button list needs: targets, plans, colours and this shop's presets."""
    rules = await sender_rules(session, shop_id)
    presets = await list_presets(session, shop_id)
    return {
        "button_target_groups": target_groups(links=rules.allow_links, plans=bool(rules.plan_ids)),
        "button_plans": await _plan_options(session, rules.plan_ids),
        "button_styles": STYLE_OPTIONS,
        "button_max": BUTTONS_MAX,
        "button_presets": [
            {
                "id": p.id,
                "name": p.name,
                "buttons": [
                    {"target": i.target, "label": i.label, "url": i.url, "style": i.style, "plan": i.plan}
                    for i in stored_inputs(p.buttons)
                ],
            }
            for p in presets
        ],
        "button_preset_limit": PRESETS_PER_SHOP,
    }
