"""Bulk table action routes — preserve tab/query; eligible-only semantics on server."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession


def _bulk_result(
    return_to: str,
    ok: int,
    fail: int,
    *,
    done: str,
    none: str,
    detail: str | None = None,
):
    from app.services.table_bulk import redirect_bulk

    if ok == 0 and fail:
        if detail:
            return redirect_bulk(return_to, err=detail)
        return redirect_bulk(return_to, err=f"{none} · {fail} نامعتبر")
    msg = f"{ok} {done}"
    if fail:
        msg += f" · {fail} رد شد"
        if detail:
            msg += f" — {detail}"
    return redirect_bulk(return_to, ok=msg)


def register_bulk_pages(
    app,
    *,
    require_admin,
    require_perm,
    get_db,
    require_pg_perm=None,
    require_staff=None,
):
    # ---- users / finance / bot tickets (existing) ----

    @app.post("/users/bulk-action")
    async def users_bulk_action(
        request: Request,
        staff: dict = Depends(require_perm("dashboard")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.platform_identity import is_explicit_owner_staff
        from app.services.table_bulk import (
            bulk_delete_users,
            bulk_renew_users,
            bulk_toggle_block,
            parse_bulk_ids,
            redirect_bulk,
            sanitize_return_to,
        )

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/users"), default="/users"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ کاربری انتخاب نشده")

        manage = is_explicit_owner_staff(staff)
        if action in {"block", "unblock", "delete"} and not manage:
            return redirect_bulk(return_to, err="اجازه این عملیات را ندارید")
        if action == "renew":
            from app.services.pg_access import staff_user_actions

            if not staff_user_actions(staff).get("update"):
                return redirect_bulk(
                    return_to, err="نقش پاسارگارد شما اجازه تمدید ندارد"
                )

        if action == "block":
            ok, fail = await bulk_toggle_block(session, staff, ids, block=True)
            noun = "کاربر مسدود شد"
        elif action == "unblock":
            ok, fail = await bulk_toggle_block(session, staff, ids, block=False)
            noun = "رفع مسدودی"
        elif action == "renew":
            ok, fail = await bulk_renew_users(session, staff, ids)
            noun = "تمدید"
        elif action == "delete":
            from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

            reason = extract_delete_reason(form)
            if delete_reason_too_short(reason):
                return redirect_bulk(return_to, err="علت حذف الزامی است (حداقل ۳ کاراکتر)")
            ok, fail = await bulk_delete_users(session, staff, ids, reason=reason)
            noun = "حذف"
        else:
            return redirect_bulk(return_to, err="عملیات نامعتبر")

        return _bulk_result(return_to, ok, fail, done=noun, none="هیچ موردی انجام نشد")

    @app.post("/finance/orders/bulk-action")
    async def finance_orders_bulk_action(
        request: Request,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import (
            bulk_order_action,
            parse_bulk_ids,
            redirect_bulk,
            sanitize_return_to,
        )

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/finance?tab=orders"),
            default="/finance?tab=orders",
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ سفارشی انتخاب نشده")
        if action not in {"approve", "reject", "cancel"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_order_action(session, staff, ids, action)
        labels = {"approve": "تأیید", "reject": "رد", "cancel": "لغو"}
        return _bulk_result(
            return_to,
            ok,
            fail,
            done=f"سفارش {labels[action]} شد",
            none=f"هیچ سفارشی {labels[action]} نشد",
        )

    @app.post("/finance/payments/bulk-action")
    async def finance_payments_bulk_action(
        request: Request,
        staff: dict = Depends(require_perm("payments")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import (
            bulk_payment_action,
            parse_bulk_ids,
            redirect_bulk,
            sanitize_return_to,
        )

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/finance?tab=payments"),
            default="/finance?tab=payments",
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ پرداختی انتخاب نشده")
        if action not in {"approve", "reject"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_payment_action(session, staff, ids, action)
        labels = {"approve": "تأیید", "reject": "رد"}
        return _bulk_result(
            return_to,
            ok,
            fail,
            done=f"پرداخت {labels[action]} شد",
            none=f"هیچ پرداختی {labels[action]} نشد",
        )

    @app.post("/finance/delivery/bulk-action")
    async def finance_delivery_bulk_action(
        request: Request,
        staff: dict = Depends(require_perm("orders")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import (
            bulk_retry_delivery,
            parse_bulk_ids,
            redirect_bulk,
            sanitize_return_to,
        )

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/finance?tab=delivery"),
            default="/finance?tab=delivery",
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ سفارشی انتخاب نشده")
        if action != "retry":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_retry_delivery(session, staff, ids)
        return _bulk_result(
            return_to,
            ok,
            fail,
            done="تحویل دوباره تلاش شد",
            none="هیچ تحویلی تکرار نشد",
        )

    @app.post("/tickets/bot/bulk-action")
    async def tickets_bot_bulk_action(
        request: Request,
        staff: dict = Depends(require_perm("tickets")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import (
            bulk_close_bot_tickets,
            parse_bulk_ids,
            redirect_bulk,
            sanitize_return_to,
        )

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/tickets"), default="/tickets"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ تیکتی انتخاب نشده")
        if action != "close":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_close_bot_tickets(session, staff, ids)
        return _bulk_result(
            return_to, ok, fail, done="تیکت بسته شد", none="هیچ تیکتی بسته نشد"
        )

    # ---- panel tickets ----
    staff_dep = require_staff or require_admin

    @app.post("/tickets/panel/bulk-action")
    async def tickets_panel_bulk_action(
        request: Request,
        staff: dict = Depends(staff_dep),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_close_panel_tickets

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/tickets"), default="/tickets"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ تیکتی انتخاب نشده")
        if action != "close":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_close_panel_tickets(session, staff, ids)
        return _bulk_result(
            return_to, ok, fail, done="تیکت پنلی بسته شد", none="هیچ تیکتی بسته نشد"
        )

    # ---- resellers ----

    @app.post("/resellers/applications/bulk-action")
    async def reseller_apps_bulk_action(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.representative_unification import (
            RepresentativeUnifyError,
            assert_staff_can_manage_representatives,
        )
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_reseller_app_action

        try:
            assert_staff_can_manage_representatives(staff)
        except RepresentativeUnifyError:
            return redirect_bulk("/resellers/applications", err="اجازه ندارید")

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/resellers/applications"),
            default="/resellers/applications",
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ درخواستی انتخاب نشده")
        if action not in {"approve", "reject"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_reseller_app_action(session, staff, ids, action)
        labels = {"approve": "تأیید", "reject": "رد"}
        return _bulk_result(
            return_to,
            ok,
            fail,
            done=f"درخواست {labels[action]} شد",
            none=f"هیچ درخواستی {labels[action]} نشد",
        )

    @app.post("/resellers/bulk-action")
    async def resellers_bulk_action(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.representative_unification import (
            RepresentativeUnifyError,
            assert_staff_can_manage_representatives,
        )
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import (
            bulk_delete_reseller_users,
            bulk_revoke_resellers,
        )

        try:
            assert_staff_can_manage_representatives(staff)
        except RepresentativeUnifyError:
            return redirect_bulk("/resellers", err="اجازه ندارید")

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/resellers"), default="/resellers"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ نماینده‌ای انتخاب نشده")
        from app.services.delete_reason import delete_reason_too_short, extract_delete_reason

        reason = extract_delete_reason(form)
        if action == "delete_reseller":
            if delete_reason_too_short(reason):
                return redirect_bulk(return_to, err="علت حذف الزامی است (حداقل ۳ کاراکتر)")
            ok, fail = await bulk_revoke_resellers(session, staff, ids, reason=reason)
            return _bulk_result(
                return_to, ok, fail, done="نمایندگی حذف شد", none="هیچ نمایندگی حذف نشد"
            )
        if action == "delete_user":
            if delete_reason_too_short(reason):
                return redirect_bulk(return_to, err="علت حذف الزامی است (حداقل ۳ کاراکتر)")
            ok, fail = await bulk_delete_reseller_users(session, staff, ids, reason=reason)
            return _bulk_result(
                return_to, ok, fail, done="کاربر حذف شد", none="هیچ کاربری حذف نشد"
            )
        return redirect_bulk(return_to, err="عملیات نامعتبر")

    @app.post("/resellers/plans/bulk-action")
    async def reseller_plans_bulk_action(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_reseller_plan_action

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/plans"), default="/plans"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ پلنی انتخاب نشده")
        if action not in {"toggle", "delete"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail, detail = await bulk_reseller_plan_action(session, staff, ids, action)
        labels = {"toggle": "وضعیت پلن تغییر کرد", "delete": "پلن حذف شد"}
        none = {"toggle": "هیچ پلنی تغییر نکرد", "delete": "هیچ پلنی حذف نشد"}
        return _bulk_result(
            return_to, ok, fail, done=labels[action], none=none[action], detail=detail
        )

    # ---- shop plans + gift codes ----

    @app.post("/plans/bulk-action")
    async def plans_bulk_action(
        request: Request,
        staff: dict = Depends(require_perm("plans")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_shop_plan_action

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/plans"), default="/plans"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ پلنی انتخاب نشده")
        if action not in {"toggle", "delete"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail, detail = await bulk_shop_plan_action(session, staff, ids, action)
        labels = {"toggle": "وضعیت پلن تغییر کرد", "delete": "پلن حذف شد"}
        none = {"toggle": "هیچ پلنی تغییر نکرد", "delete": "هیچ پلنی حذف نشد"}
        return _bulk_result(
            return_to, ok, fail, done=labels[action], none=none[action], detail=detail
        )

    @app.post("/plans/gift-codes/bulk-action")
    async def gift_codes_bulk_action(
        request: Request,
        staff: dict = Depends(staff_dep),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_gift_code_toggle

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/plans?gifts=1"),
            default="/plans?gifts=1",
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ کدی انتخاب نشده")
        if action != "toggle":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_gift_code_toggle(session, staff, ids)
        return _bulk_result(
            return_to, ok, fail, done="وضعیت کد تغییر کرد", none="هیچ کدی تغییر نکرد"
        )

    # ---- broadcast ----

    @app.post("/broadcast/history/bulk-action")
    async def broadcast_history_bulk_action(
        request: Request,
        staff: dict = Depends(require_admin),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_broadcast_delete

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/broadcast"), default="/broadcast"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ رکوردی انتخاب نشده")
        if action != "delete":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_broadcast_delete(session, ids)
        return _bulk_result(
            return_to, ok, fail, done="رکورد حذف شد", none="هیچ رکوردی حذف نشد"
        )

    # ---- PasarGuard ----
    if require_pg_perm is None:
        return

    @app.post("/pg/users/bulk-action")
    async def pg_users_bulk_action(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_users")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_pg_user_action

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/pg/users"), default="/pg/users"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ کاربری انتخاب نشده")
        if action not in {"disable", "enable", "reset", "revoke", "delete"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_pg_user_action(session, staff, ids, action)
        labels = {
            "disable": "کاربر غیرفعال شد",
            "enable": "کاربر فعال شد",
            "reset": "مصرف ریست شد",
            "revoke": "ساب ابطال شد",
            "delete": "کاربر حذف شد",
        }
        return _bulk_result(
            return_to, ok, fail, done=labels[action], none="هیچ موردی انجام نشد"
        )

    @app.post("/pg/templates/bulk-action")
    async def pg_templates_bulk_action(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_templates")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_pg_template_delete

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/pg/templates"), default="/pg/templates"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ تمپلیتی انتخاب نشده")
        if action != "delete":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_pg_template_delete(session, staff, ids)
        return _bulk_result(
            return_to, ok, fail, done="تمپلیت حذف شد", none="هیچ تمپلیتی حذف نشد"
        )

    @app.post("/pg/groups/bulk-action")
    async def pg_groups_bulk_action(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_groups")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_pg_group_delete

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/pg/groups"), default="/pg/groups"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ گروهی انتخاب نشده")
        if action != "delete":
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_pg_group_delete(session, staff, ids)
        return _bulk_result(
            return_to, ok, fail, done="گروه حذف شد", none="هیچ گروهی حذف نشد"
        )

    @app.post("/pg/hosts/bulk-action")
    async def pg_hosts_bulk_action(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_hosts")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_pg_host_action

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/pg/hosts"), default="/pg/hosts"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ هاستی انتخاب نشده")
        if action not in {"toggle", "delete"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_pg_host_action(session, staff, ids, action)
        labels = {"toggle": "وضعیت هاست تغییر کرد", "delete": "هاست حذف شد"}
        none = {"toggle": "هیچ هاستی تغییر نکرد", "delete": "هیچ هاستی حذف نشد"}
        return _bulk_result(return_to, ok, fail, done=labels[action], none=none[action])

    @app.post("/pg/nodes/bulk-action")
    async def pg_nodes_bulk_action(
        request: Request,
        staff: dict = Depends(require_pg_perm("pg_nodes")),
        session: AsyncSession = Depends(get_db),
    ):
        from app.services.table_bulk import parse_bulk_ids, redirect_bulk, sanitize_return_to
        from app.services.table_bulk_ext import bulk_pg_node_action

        form = await request.form()
        action = str(form.get("action") or "").strip()
        ids = parse_bulk_ids(form.getlist("ids"))
        return_to = sanitize_return_to(
            str(form.get("return_to") or "/pg/nodes"), default="/pg/nodes"
        )
        if not ids:
            return redirect_bulk(return_to, err="هیچ نودی انتخاب نشده")
        if action not in {"reconnect", "sync", "reset", "toggle", "delete"}:
            return redirect_bulk(return_to, err="عملیات نامعتبر")
        ok, fail = await bulk_pg_node_action(session, staff, ids, action)
        labels = {
            "reconnect": "اتصال مجدد ارسال شد",
            "sync": "همگام‌سازی شد",
            "reset": "مصرف ریست شد",
            "toggle": "وضعیت نود تغییر کرد",
            "delete": "نود حذف شد",
        }
        return _bulk_result(
            return_to, ok, fail, done=labels[action], none="هیچ نودی انجام نشد"
        )
