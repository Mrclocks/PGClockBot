"""Add, switch, reorder and remove card, link-gateway and crypto destinations from the bots.

The lists are the same ``payment_cards``, ``payment_gateways`` and ``payment_crypto_wallets``
settings the web edits, read and saved through ``payment_destinations`` so the legacy
first-entry keys stay in step. Each change re-reads the list under a per-shop lock and refuses
values the web form would refuse, with a Persian message.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import payment_destinations as pd

Item = dict[str, Any]
Loader = Callable[..., Awaitable[list[Item]]]
Saver = Callable[..., Awaitable[list[Item]]]

TEXT_MAX = 128
NETWORK_MAX = 32
ADDRESS_MAX = 256
_ASSET = re.compile(r"^[A-Za-z0-9]{2,16}$")
_NETWORK = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
_ADDRESS = re.compile(r"^[A-Za-z0-9:_-]{8,256}$")

FULL_LIST = f"حداکثر {pd.MAX_ITEMS} مورد در این فهرست جا می‌شود."
NOT_FOUND = "این مورد دیگر در فهرست نیست."
DUPLICATE = "این مورد قبلاً در فهرست هست."


@dataclass(frozen=True, slots=True)
class Kind:
    code: str
    title: str
    steps: tuple[tuple[str, str], ...]  # (field, prompt)
    load: Loader
    save: Saver
    identity: Callable[[Item], str]


def _card_label(item: Item) -> str:
    label = f"💳 …{str(item.get('number') or '')[-4:]}"
    return f"{label} ({str(item['holder'])[:20]})" if item.get("holder") else label


def _gateway_label(item: Item) -> str:
    return f"🌐 {str(item.get('name') or 'درگاه')[:30]}"


def _wallet_label(item: Item) -> str:
    return f"💎 {item.get('asset') or 'USDT'} {item.get('network') or ''} …{str(item.get('address') or '')[-6:]}"


LABELS: dict[str, Callable[[Item], str]] = {"c": _card_label, "g": _gateway_label, "w": _wallet_label}

KINDS: dict[str, Kind] = {
    "c": Kind(
        "c",
        "کارت‌های کارت به کارت",
        (("number", "شمارهٔ کارت ۱۶ رقمی را بفرستید."), ("holder", "نام صاحب کارت را بفرستید.")),
        pd.get_payment_cards,
        pd.save_payment_cards,
        lambda item: str(item.get("number") or ""),
    ),
    "g": Kind(
        "g",
        "درگاه‌های لینک",
        (
            ("name", "نام درگاه را بفرستید (همان که روی دکمه می‌آید)."),
            ("link", "لینک درگاه را بفرستید؛ با https:// شروع شود. متغیرها: {amount} {order_id} {payment_id}"),
        ),
        pd.get_payment_gateways,
        pd.save_payment_gateways,
        lambda item: str(item.get("link") or item.get("name") or "").lower(),
    ),
    "w": Kind(
        "w",
        "آدرس‌های ولت رمزارز",
        (
            ("asset", "نماد ارز را بفرستید؛ مثلاً USDT"),
            ("network", "شبکه را بفرستید؛ مثلاً TRC20"),
            ("address", "آدرس ولت را بفرستید."),
        ),
        pd.get_payment_crypto_wallets,
        pd.save_payment_crypto_wallets,
        lambda item: str(item.get("address") or "").lower(),
    ),
}

_locks: dict[tuple[str, int | None], asyncio.Lock] = {}


def _lock(kind: str, shop_id: int | None) -> asyncio.Lock:
    return _locks.setdefault((kind, shop_id), asyncio.Lock())


def clean_field(kind: str, field: str, raw: str | None) -> str:
    """One typed value of a new destination; ``ValueError`` carries the Persian message."""
    text = str(raw or "").strip()
    if kind == "c" and field == "number":
        error = pd.validate_card_number(text)
        if error:
            raise ValueError(error)
        return pd.normalize_card_number(text)
    if kind == "c" and field == "holder":
        return text[:TEXT_MAX]
    if kind == "g" and field == "name":
        if not text:
            raise ValueError("نام درگاه را خالی نگذارید.")
        return text[:TEXT_MAX]
    if kind == "g" and field == "link":
        # Placeholders may sit inside the address, but it always starts as https.
        if not text.startswith("https://") or len(text) > 2048 or any(ch.isspace() for ch in text):
            raise ValueError("لینک درگاه باید با https:// شروع شود و فاصله نداشته باشد.")
        return text
    if kind == "w" and field == "asset":
        if not _ASSET.match(text):
            raise ValueError("نماد ارز ۲ تا ۱۶ حرف یا رقم انگلیسی است؛ مثلاً USDT")
        return text.upper()
    if kind == "w" and field == "network":
        if not _NETWORK.match(text):
            raise ValueError("نام شبکه را با حروف انگلیسی بفرستید؛ مثلاً TRC20")
        return text.upper()
    if kind == "w" and field == "address":
        if not _ADDRESS.match(text):
            raise ValueError("آدرس ولت معتبر نیست؛ آن را بدون فاصله کپی کنید.")
        return text
    raise ValueError("این مقدار پذیرفته نمی‌شود.")


async def list_items(session: AsyncSession, kind: str, shop_id: int | None) -> list[Item]:
    return await KINDS[kind].load(session, reseller_id=shop_id)


async def add_item(session: AsyncSession, kind: str, shop_id: int | None, values: dict[str, str]) -> list[Item]:
    """Append a destination built from validated ``values``."""
    spec = KINDS[kind]
    cleaned = {field: clean_field(kind, field, values.get(field)) for field, _prompt in spec.steps}
    async with _lock(kind, shop_id):
        items = await spec.load(session, reseller_id=shop_id)
        if len(items) >= pd.MAX_ITEMS:
            raise ValueError(FULL_LIST)
        new = {**cleaned, "enabled": True, "sort": len(items)}
        if spec.identity(new) in {spec.identity(item) for item in items}:
            raise ValueError(DUPLICATE)
        return await spec.save(session, [*items, new], reseller_id=shop_id)


async def _change(
    session: AsyncSession, kind: str, shop_id: int | None, item_id: str, change: Callable[[list[Item], int], list[Item]]
) -> list[Item]:
    spec = KINDS[kind]
    async with _lock(kind, shop_id):
        items = await spec.load(session, reseller_id=shop_id)
        index = next((i for i, item in enumerate(items) if str(item.get("id")) == item_id), None)
        if index is None:
            raise ValueError(NOT_FOUND)
        updated = change([dict(item) for item in items], index)
        for position, item in enumerate(updated):
            item["sort"] = position
        return await spec.save(session, updated, reseller_id=shop_id)


async def toggle_item(session: AsyncSession, kind: str, shop_id: int | None, item_id: str) -> list[Item]:
    def change(items: list[Item], index: int) -> list[Item]:
        items[index]["enabled"] = not items[index].get("enabled", True)
        return items

    return await _change(session, kind, shop_id, item_id, change)


async def move_item(session: AsyncSession, kind: str, shop_id: int | None, item_id: str, *, up: bool) -> list[Item]:
    def change(items: list[Item], index: int) -> list[Item]:
        target = index - 1 if up else index + 1
        if 0 <= target < len(items):
            items[index], items[target] = items[target], items[index]
        return items

    return await _change(session, kind, shop_id, item_id, change)


async def delete_item(session: AsyncSession, kind: str, shop_id: int | None, item_id: str) -> list[Item]:
    return await _change(session, kind, shop_id, item_id, lambda items, index: items[:index] + items[index + 1 :])


def find_item(items: list[Item], item_id: str) -> Item | None:
    return next((item for item in items if str(item.get("id")) == item_id), None)
