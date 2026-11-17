"""Pick the PasarGuard role a new non-owner admin is created with.

Recent PasarGuard versions reject ``POST /api/admin`` without ``role_id``. Every
path that creates an admin for a shop or a principal resolves the role here, so
an explicit role is validated the same way and a missing one is filled in only
when the panel leaves exactly one safe choice.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

NO_ROLE_DEFINED = (
    "پاسارگارد برای ساخت ادمین «نقش» می‌خواهد ولی هیچ نقش غیرمالکی در پنل تعریف نشده است. "
    "راه حل: در پاسارگارد یک نقش برای نماینده‌ها بسازید و دوباره تلاش کنید."
)
ROLE_AMBIGUOUS = (
    "پاسارگارد برای ساخت ادمین «نقش» می‌خواهد و چند نقش در پنل هست. "
    "راه حل: نماینده را از صفحهٔ «نمایندگان» با پلنی بسازید که نقش پاسارگارد دارد، "
    "یا برای پلن نمایندگی یک نقش پاسارگارد انتخاب کنید."
)
ROLE_NOT_FOUND = "نقش پاسارگارد انتخاب‌شده در پنل یافت نشد — نقش پلن را دوباره انتخاب کنید"
OWNER_ROLE_FORBIDDEN = "نمی‌توان نقش مالک پاسارگارد را به ادمین جدید داد"
ROLE_LIST_UNREADABLE = (
    "پاسارگارد برای ساخت ادمین «نقش» می‌خواهد ولی ربات نتوانست فهرست نقش‌ها را بخواند "
    "(معمولاً کلید API یا ادمین متصل به ربات اجازهٔ خواندن نقش‌ها را ندارد). "
    "راه حل: پلن نمایندگی‌ای انتخاب کنید که نقش پاسارگارد دارد، "
    "یا به اتصال پاسارگارد ربات دسترسی خواندن نقش‌ها بدهید."
)
VALIDATION_STATUS = 422


class AdminRoleError(ValueError):
    """The panel offers no role this admin may safely be created with."""


class _RoleSource(Protocol):
    async def get_admin_roles(self) -> list[dict]: ...


def _role_id(role: Any) -> int | None:
    if not isinstance(role, dict) or isinstance(role.get("id"), bool):
        return None
    try:
        rid = int(role.get("id"))
    except (TypeError, ValueError):
        return None
    return rid if rid > 0 else None


async def resolve_new_admin_role_id(client: _RoleSource, requested: int | None) -> int | None:
    """Role id for a new non-owner admin, or ``None`` on a panel without roles.

    ``requested`` must name an existing non-owner role. Without one, the only
    non-owner role on the panel is used; several roles raise, because guessing
    could hand a shop more panel access than intended.
    """
    try:
        roles = await client.get_admin_roles()
    except Exception:
        # Unreadable role list: fall through to the legacy payload; the panel answers for itself.
        logger.warning("PasarGuard role list unavailable for a new admin", exc_info=True)
        roles = []
    if not isinstance(roles, list):
        roles = []
    by_id = {rid: role for role in roles if (rid := _role_id(role)) is not None}
    if requested is not None:
        chosen = by_id.get(int(requested))
        if chosen is None:
            # Panels without the roles API return no list; keep the operator's choice.
            if not by_id:
                return int(requested)
            raise AdminRoleError(ROLE_NOT_FOUND)
        if chosen.get("is_owner"):
            raise AdminRoleError(OWNER_ROLE_FORBIDDEN)
        return int(requested)
    if not by_id:
        # Older PasarGuard without roles: the legacy ``is_sudo=false`` payload still works.
        return None
    candidates = sorted(rid for rid, role in by_id.items() if not role.get("is_owner"))
    if not candidates:
        raise AdminRoleError(NO_ROLE_DEFINED)
    if len(candidates) > 1:
        raise AdminRoleError(ROLE_AMBIGUOUS)
    logger.info("new PasarGuard admin gets the only non-owner role id=%s", candidates[0])
    return candidates[0]


def _names_role_field(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    loc = item.get("loc")
    return isinstance(loc, (list, tuple)) and "role_id" in loc


def is_missing_role_rejection(exc: BaseException) -> bool:
    """True when the panel refused a create-admin call only for lacking ``role_id``.

    A validation refusal means no admin was created, so the caller may report it
    plainly instead of treating the outcome as unknown.
    """
    if getattr(exc, "status_code", None) != VALIDATION_STATUS:
        return False
    body = getattr(exc, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    return isinstance(detail, list) and any(_names_role_field(item) for item in detail)


def apply_admin_role(payload: dict[str, Any], role_id: int | None) -> None:
    """Set the role on a create-admin payload; the legacy flag only without roles."""
    if role_id is not None:
        payload["role_id"] = int(role_id)
        payload.pop("is_sudo", None)
    else:
        payload.pop("role_id", None)
        payload["is_sudo"] = False
