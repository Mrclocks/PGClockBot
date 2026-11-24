"""Export a shop's settings and catalog as one bundle, and import it back safely."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Plan,
)
from app.services.connection_guides import serialize_submitted_guides

# Credentials never leave the server in a downloadable settings file.
_SECRET_SETTING_KEYS = frozenset({"bot_token", "webhook_secret", "backup_telegram_bot_token", "psp_merchant_id"})


_SECRET_SETTING_SUFFIXES = ("_token", "_secret", "_api_key", "_password")


def _is_secret_setting(key: str) -> bool:
    return key in _SECRET_SETTING_KEYS or key.endswith(_SECRET_SETTING_SUFFIXES)


async def export_shop_bundle(
    session: AsyncSession, *, reseller_id: int | None = None
) -> dict[str, Any]:
    from app.services.users import get_all_settings

    values = await get_all_settings(session, reseller_id=reseller_id)
    # Keep operator-facing keys; drop secrets-ish if any sneak in
    settings = {k: v for k, v in values.items() if not _is_secret_setting(k)}
    pq = select(Plan).order_by(Plan.sort_order.asc(), Plan.id.asc())
    if reseller_id is None:
        pq = pq.where(Plan.owner_reseller_id.is_(None))
    else:
        pq = pq.where(Plan.owner_reseller_id == int(reseller_id))
    plans = [
        {
            "name": p.name,
            "description": p.description,
            "price": p.price,
            "duration_days": p.duration_days,
            "data_limit_gb": p.data_limit_gb,
            "pg_template_id": p.pg_template_id,
            "pg_group_ids": p.pg_group_ids,
            "pg_username_prefix": p.pg_username_prefix,
            "pg_username_suffix": p.pg_username_suffix,
            "pg_username_pattern": p.pg_username_pattern,
            "is_active": bool(p.is_active),
            "is_trial": bool(p.is_trial),
            "sort_order": p.sort_order,
        }
        for p in (await session.execute(pq)).scalars().all()
    ]
    return {
        "format": "pgclock-shop-bundle",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "reseller_id": reseller_id,
        "settings": settings,
        "plans": plans,
    }


def shop_bundle_allowed_setting_keys() -> set[str]:
    """Keys a shop import may write — same surface as shop-settings tabs + domain posts."""
    from app.services.resellers import (
        RESELLER_SETTINGS_TABS,
        SHOP_SETTINGS_DOMAIN_POST_TABS,
    )
    from app.services.users import PLATFORM_ONLY_SETTING_KEYS, keys_for_tab

    keys: set[str] = {"menu_order"}
    for tab, _label in RESELLER_SETTINGS_TABS:
        keys |= keys_for_tab(tab)
    for tab in SHOP_SETTINGS_DOMAIN_POST_TABS:
        keys |= keys_for_tab(tab)
    return keys - PLATFORM_ONLY_SETTING_KEYS


def _parse_import_group_ids(raw: Any) -> list[int]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        out: list[int] = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError) as exc:
                raise ValueError("گروه‌های پلن نامعتبر است") from exc
        return out
    text = str(raw).strip()
    if not text:
        return []
    try:
        return [int(x.strip()) for x in text.split(",") if x.strip()]
    except (TypeError, ValueError) as exc:
        raise ValueError("گروه‌های پلن نامعتبر است") from exc


async def _validate_import_plan(
    session: AsyncSession,
    raw: dict[str, Any],
    *,
    staff: dict | None,
) -> None:
    """Apply the same template / group / quota gates as manual plan create."""
    if not staff:
        return
    from app.services.pg_quota import PgQuotaError, assert_user_plan_within_limits
    from app.services.plans_catalog import (
        plan_groups_denial,
        plan_template_denial,
    )

    tpl_raw = raw.get("pg_template_id")
    tpl_id: int | None = None
    if tpl_raw is not None and str(tpl_raw).strip() != "":
        try:
            tpl_id = int(tpl_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("تمپلیت پلن نامعتبر است") from exc
        template_denial = plan_template_denial(staff, tpl_id)
        if template_denial:
            raise ValueError(template_denial)

    gids = _parse_import_group_ids(raw.get("pg_group_ids"))
    groups_denial = plan_groups_denial(staff, gids) if gids else None
    if groups_denial:
        raise ValueError(groups_denial)

    duration_days = _import_int(raw.get("duration_days"), "مدت", default=30, maximum=MAX_IMPORT_DURATION_DAYS)
    gb = _import_gb(raw.get("data_limit_gb"))
    data_limit = None if gb is None else int(gb * (1024**3))
    try:
        await assert_user_plan_within_limits(
            staff,
            data_limit=data_limit,
            duration_days=duration_days,
            label="پلن ایمپورت",
            session=session,
        )
    except PgQuotaError as exc:
        raise ValueError(exc.message) from exc


async def import_shop_bundle(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    reseller_id: int | None = None,
    replace_plans: bool = False,
    staff: dict | None = None,
) -> dict[str, int]:
    """Apply an exported settings + plans bundle in one commit, or change nothing.

    Every plan is validated before anything is written. With ``replace_plans``,
    plans that were ever sold or referenced are deactivated rather than deleted,
    so existing services and orders keep their plan.
    """
    from app.services.users import DEFAULT_SETTINGS, set_settings_bulk

    if not isinstance(payload, dict) or payload.get("format") != "pgclock-shop-bundle":
        raise ValueError("فرمت فایل نامعتبر است")
    settings = payload.get("settings") or {}
    if not isinstance(settings, dict):
        raise ValueError("settings نامعتبر است")
    plans_in = payload.get("plans") or []
    if not isinstance(plans_in, list):
        raise ValueError("فهرست پلن‌ها نامعتبر است")
    # Known keys only; shop imports are further limited to shop-settings surface.
    allowed_keys = (
        shop_bundle_allowed_setting_keys()
        if reseller_id is not None
        else set(DEFAULT_SETTINGS)
    )
    clean = {
        str(k): "" if v is None else str(v)
        for k, v in settings.items()
        if str(k) in DEFAULT_SETTINGS and str(k) in allowed_keys
    }
    new_plans: list[Plan] = []
    for raw in plans_in:
        if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
            continue
        await _validate_import_plan(session, raw, staff=staff)
        new_plans.append(_plan_from_import(raw, reseller_id=reseller_id))
    if "connection_guides" in clean:
        # Media files live on the exporting server; an import keeps only this shop's own media.
        clean["connection_guides"] = await serialize_submitted_guides(
            session, clean["connection_guides"], reseller_id=reseller_id
        )
    deleted = deactivated = 0
    if replace_plans:
        deleted, deactivated = await _retire_catalog_plans(session, reseller_id=reseller_id)
    session.add_all(new_plans)
    if clean:
        # Commits the plan changes above in the same transaction.
        await set_settings_bulk(session, clean, reseller_id=reseller_id)
    else:
        await session.commit()
    return {"settings": len(clean), "plans": len(new_plans), "deleted": deleted, "deactivated": deactivated}


MAX_IMPORT_INT = 2_147_483_647


MAX_IMPORT_DURATION_DAYS = 36_500


def _import_int(raw: Any, label: str, *, default: int, maximum: int = MAX_IMPORT_INT) -> int:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    if isinstance(raw, bool):
        raise ValueError(f"{label} پلن ایمپورت نامعتبر است")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{label} پلن ایمپورت نامعتبر است") from None
    if not number.is_integer() or number < 0 or number > maximum:
        raise ValueError(f"{label} پلن ایمپورت نامعتبر است")
    return int(number)


def _import_gb(raw: Any) -> float | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    if isinstance(raw, bool):
        raise ValueError("حجم پلن ایمپورت نامعتبر است")
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ValueError("حجم پلن ایمپورت نامعتبر است") from None
    if number != number or number < 0 or number > 1_000_000:
        raise ValueError("حجم پلن ایمپورت نامعتبر است")
    return number


def _import_flag(raw: Any, *, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off", ""}


def _plan_from_import(raw: dict[str, Any], *, reseller_id: int | None) -> Plan:
    gids = _parse_import_group_ids(raw.get("pg_group_ids"))
    tpl_raw = raw.get("pg_template_id")
    tpl_id = None if tpl_raw is None or str(tpl_raw).strip() == "" else _import_int(tpl_raw, "تمپلیت", default=0)

    def text(key: str, limit: int) -> str | None:
        value = raw.get(key)
        return None if value is None else str(value)[:limit] or None

    return Plan(
        name=str(raw.get("name")).strip()[:128],
        description=text("description", 2000),
        price=_import_int(raw.get("price"), "قیمت", default=0),
        duration_days=_import_int(raw.get("duration_days"), "مدت", default=30, maximum=MAX_IMPORT_DURATION_DAYS),
        data_limit_gb=_import_gb(raw.get("data_limit_gb")),
        pg_template_id=tpl_id,
        pg_group_ids=",".join(str(g) for g in gids) or None,
        pg_username_prefix=text("pg_username_prefix", 64),
        pg_username_suffix=text("pg_username_suffix", 64),
        pg_username_pattern=text("pg_username_pattern", 255),
        owner_reseller_id=int(reseller_id) if reseller_id else None,
        is_active=_import_flag(raw.get("is_active"), default=True),
        is_trial=False,
        sort_order=_import_int(raw.get("sort_order"), "ترتیب", default=0),
    )


async def _retire_catalog_plans(session: AsyncSession, *, reseller_id: int | None) -> tuple[int, int]:
    """Delete unused catalog plans and deactivate referenced ones; returns (deleted, deactivated)."""
    scope = Plan.owner_reseller_id.is_(None) if reseller_id is None else Plan.owner_reseller_id == int(reseller_id)
    plans = (await session.execute(select(Plan).where(scope, Plan.is_trial.is_(False)))).scalars().all()
    if not plans:
        return 0, 0
    referenced = await _referenced_plan_ids(session, [plan.id for plan in plans])
    deleted = deactivated = 0
    for plan in plans:
        if plan.id in referenced:
            plan.is_active = False
            deactivated += 1
        else:
            await session.delete(plan)
            deleted += 1
    await session.flush()
    return deleted, deactivated


async def _referenced_plan_ids(session: AsyncSession, plan_ids: list[int]) -> set[int]:
    """Plans any table points to (orders, services, automations, …), one query per reference."""
    from app.db import Base

    found: set[int] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if any(fk.column.table.name == Plan.__tablename__ for fk in column.foreign_keys):
                rows = await session.execute(select(column).where(column.in_(plan_ids)).distinct())
                found |= {int(value) for (value,) in rows if value is not None}
    return found


def shop_bundle_to_json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
