"""Additional bulk table ops — plans, resellers, PG, broadcast, panel tickets."""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BotUser, ChargeCode, ResellerPlan
from app.services.table_bulk import parse_bulk_ids

logger = logging.getLogger(__name__)


async def _staff_pg_client(session: AsyncSession, staff: dict):
    from app.services.pasarguard import (
        PasarGuardError,
        get_pg,
        get_pg_for_principal,
        get_pg_for_reseller,
        get_pg_for_staff,
    )
    from app.services.shop_scope import is_platform_admin, shop_owner_id

    if is_platform_admin(staff):
        return get_pg(), bool(staff.get("pg_is_owner"))
    rid = shop_owner_id(staff)
    if rid:
        return await get_pg_for_reseller(session, int(rid)), False
    if staff.get("role") == "pg_staff":
        return await get_pg_for_staff(
            session,
            pg_username=staff.get("pg_admin_username"),
            staff_id=staff.get("pg_staff_id"),
        ), False
    if staff.get("role") == "principal":
        return await get_pg_for_principal(
            session,
            principal_id=staff.get("org_principal_id"),
            pg_username=staff.get("pg_admin_username"),
        ), False
    raise PasarGuardError("تغییر در پاسارگارد بدون اعتبارنامه اختصاصی ممکن نیست.")


async def bulk_reseller_app_action(
    session: AsyncSession,
    staff: dict,
    app_ids: list[int],
    action: str,
) -> tuple[int, int]:
    from app.services.resellers import (
        approve_application,
        get_application,
        get_reseller_panel_base_url,
        reject_application,
    )

    ids = parse_bulk_ids(app_ids)
    ok = fail = 0
    for aid in ids:
        app = await get_application(session, aid)
        if not app:
            fail += 1
            continue
        try:
            if action == "approve":
                if app.status != "awaiting_approval":
                    fail += 1
                    continue
                await approve_application(
                    session,
                    app,
                    reviewer_tg=0,
                    panel_base_url=await get_reseller_panel_base_url(session),
                )
            elif action == "reject":
                if app.status not in {"awaiting_approval", "pending_payment"}:
                    fail += 1
                    continue
                await reject_application(session, app, reviewer_tg=0)
            else:
                fail += 1
                continue
            ok += 1
        except Exception:
            logger.debug("bulk reseller app %s failed id=%s", action, aid, exc_info=True)
            fail += 1
    return ok, fail


async def bulk_revoke_resellers(
    session: AsyncSession,
    staff: dict,
    user_ids: list[int],
    *,
    reason: str,
) -> tuple[int, int]:
    from app.services.notifications import actor_label_from_staff, notify_reseller_revoked
    from app.services.resellers import revoke_reseller

    reason = (reason or "").strip()
    if len(reason) < 3:
        return 0, len(parse_bulk_ids(user_ids))

    ids = parse_bulk_ids(user_ids)
    ok = fail = 0
    actor = actor_label_from_staff(staff)
    for uid in ids:
        try:
            info = await revoke_reseller(
                session, uid, delete_pg_admin=True, reason=reason
            )
            user = await session.get(BotUser, uid)
            try:
                if user:
                    await notify_reseller_revoked(
                        int(info["telegram_id"]),
                        reason,
                        session=session,
                        user=user,
                        actor=actor,
                    )
                else:
                    await notify_reseller_revoked(int(info["telegram_id"]), reason)
            except Exception:
                logger.debug("bulk revoke notify failed uid=%s", uid, exc_info=True)
            ok += 1
        except Exception:
            logger.debug("bulk revoke reseller failed uid=%s", uid, exc_info=True)
            fail += 1
    return ok, fail


async def bulk_delete_reseller_users(
    session: AsyncSession,
    staff: dict,
    user_ids: list[int],
    *,
    reason: str,
) -> tuple[int, int]:
    from app.services.notifications import actor_label_from_staff, notify_account_edit
    from app.services.users import delete_bot_user

    reason = (reason or "").strip()
    if len(reason) < 3:
        return 0, len(parse_bulk_ids(user_ids))

    ids = parse_bulk_ids(user_ids)
    ok = fail = 0
    actor = actor_label_from_staff(staff)
    actor_id = staff.get("bot_user_id")
    for uid in ids:
        user = await session.get(BotUser, uid)
        if not user:
            fail += 1
            continue
        try:
            await notify_account_edit(
                session,
                user=user,
                event="user_delete",
                reason=reason,
                actor=actor,
            )
            await delete_bot_user(session, uid, actor_user_id=actor_id)
            ok += 1
        except Exception:
            logger.debug("bulk delete reseller user failed uid=%s", uid, exc_info=True)
            fail += 1
            try:
                await session.rollback()
            except Exception:
                pass
    return ok, fail


