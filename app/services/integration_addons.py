"""Explicit shop pack eligibility and immutable technical add-on terms."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

from sqlalchemy import Select, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.integration_addon_models import IntegrationAddonGrant
from app.db.models import Plan, ServiceAddonPack, UserService
from app.services.authz import can_shop
from app.services.integration_authority import IntegrationAuthority
from app.services.integration_observations import MAX_DURATION_SECONDS, MAX_PUBLIC_BYTES, PanelIdentity, observe_service, strict_integer
from app.services.integration_pagination import CursorBinding, IntegrationCursors, Keyset, page_limit
from app.services.integration_policy import IntegrationError
from app.services.integration_request_keys import canonical_digest
from app.services.integration_service_reads import ReadAccess, mapped_customer, read_authority, scoped_services
from app.services.integrations import managed_integration
from app.services.service_addon_targets import calculate_addon_target


@dataclass(frozen=True, slots=True)
class AddonTerms:
    local_id: int
    name: str
    kind: str
    amount_bytes: int | None
    amount_seconds: int | None

    def revision(self) -> str:
        data = asdict(self)
        data.pop("name")
        return canonical_digest(data)


@dataclass(frozen=True, slots=True)
class AddonView:
    id: str
    name: str
    kind: str
    amount_bytes: int | None
    amount_seconds: int | None
    terms_revision: str
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AddonPage:
    items: tuple[AddonView, ...]
    next_cursor: str | None
    has_more: bool


def pack_terms(pack: ServiceAddonPack) -> AddonTerms:
    if pack.kind not in {"volume", "duration"} or isinstance(pack.amount, bool):
        raise IntegrationError("unsupported_terms")
    try:
        amount = Decimal(str(pack.amount))*(1024**3 if pack.kind == "volume" else 86400)
        maximum = MAX_PUBLIC_BYTES if pack.kind == "volume" else MAX_DURATION_SECONDS
        if not amount.is_finite() or not 0 < amount <= maximum or amount != amount.to_integral_value():
            raise ValueError
        if pack.kind == "duration" and amount % 86400:
            raise ValueError
    except (InvalidOperation, ValueError):
        raise IntegrationError("unsupported_terms") from None
    return AddonTerms(pack.id, pack.name, pack.kind, int(amount) if pack.kind == "volume" else None,
        int(amount) if pack.kind == "duration" else None)


def eligible_pack_query(authority: IntegrationAuthority, service: UserService) -> Select[tuple[IntegrationAddonGrant, ServiceAddonPack]]:
    category = select(Plan.category_id).where(Plan.id == service.plan_id).scalar_subquery()
    return select(IntegrationAddonGrant, ServiceAddonPack).join(ServiceAddonPack,
        ServiceAddonPack.id == IntegrationAddonGrant.pack_id).where(IntegrationAddonGrant.integration_id == authority.integration_id,
        IntegrationAddonGrant.enabled.is_(True), ServiceAddonPack.owner_reseller_id == authority.shop.shop_id,
        ServiceAddonPack.is_active.is_(True), or_(ServiceAddonPack.is_general.is_(True),
            (ServiceAddonPack.category_id.is_not(None) & (ServiceAddonPack.category_id == category))))


async def accepted_addon(session: AsyncSession, authority: IntegrationAuthority, service: UserService,
                         public_id: str, revision: str) -> AddonTerms:
    row = (await session.execute(eligible_pack_query(authority, service).where(IntegrationAddonGrant.public_id == public_id))).one_or_none()
    if row is None:
        raise IntegrationError("addon_ineligible")
    terms = pack_terms(row[1])
    if terms.revision() != revision:
        raise IntegrationError("terms_changed")
    return terms


async def set_addon_grants(session: AsyncSession, staff: dict, integration_id: str, pack_ids: frozenset[int]) -> None:
    if not isinstance(pack_ids, frozenset) or len(pack_ids) > 100 or any(type(value) is not int or not 0 < value <= 2**31-1 for value in pack_ids):
        raise IntegrationError("invalid_request")
    integration, owner = await managed_integration(session, staff, integration_id, lock=True)
    if not can_shop(owner.context, "plans") or not can_shop(owner.context, "integration_services_addons"):
        raise IntegrationError("permission_denied")
    packs = list(await session.scalars(select(ServiceAddonPack).where(ServiceAddonPack.id.in_(pack_ids),
        ServiceAddonPack.is_active.is_(True), ServiceAddonPack.owner_reseller_id == owner.shop.shop_id).limit(100)))
    if {pack.id for pack in packs} != pack_ids:
        raise IntegrationError("addon_ineligible")
    for pack in packs:
        pack_terms(pack)
    grants = list(await session.scalars(select(IntegrationAddonGrant).where(IntegrationAddonGrant.integration_id == integration.id,
        IntegrationAddonGrant.pack_id.in_(pack_ids)).limit(100)))
    await session.execute(update(IntegrationAddonGrant).where(IntegrationAddonGrant.integration_id == integration.id).values(enabled=False))
    existing = {grant.pack_id for grant in grants}
    for grant in grants:
        grant.enabled = True
    session.add_all(IntegrationAddonGrant(integration_id=integration.id, pack_id=pack_id, enabled=True) for pack_id in pack_ids-existing)


def addon_target(info: dict, identity: PanelIdentity, terms: AddonTerms, *, now: int) -> dict:
    observation = observe_service(info, identity)
    hold_expire = now+observation.on_hold_seconds if observation.on_hold_seconds else None
    try:
        payload = calculate_addon_target(info, kind=terms.kind, amount_bytes=terms.amount_bytes,
            amount_seconds=terms.amount_seconds, now=now, on_hold_expire_ts=hold_expire)
    except ValueError:
        raise IntegrationError("unsupported_terms") from None
    payload.setdefault("expire", int(observation.expires_at.timestamp()) if observation.expires_at else 0)
    strict_integer(payload.get("data_limit"))
    if payload["expire"]:
        strict_integer(payload["expire"]-now, maximum=MAX_DURATION_SECONDS)
    return payload


async def list_service_addons(session: AsyncSession, access: ReadAccess, customer_id: str, service_id: str,
                              cursors: IntegrationCursors, *, cursor: str = "", limit: int = 25) -> AddonPage:
    limit = page_limit(limit)
    authority = await read_authority(session, access, frozenset({"services:read", "services:addons"}))
    customer = await mapped_customer(session, authority, customer_id)
    row = (await session.execute(scoped_services(authority, customer.id).where(UserService.public_id == service_id))).one_or_none()
    if row is None:
        raise IntegrationError("not_found")
    service, snapshot, _catalog = row
    if snapshot is None or snapshot.state != "ready":
        raise IntegrationError("access_not_ready")
    query = eligible_pack_query(authority, service)
    binding = CursorBinding(authority.integration_id, authority.shop.shop_id, customer_id, "addons", service_id)
    high = await session.scalar(query.with_only_columns(IntegrationAddonGrant.id).order_by(IntegrationAddonGrant.id.desc()).limit(1))
    key = cursors.decode(cursor, binding) if cursor else Keyset((high or 0)+1, high or 0)
    rows = (await session.execute(query.where(IntegrationAddonGrant.id < key.last, IntegrationAddonGrant.id <= key.high)
        .order_by(IntegrationAddonGrant.id.desc()).limit(limit+1))).all()
    items = []
    for grant, pack in rows[:limit]:
        terms = pack_terms(pack)
        if terms.kind == "volume" and snapshot.volume_unlimited or terms.kind == "duration" and snapshot.time_unlimited:
            continue
        items.append(AddonView(grant.public_id, terms.name, terms.kind, terms.amount_bytes, terms.amount_seconds,
            terms.revision(), ("activates_on_hold",) if snapshot.panel_status == "on_hold" else ()))
    more = len(rows) > limit
    next_cursor = cursors.encode(binding, Keyset(rows[limit-1][0].id, key.high)) if more else None
    return AddonPage(tuple(items), next_cursor, more)
