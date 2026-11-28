"""Which services the panel removed while usable, and which ones drop out of the customer's list.

PasarGuard deletes users whose time or volume ran out on its own schedule. Once the presence scan
has confirmed such a removal (``panel_missing_at``), the row is only a leftover for the customer:
after a short grace period it leaves «سرویس‌های من». A service that still had time and volume
when it disappeared stays listed, because staff may owe the customer a refund for it.

Nothing here deletes or changes a row; the rules are read-only filters, and a service the panel
reports again clears its mark and comes back.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import and_, or_
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import UserService

# Long enough for the customer to open the service once and read «حذف‌شده از پنل».
REMOVED_SERVICE_LISTED_FOR = timedelta(days=1)


def usable_when_removed_clause() -> ColumnElement[bool]:
    """By its last cached quota the service still had volume and time when the panel removed it.

    No cached expiry counts as usable (unlimited, or never read), so an unknown never hides a service.
    """
    return and_(
        UserService.volume_exhausted_at.is_(None),
        or_(UserService.quota_expire_at.is_(None), UserService.quota_expire_at > UserService.panel_missing_at),
    )


def hidden_from_customer_clause(now: datetime) -> ColumnElement[bool]:
    """The panel removed the service after it ran out, at least ``REMOVED_SERVICE_LISTED_FOR`` ago."""
    return and_(
        UserService.panel_missing_at.is_not(None),
        UserService.panel_missing_at <= now - REMOVED_SERVICE_LISTED_FOR,
        ~usable_when_removed_clause(),
    )
