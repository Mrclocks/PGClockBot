"""The platform owner grants one shop permission to several resellers at once.

Only keys on ``BULK_GRANTABLE`` can be granted this way. A reseller whose saved list is
``NULL`` already resolves to the defaults; an empty list is the owner's explicit
"no permissions" and is never widened. Each row is written with a compare-and-set on the
list it was read with, so an edit saved meanwhile is not overwritten.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerProfile
from app.services.miniapp_shop import PERMISSION as MINIAPP_PERMISSION
from app.services.resellers import join_perms, parse_perms

BULK_GRANTABLE = frozenset({MINIAPP_PERMISSION})
# The owner's bulk-bar action on the resellers page.
GRANT_MINIAPP_ACTION = "grant_miniapp"


@dataclass(slots=True)
class GrantResult:
    granted_profile_ids: list[int] = field(default_factory=list)
    already_had: int = 0
    skipped: int = 0


def _granted_list(raw: str | None, key: str) -> str | None:
    """The new saved list, or None when this profile is left as it is."""
    if raw is None:
        return None
    current = parse_perms(raw)
    if not current or key in current:
        return None
    return join_perms([*current, key])


async def grant_shop_permission(
    session: AsyncSession, user_ids: Iterable[int], *, key: str
) -> GrantResult:
    """Add ``key`` to the saved permissions of the given resellers; commits once."""
    if key not in BULK_GRANTABLE:
        raise ValueError("این دسترسی به‌صورت گروهی قابل اعطا نیست")
    ids = sorted({int(i) for i in user_ids})
    result = GrantResult()
    if not ids:
        return result
    rows = (
        await session.execute(
            select(ResellerProfile.id, ResellerProfile.user_id, ResellerProfile.web_permissions)
            .where(ResellerProfile.user_id.in_(ids))
            .limit(len(ids))
        )
    ).all()
    result.skipped += len(ids) - len(rows)
    for profile_id, _user_id, raw in rows:
        new_list = _granted_list(raw, key)
        if new_list is None:
            if raw is None or key in parse_perms(raw):
                result.already_had += 1
            else:
                result.skipped += 1
            continue
        written = await session.execute(
            update(ResellerProfile)
            .where(ResellerProfile.id == profile_id, ResellerProfile.web_permissions == raw)
            .values(web_permissions=new_list, bot_permissions=new_list)
        )
        if written.rowcount == 1:
            result.granted_profile_ids.append(int(profile_id))
        else:
            result.skipped += 1
    await session.commit()
    return result


def grant_summary(result: GrantResult) -> dict[str, str]:
    """Flash text for the resellers page: ``ok`` when anything was granted or already held."""
    parts = []
    if result.granted_profile_ids:
        parts.append(f"{len(result.granted_profile_ids)} نماینده دسترسی مینی‌اپ گرفت")
    if result.already_had:
        parts.append(f"{result.already_had} نماینده از قبل داشت")
    if result.skipped:
        parts.append(f"{result.skipped} نماینده رد شد (دسترسی‌هایش عمداً خالی است یا هم‌زمان تغییر کرد)")
    if not result.granted_profile_ids and not result.already_had:
        return {"err": " · ".join(parts) or "هیچ نماینده‌ای تغییر نکرد"}
    return {"ok": " · ".join(parts)}