async def bulk_shop_plan_action(
    session: AsyncSession,
    staff: dict,
    plan_ids: list[int],
    action: str,
) -> tuple[int, int, str | None]:
    from app.services.plans_catalog import (
        PlanDeleteBlocked,
        delete_shop_plan,
        get_owned_plan,
    )

    ids = parse_bulk_ids(plan_ids)
    ok = fail = 0
    first_err: str | None = None
    for pid in ids:
        plan = await get_owned_plan(session, pid, staff)
        if not plan or getattr(plan, "is_trial", False):
            fail += 1
            continue
        try:
            async with session.begin_nested():
                if action == "toggle":
                    plan.is_active = not plan.is_active
                    await session.flush()
                    ok += 1
                elif action == "delete":
                    await delete_shop_plan(session, plan)
                    ok += 1
                else:
                    fail += 1
        except PlanDeleteBlocked as e:
            fail += 1
            if first_err is None:
                first_err = e.message
        except Exception:
            logger.debug("bulk shop plan %s failed id=%s", action, pid, exc_info=True)
            fail += 1
    if ok:
        await session.commit()
    return ok, fail, first_err


async def bulk_reseller_plan_action(
    session: AsyncSession,
    staff: dict,
    plan_ids: list[int],
    action: str,
) -> tuple[int, int, str | None]:
    from app.services.billing import sync_plan_billing_rate
    from app.services.plans_catalog import PlanDeleteBlocked, delete_reseller_plan

    ids = parse_bulk_ids(plan_ids)
    ok = fail = 0
    first_err: str | None = None
    for pid in ids:
        plan = await session.get(ResellerPlan, pid)
        if not plan:
            fail += 1
            continue
        try:
            async with session.begin_nested():
                if action == "toggle":
                    plan.is_active = not plan.is_active
                    await sync_plan_billing_rate(session, plan)
                    await session.flush()
                    ok += 1
                elif action == "delete":
                    await delete_reseller_plan(session, plan)
                    ok += 1
                else:
                    fail += 1
        except PlanDeleteBlocked as e:
            fail += 1
            if first_err is None:
                first_err = e.message
        except Exception:
            logger.debug("bulk reseller plan %s failed id=%s", action, pid, exc_info=True)
            fail += 1
    if ok:
        await session.commit()
    return ok, fail, first_err


async def bulk_gift_code_toggle(
    session: AsyncSession,
    staff: dict,
    code_ids: list[int],
) -> tuple[int, int]:
    from app.services.authz import authz_from_staff, can_shop
    from app.services.shop_scope import is_platform_admin, shop_owner_id

    admin_scope = is_platform_admin(staff)
    if not (
        admin_scope
        or can_shop(authz_from_staff(staff), "orders")
        or can_shop(authz_from_staff(staff), "plans")
    ):
        return 0, len(parse_bulk_ids(code_ids))
    rid = None if admin_scope else shop_owner_id(staff)
    if not admin_scope and rid is None:
        return 0, len(parse_bulk_ids(code_ids))

    ids = parse_bulk_ids(code_ids)
    ok = fail = 0
    for cid in ids:
        row = await session.get(ChargeCode, cid)
        if not row:
            fail += 1
            continue
        if rid is None and row.reseller_id is not None:
            fail += 1
            continue
        if rid is not None and int(row.reseller_id or 0) != int(rid):
            fail += 1
            continue
        row.is_active = not bool(row.is_active)
        ok += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_close_panel_tickets(
    session: AsyncSession,
    staff: dict,
    ticket_ids: list[int],
) -> tuple[int, int]:
    from app.services.panel_tickets import (
        PanelTicketStatus,
        can_access_panel_tickets,
        get_ticket,
        set_status,
    )

    if not can_access_panel_tickets(staff):
        return 0, len(parse_bulk_ids(ticket_ids))

    ids = parse_bulk_ids(ticket_ids)
    ok = fail = 0
    for tid in ids:
        try:
            ticket = await get_ticket(session, staff, tid, load_messages=False)
            if not ticket or ticket.status == PanelTicketStatus.CLOSED.value:
                fail += 1
                continue
            await set_status(session, staff, tid, status="closed")
            ok += 1
        except Exception:
            logger.debug("bulk close panel ticket failed id=%s", tid, exc_info=True)
            fail += 1
    if ok:
        await session.commit()
    return ok, fail


