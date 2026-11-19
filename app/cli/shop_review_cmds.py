"""pgclock shop-review: list and assign services whose selling shop is unknown."""

from __future__ import annotations

import asyncio

from app.cli.context import CliContext
from app.cli.output import CliError, header, info, ok, warn


def _parse_shop(raw: str) -> int | None:
    if raw.strip().lower() in {"platform", "main", "0"}:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise CliError("shop must be a shop owner's user id or 'platform'") from exc


async def _list() -> list:
    from app.db.session import SessionLocal
    from app.services.service_shop_review import list_services_under_review

    async with SessionLocal() as session:
        return await list_services_under_review(session)


async def _assign(service_id: int, shop_id: int | None) -> None:
    from app.db.session import SessionLocal
    from app.services.service_shop_review import assign_service_shop

    async with SessionLocal() as session:
        await assign_service_shop(session, service_id, shop_id)


def cmd_shop_review_list(ctx: CliContext) -> int:
    header("services waiting for a shop")
    rows = asyncio.run(_list())
    if not rows:
        ok("nothing to review")
        return 0
    for row in rows:
        shops = ", ".join("platform" if s is None else str(s) for s in row.order_shops) or "none"
        info(
            f"service {row.service_id} ({row.pg_username}) customer {row.customer_id}: "
            f"attributed to {row.attributed_shop or 'platform'}, bought from {shops}"
        )
    warn(f"{len(rows)} service(s) are hidden from every shop until assigned")
    return 0


def cmd_shop_review_assign(ctx: CliContext, *, service_id: int, shop: str) -> int:
    # Deferred like the other service imports here: loading the app is only needed to run a command.
    from app.services.service_shop_review import NOT_UNDER_REVIEW, SHOP_NOT_FOUND

    english = {SHOP_NOT_FOUND: f"shop {shop} not found", NOT_UNDER_REVIEW: f"service {service_id} is not waiting for a shop"}
    try:
        asyncio.run(_assign(service_id, _parse_shop(shop)))
    except ValueError as exc:
        raise CliError(english.get(str(exc), str(exc))) from exc
    ok(f"service {service_id} assigned to {shop}")
    return 0
