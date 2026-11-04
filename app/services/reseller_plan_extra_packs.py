"""Per-plan quick-buy packs for reseller extra volume and user slots.

These are the stairs under ``allow_buy_extra`` (not catalog addon ResellerPlans).
Each row belongs to exactly one ``ResellerPlan``; purchase validation must use that
plan's rows only. Unit price stays on the parent plan.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerPlan, ResellerPlanExtraPack
from app.services.button_styles import normalize_style, resolve_plan_button_style

KIND_GB = "gb"
KIND_USERS = "users"
VALID_KINDS = frozenset({KIND_GB, KIND_USERS})

# Historic hardcoded stairs — used as seed / legacy fallback when a plan has no rows yet.
DEFAULT_EXTRA_GB_AMOUNTS: tuple[int, ...] = (1, 5, 10, 50)
DEFAULT_EXTRA_USER_AMOUNTS: tuple[int, ...] = (1, 5, 10, 20)

MAX_PACK_AMOUNT_GB = 100_000
MAX_PACK_AMOUNT_USERS = 100_000
MAX_PACKS_PER_KIND = 20

_DEFAULTS: dict[str, tuple[int, ...]] = {
    KIND_GB: DEFAULT_EXTRA_GB_AMOUNTS,
    KIND_USERS: DEFAULT_EXTRA_USER_AMOUNTS,
}
_MAX_AMOUNT: dict[str, int] = {
    KIND_GB: MAX_PACK_AMOUNT_GB,
    KIND_USERS: MAX_PACK_AMOUNT_USERS,
}


def default_amounts(kind: str) -> tuple[int, ...]:
    if kind not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    return _DEFAULTS[kind]


def parse_pack_kind(raw: object) -> str:
    kind = str(raw or "").strip().lower()
    if kind not in VALID_KINDS:
        raise ValueError("نوع بسته نامعتبر است")
    return kind


def parse_pack_amount(kind: str, raw: object) -> int:
    """Positive int in range for ``kind``; rejects bool and non-integers."""
    parse_pack_kind(kind)
    if isinstance(raw, bool):
        raise ValueError("مقدار بسته نامعتبر است")
    if isinstance(raw, int):
        amount = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        amount = int(raw.strip())
    else:
        raise ValueError("مقدار بسته نامعتبر است")
    if amount <= 0 or amount > _MAX_AMOUNT[kind]:
        raise ValueError("مقدار بسته خارج از محدوده است")
    return amount


def parse_pack_button_style(raw: object | None) -> str | None:
    """None = inherit; '' / primary / success / danger otherwise."""
    if raw is None:
        return None
    text = str(raw).strip().lower()
    if text in {"inherit", "__inherit__"}:
        return None
    if text == "":
        return ""
    return normalize_style(text)


def normalize_amount_list(kind: str, amounts: Iterable[object]) -> list[int]:
    """Dedupe preserving order; enforce count and range limits."""
    parse_pack_kind(kind)
    out: list[int] = []
    seen: set[int] = set()
    for raw in amounts:
        amount = parse_pack_amount(kind, raw)
        if amount in seen:
            continue
        seen.add(amount)
        out.append(amount)
        if len(out) > MAX_PACKS_PER_KIND:
            raise ValueError(f"حداکثر {MAX_PACKS_PER_KIND} بسته برای هر نوع مجاز است")
    return out


async def has_any_pack(session: AsyncSession, plan_id: int) -> bool:
    pid = int(plan_id)
    found = await session.scalar(
        select(ResellerPlanExtraPack.id)
        .where(ResellerPlanExtraPack.plan_id == pid)
        .limit(1)
    )
    return found is not None


async def list_packs(
    session: AsyncSession,
    plan_id: int,
    *,
    kind: str | None = None,
    active_only: bool = False,
) -> list[ResellerPlanExtraPack]:
    """Packs for one plan only (never crosses plan_id)."""
    pid = int(plan_id)
    q = select(ResellerPlanExtraPack).where(ResellerPlanExtraPack.plan_id == pid)
    if kind is not None:
        q = q.where(ResellerPlanExtraPack.kind == parse_pack_kind(kind))
    if active_only:
        q = q.where(ResellerPlanExtraPack.is_active.is_(True))
    q = q.order_by(
        ResellerPlanExtraPack.kind,
        ResellerPlanExtraPack.sort_order,
        ResellerPlanExtraPack.amount,
        ResellerPlanExtraPack.id,
    )
    return list(await session.scalars(q))


async def get_pack_for_plan(
    session: AsyncSession, *, plan_id: int, pack_id: int
) -> ResellerPlanExtraPack | None:
    """Load a pack only when it belongs to ``plan_id``."""
    pack = await session.get(ResellerPlanExtraPack, int(pack_id))
    if pack is None or int(pack.plan_id) != int(plan_id):
        return None
    return pack


async def active_amounts_for_plan(
    session: AsyncSession, plan_id: int, kind: str
) -> frozenset[int]:
    """Amounts a buyer may pick for this plan+kind.

    - Active rows for the kind → those amounts only.
    - No rows on the plan at all → legacy defaults (pre-seed / unmigrated).
    - Plan has packs but none active for this kind → empty (explicitly cleared).
    """
    parse_pack_kind(kind)
    packs = await list_packs(session, plan_id, kind=kind, active_only=True)
    if packs:
        return frozenset(int(p.amount) for p in packs)
    if await has_any_pack(session, plan_id):
        return frozenset()
    return frozenset(default_amounts(kind))


async def buyable_packs_for_plan(
    session: AsyncSession, plan_id: int, kind: str
) -> list[ResellerPlanExtraPack]:
    """Active pack rows for UI; empty when the kind was cleared on a configured plan."""
    parse_pack_kind(kind)
    packs = await list_packs(session, plan_id, kind=kind, active_only=True)
    if packs or await has_any_pack(session, plan_id):
        return packs
    return []


async def ensure_default_packs(session: AsyncSession, plan: ResellerPlan) -> list[ResellerPlanExtraPack]:
    """Seed default stairs when the plan has no pack rows yet. Idempotent.

    Concurrent callers may race the unique (plan_id, kind, amount) constraint.
    Inserts run inside a savepoint so a conflict rolls back only this seed, not
    the caller's pending plan/form changes on the same session.
    """
    from sqlalchemy.exc import IntegrityError

    if plan.id is None:
        await session.flush()
    if await has_any_pack(session, int(plan.id)):
        return await list_packs(session, int(plan.id))
    created: list[ResellerPlanExtraPack] = []
    try:
        async with session.begin_nested():
            for kind, amounts in _DEFAULTS.items():
                for index, amount in enumerate(amounts):
                    row = ResellerPlanExtraPack(
                        plan_id=int(plan.id),
                        kind=kind,
                        amount=int(amount),
                        button_style=None,
                        sort_order=index,
                        is_active=True,
                    )
                    session.add(row)
                    created.append(row)
            await session.flush()
    except IntegrityError:
        return await list_packs(session, int(plan.id))
    return created


async def replace_kind_packs(
    session: AsyncSession,
    plan: ResellerPlan,
    kind: str,
    amounts: Sequence[object],
    *,
    button_styles: Sequence[object | None] | None = None,
) -> list[ResellerPlanExtraPack]:
    """Replace all packs of ``kind`` for ``plan`` with the given ordered amounts."""
    kind_n = parse_pack_kind(kind)
    normalized = normalize_amount_list(kind_n, amounts)
    styles: list[str | None]
    if button_styles is None:
        styles = [None] * len(normalized)
    else:
        if len(button_styles) != len(normalized):
            raise ValueError("تعداد رنگ دکمه با بسته‌ها یکی نیست")
        styles = [parse_pack_button_style(s) for s in button_styles]

    existing = await list_packs(session, int(plan.id), kind=kind_n, active_only=False)
    for row in existing:
        await session.delete(row)
    await session.flush()

    created: list[ResellerPlanExtraPack] = []
    for index, amount in enumerate(normalized):
        row = ResellerPlanExtraPack(
            plan_id=int(plan.id),
            kind=kind_n,
            amount=amount,
            button_style=styles[index],
            sort_order=index,
            is_active=True,
        )
        session.add(row)
        created.append(row)
    await session.flush()
    return created


async def pack_count(session: AsyncSession, plan_id: int) -> int:
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ResellerPlanExtraPack)
            .where(ResellerPlanExtraPack.plan_id == int(plan_id))
        )
        or 0
    )


def resolve_extra_pack_button_style(
    pack_button_style: str | None,
    plan: object | None,
    *,
    ui: dict | None = None,
) -> str | None:
    """Telegram style for one extra-pack button: pack override, else plan resolution."""
    if pack_button_style is not None:
        return normalize_style(pack_button_style) or None
    return resolve_plan_button_style(
        ui, plan, audience="resellers", kind="subscription"
    )


def telegram_style_kwargs(style: str | None) -> dict[str, str]:
    return {"style": style} if style else {}


def stair_purchase_button_text(*, kind: str, amount: int, price_text: str) -> str:
    """Plain Telegram caption for a unit stair: emoji, amount+unit, price."""
    kind_n = parse_pack_kind(kind)
    emoji = "📦" if kind_n == KIND_GB else "👥"
    unit = "گیگ" if kind_n == KIND_GB else "کاربر"
    return f"{emoji} {int(amount)} {unit} — {price_text}"


def _form_getlist(form: object, key: str) -> list[str]:
    if hasattr(form, "getlist"):
        return [str(v) for v in form.getlist(key)]  # type: ignore[attr-defined]
    raw = form.get(key) if hasattr(form, "get") else None  # type: ignore[attr-defined]
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(v) for v in raw]
    return [str(raw)]


def packs_payload_from_form(form: object, kind: str) -> tuple[list[int], list[str | None]]:
    """Read ordered amounts + styles for one kind from a multipart/form body.

    Amount and style are paired by row index before dedupe, so a duplicate amount
    keeps the style from the first occurrence of that amount.
    """
    kind_n = parse_pack_kind(kind)
    amount_key = "extra_gb_amount" if kind_n == KIND_GB else "extra_user_amount"
    style_key = "extra_gb_style" if kind_n == KIND_GB else "extra_user_style"
    raw_amounts = _form_getlist(form, amount_key)
    raw_styles = _form_getlist(form, style_key)
    if not raw_amounts:
        return [], []
    amounts: list[int] = []
    styles: list[str | None] = []
    seen: set[int] = set()
    for index, raw_amount in enumerate(raw_amounts):
        amount = parse_pack_amount(kind_n, raw_amount)
        if amount in seen:
            continue
        if len(amounts) >= MAX_PACKS_PER_KIND:
            raise ValueError(f"حداکثر {MAX_PACKS_PER_KIND} بسته برای هر نوع مجاز است")
        seen.add(amount)
        amounts.append(amount)
        raw_style = raw_styles[index] if index < len(raw_styles) else "inherit"
        styles.append(parse_pack_button_style(raw_style))
    return amounts, styles


async def apply_extra_packs_from_form(
    session: AsyncSession, plan: ResellerPlan, form: object
) -> None:
    """Persist pack editors for a plan that allows buy-extra; otherwise leave rows alone."""
    if not bool(getattr(plan, "allow_buy_extra", False)):
        return
    has_fields = bool(_form_getlist(form, "extra_gb_amount") or _form_getlist(form, "extra_user_amount"))
    if not has_fields:
        await ensure_default_packs(session, plan)
        return
    for kind in (KIND_GB, KIND_USERS):
        amounts, styles = packs_payload_from_form(form, kind)
        await replace_kind_packs(session, plan, kind, amounts, button_styles=styles)