async def bulk_broadcast_delete(
    session: AsyncSession,
    log_ids: list[int],
) -> tuple[int, int]:
    from app.services.broadcast import delete_broadcast_log

    ids = parse_bulk_ids(log_ids)
    ok = fail = 0
    for lid in ids:
        try:
            if await delete_broadcast_log(session, lid):
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    return ok, fail


async def bulk_pg_user_action(
    session: AsyncSession,
    staff: dict,
    user_ids: list[int],
    action: str,
) -> tuple[int, int]:
    from app.services.pg_access import staff_user_actions
    from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users
    from app.services.pg_user_scope import (
        is_full_pg_owner,
        pg_user_in_staff_scope,
        staff_pg_username,
    )
    from app.services.pasarguard import get_pg

    acts = staff_user_actions(staff)
    need = {
        "disable": "disable",
        "enable": "enable",
        "reset": "reset_usage",
        "revoke": "revoke_sub",
        "delete": "delete",
    }.get(action)
    if not need or not acts.get(need):
        return 0, len(parse_bulk_ids(user_ids))

    ids = parse_bulk_ids(user_ids)
    ok = fail = 0

    async def _load_user(uid: int) -> dict | None:
        if is_full_pg_owner(staff):
            raw = await get_pg().get_user_by_id(uid)
            return raw if isinstance(raw, dict) else None
        if not staff_pg_username(staff):
            return None
        try:
            pg, _ = await _staff_pg_client(session, staff)
            raw = await pg.get_user_by_id(uid)
            info = raw if isinstance(raw, dict) else None
        except Exception:
            return None
        if info is None:
            return None
        if not pg_user_in_staff_scope(info, staff):
            return None
        return info

    try:
        await assert_can_mutate_owned_users(staff, session=session)
    except PgQuotaError:
        return 0, len(ids)

    for uid in ids:
        try:
            info = await _load_user(uid)
            if info is None:
                fail += 1
                continue
            disabled = bool(
                info.get("status") == "disabled"
                or info.get("is_disabled")
                or info.get("disabled")
            )
            if action == "disable" and disabled:
                fail += 1
                continue
            if action == "enable" and not disabled:
                fail += 1
                continue
            pg, _ = await _staff_pg_client(session, staff)
            if action == "disable":
                await pg.set_disabled_by_id(uid, True)
            elif action == "enable":
                await pg.set_disabled_by_id(uid, False)
            elif action == "reset":
                await pg.reset_user_by_id(uid)
            elif action == "revoke":
                await pg.revoke_sub_by_id(uid)
            elif action == "delete":
                await pg.delete_user_by_id(uid)
                from app.services.bot_user_admin import detach_local_services_for_pg_user

                await detach_local_services_for_pg_user(session, uid, commit=False)
            else:
                fail += 1
                continue
            ok += 1
        except Exception:
            logger.debug("bulk pg user %s failed id=%s", action, uid, exc_info=True)
            fail += 1
    return ok, fail


async def bulk_pg_template_delete(
    session: AsyncSession,
    staff: dict,
    template_ids: list[int],
) -> tuple[int, int]:
    from app.services.pg_access import staff_pg_action
    from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users
    from app.services.plans_catalog import template_allowed_for_staff

    if not staff_pg_action(staff, "templates", "delete"):
        return 0, len(parse_bulk_ids(template_ids))
    try:
        await assert_can_mutate_owned_users(staff, session=session)
    except PgQuotaError:
        return 0, len(parse_bulk_ids(template_ids))

    ids = parse_bulk_ids(template_ids)
    ok = fail = 0
    for tid in ids:
        if not template_allowed_for_staff(staff, tid):
            fail += 1
            continue
        try:
            pg, _ = await _staff_pg_client(session, staff)
            await pg.delete_user_template(tid)
            ok += 1
        except Exception:
            logger.debug("bulk pg template delete failed id=%s", tid, exc_info=True)
            fail += 1
    return ok, fail


