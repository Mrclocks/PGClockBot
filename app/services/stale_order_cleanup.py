"""Turn off legacy soft-cancel (``pending_order_cleanup_*``) without enabling purge.

The finance cleanup tab is tag-based permanent deletion under ``orders_purge``. The
hourly purge job calls ``disable_all_legacy_soft_cancel`` so shops that still have
soft-cancel on stop cancelling. It never writes purge tags or auto-delete — an
operator must opt in. When soft-cancel is turned off, a one-shot panel notice is
set so shop staff see that abandoned orders will no longer be auto-cancelled.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ResellerSetting
from app.services.db_safe import rollback_quiet
from app.services.users import get_all_settings, on, set_settings_bulk

logger = logging.getLogger(__name__)

KEY_ENABLED = "pending_order_cleanup_enabled"
KEY_RETIRED_NOTICE = "cleanup_soft_cancel_retired_notice"
# Keyset page size when scanning shops that still have soft-cancel on.
SHOP_SCAN_PAGE = 2000


async def disable_legacy_soft_cancel(session: AsyncSession, shop_id: int | None) -> bool:
    """Turn off soft-cancel for one shop and raise the one-shot notice. Idempotent."""
    settings = await get_all_settings(session, reseller_id=shop_id)
    if not on(settings.get(KEY_ENABLED, "0")):
        return False
    await set_settings_bulk(
        session,
        {KEY_ENABLED: "0", KEY_RETIRED_NOTICE: "1"},
        reseller_id=shop_id,
    )
    logger.info("disabled legacy soft-cancel shop=%s", shop_id)
    return True


async def _shops_with_soft_cancel_page(session: AsyncSession, *, after_id: int) -> list[int]:
    rows = await session.scalars(
        select(ResellerSetting.reseller_user_id)
        .where(
            ResellerSetting.key == KEY_ENABLED,
            ResellerSetting.value.in_(("1", "true", "yes", "on")),
            ResellerSetting.reseller_user_id > after_id,
        )
        .order_by(ResellerSetting.reseller_user_id)
        .limit(SHOP_SCAN_PAGE)
    )
    return [int(shop_id) for shop_id in rows.all()]


async def disable_all_legacy_soft_cancel(session: AsyncSession) -> int:
    """Platform + every shop still on soft-cancel, keyset-paged; one failure does not stop the rest."""
    done = 0
    try:
        done += int(await disable_legacy_soft_cancel(session, None))
    except Exception:
        logger.exception("soft-cancel disable failed shop=%s", None)
        await rollback_quiet(session)
    after_id = 0
    while True:
        page = await _shops_with_soft_cancel_page(session, after_id=after_id)
        if not page:
            break
        for shop_id in page:
            try:
                done += int(await disable_legacy_soft_cancel(session, shop_id))
            except Exception:
                logger.exception("soft-cancel disable failed shop=%s", shop_id)
                await rollback_quiet(session)
        after_id = page[-1]
        if len(page) < SHOP_SCAN_PAGE:
            break
    return done


async def clear_soft_cancel_retired_notice(session: AsyncSession, shop_id: int | None) -> None:
    """Clear the one-shot banner after staff acknowledge it."""
    await set_settings_bulk(session, {KEY_RETIRED_NOTICE: "0"}, reseller_id=shop_id)


def soft_cancel_retired_notice_on(settings: dict[str, str]) -> bool:
    return on(settings.get(KEY_RETIRED_NOTICE, "0"))