async def bulk_pg_group_delete(
    session: AsyncSession,
    staff: dict,
    group_ids: list[int],
) -> tuple[int, int]:
    from app.services.pg_access import staff_pg_action
    from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users
    from app.services.plans_catalog import groups_allowed_for_staff

    if not staff_pg_action(staff, "groups", "delete"):
        return 0, len(parse_bulk_ids(group_ids))
    try:
        await assert_can_mutate_owned_users(staff, session=session)
    except PgQuotaError:
        return 0, len(parse_bulk_ids(group_ids))

    ids = parse_bulk_ids(group_ids)
    ok = fail = 0
    for gid in ids:
        if not groups_allowed_for_staff(staff, [gid]):
            fail += 1
            continue
        try:
            pg, _ = await _staff_pg_client(session, staff)
            await pg.delete_group(gid)
            ok += 1
        except Exception:
            logger.debug("bulk pg group delete failed id=%s", gid, exc_info=True)
            fail += 1
    return ok, fail


async def bulk_pg_host_action(
    session: AsyncSession,
    staff: dict,
    host_ids: list[int],
    action: str,
) -> tuple[int, int]:
    from app.services.pg_access import staff_pg_action
    from app.services.pg_object_scope import assert_owned_host
    from app.services.pg_quota import PgQuotaError, assert_can_mutate_owned_users

    need = "update" if action == "toggle" else "delete"
    if action not in {"toggle", "delete"} or not staff_pg_action(staff, "hosts", need):
        return 0, len(parse_bulk_ids(host_ids))
    try:
        await assert_can_mutate_owned_users(staff, session=session)
    except PgQuotaError:
        return 0, len(parse_bulk_ids(host_ids))

    ids = parse_bulk_ids(host_ids)
    ok = fail = 0
    for hid in ids:
        try:
            pg, _ = await _staff_pg_client(session, staff)
            host = await assert_owned_host(pg, staff, hid)
            if host is None:
                fail += 1
                continue
            if action == "toggle":
                disabled = bool(host.get("is_disabled"))
                await pg.set_host_disabled(hid, not disabled)
            else:
                await pg.delete_host(hid)
            ok += 1
        except Exception:
            logger.debug("bulk pg host %s failed id=%s", action, hid, exc_info=True)
            fail += 1
    return ok, fail


async def bulk_pg_node_action(
    session: AsyncSession,
    staff: dict,
    node_ids: list[int],
    action: str,
) -> tuple[int, int]:
    from app.services.pg_access import staff_pg_action
    from app.services.pg_object_scope import assert_owned_node

    perm_map = {
        "reconnect": "reconnect",
        "sync": "update",
        "reset": "update",
        "toggle": "update",
        "delete": "delete",
    }
    need = perm_map.get(action)
    if not need or not staff_pg_action(staff, "nodes", need):
        return 0, len(parse_bulk_ids(node_ids))

    ids = parse_bulk_ids(node_ids)
    ok = fail = 0
    for nid in ids:
        try:
            pg, _ = await _staff_pg_client(session, staff)
            node = await assert_owned_node(pg, staff, nid)
            if node is None:
                fail += 1
                continue
            if action == "reconnect":
                await pg.reconnect_node(nid)
            elif action == "sync":
                await pg.sync_node(nid)
            elif action == "reset":
                await pg.reset_node(nid)
            elif action == "toggle":
                st = str(node.get("status") or "").lower()
                new_status = (
                    "connected" if st in {"disabled", "error", "limited"} else "disabled"
                )
                await pg.modify_node(nid, {"status": new_status})
            elif action == "delete":
                await pg.delete_node(nid)
            else:
                fail += 1
                continue
            ok += 1
        except Exception:
            logger.debug("bulk pg node %s failed id=%s", action, nid, exc_info=True)
            fail += 1
    return ok, fail
