"""Mini App commerce + ops API (payments, wallet, tickets, reseller queues).

Security invariants (must not regress):
- Every mutation scopes to ``BotUser`` from validated initData.
- Platform catalog only (no shop ContextVar in Mini App).
- Admin persona: ops review of **platform** payments only — never shop tenants.
- Reseller persona: own shop orders/customers/tickets only; never wallet top-ups.
- Receipt uploads go to private store (``local:``), never public media.
- Errors use ``_safe_client_message`` — no internal English leaks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BotUser,
    Order,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Ticket,
    TicketStatus,
    UserService,
)
from app.services.db_safe import rollback_quiet
from app.services.miniapp_acl import (
    load_ops_context,
    mini_can_access_ticket,
    mini_can_manage_customer,
    mini_can_review_payment,
)
from app.services.miniapp_auth import load_mini_user, resolve_mini_persona
from app.services.payment_destinations import enrich_payment_settings
from app.services.users import get_all_settings, on

log = logging.getLogger(__name__)


def register_miniapp_commerce(app, *, get_db, helpers: dict) -> None:
    """Attach commerce/ops routes. ``helpers`` from ``miniapp_pages`` module."""
    _no_store = helpers["_no_store"]
    _require_commerce = helpers["_require_commerce"]
    _require_commerce_ready = helpers["_require_commerce_ready"]
    _owned_service_or_404 = helpers["_owned_service_or_404"]
    _safe_client_message = helpers["_safe_client_message"]
    _serialize_service = helpers["_serialize_service"]
    _fetch_pg_info = helpers["_fetch_pg_info"]
    _enrich_services = helpers["_enrich_services"]

    def _pay_methods_payload(ui: dict) -> dict:
        enriched = enrich_payment_settings(dict(ui))
        methods = []
        if on(enriched.get("pay_wallet_enabled")):
            methods.append({"id": "wallet", "label": "کیف پول"})
        if on(enriched.get("pay_card_enabled")):
            methods.append({"id": "card", "label": "کارت به کارت"})
        if on(enriched.get("pay_gateway_enabled")):
            methods.append({"id": "gateway", "label": "درگاه لینکی"})
        if on(enriched.get("pay_psp_enabled")):
            methods.append({"id": "psp", "label": "درگاه آنلاین"})
        cards = enriched.get("payment_cards_parsed") or []
        card0 = cards[0] if cards else {}
        return {
            "methods": methods,
            "discount_enabled": on(enriched.get("pay_discount_enabled")),
            "card": {
                "number": (card0.get("number") or enriched.get("card_number") or "")[:32],
                "holder": (card0.get("holder") or enriched.get("card_holder") or "")[:64],
            },
            "gateway_text": (enriched.get("gateway_pay_text") or "")[:500],
            "psp_text": (enriched.get("psp_pay_text") or "")[:500],
            "auto_renew_shop_enabled": on(enriched.get("auto_renew_enabled", "0")),
            "self_pause_enabled": on(enriched.get("user_self_pause_enabled", "1")),
            "emergency_credit_enabled": on(enriched.get("emergency_credit_enabled", "0")),
            "cart_recovery_enabled": on(enriched.get("cart_recovery_enabled", "0")),
        }

    @app.get("/api/mini/pay-methods")
    async def mini_pay_methods(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        _require_commerce(user)
        ui = await get_all_settings(session)
        return _no_store(_pay_methods_payload(ui))

    @app.post("/api/mini/order")
    async def mini_create_order(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.orders import create_order, get_catalog_plan, renew_service_with_plan

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        try:
            plan_id = int(body.get("plan_id"))
        except (TypeError, ValueError):
            raise HTTPException(400, "plan_id required")
        discount = (body.get("discount_code") or "").strip() or None
        renew_service_id = body.get("service_id")
        plan = await get_catalog_plan(session, plan_id)
        if not plan or not plan.is_active or plan.owner_reseller_id is not None:
            raise HTTPException(400, "پلن یافت نشد")
        try:
            if renew_service_id is not None:
                svc = _owned_service_or_404(
                    await session.get(UserService, int(renew_service_id)), user
                )
                if plan.is_trial:
                    raise HTTPException(400, "پلن تست برای تمدید مجاز نیست")
                order = await renew_service_with_plan(
                    session, user_id=user.id, service=svc, plan=plan
                )
            else:
                order = await create_order(
                    session,
                    user_id=user.id,
                    plan_id=plan_id,
                    discount_code=discount,
                )
        except HTTPException:
            raise
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="ایجاد سفارش ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini order failed user=%s", user.id)
            raise HTTPException(500, "ایجاد سفارش ناموفق")
        return _no_store(
            {
                "ok": True,
                "order_id": order.id,
                "amount": int(order.amount or 0),
                "status": order.status,
                "discount_amount": int(order.discount_amount or 0),
            }
        )

    @app.post("/api/mini/order/{order_id}/pay")
    async def mini_pay_order(
        order_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.orders import pay_with_wallet, start_method_payment
        from app.services.payment_settlement import create_psp_checkout

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        order = await session.get(Order, int(order_id))
        if not order or int(order.user_id) != int(user.id):
            raise HTTPException(404)
        if order.reseller_id is not None:
            # Mini App is platform-scoped — refuse shop-attributed orders.
            raise HTTPException(403, "این سفارش در مینی‌اپ قابل پرداخت نیست")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        method = (body.get("method") or "").strip().lower()
        ui = await get_all_settings(session)
        methods = {m["id"] for m in _pay_methods_payload(ui)["methods"]}
        if method not in methods:
            raise HTTPException(400, "روش پرداخت نامعتبر است")
        try:
            if method == "wallet":
                if not on(ui.get("pay_wallet_enabled")):
                    raise HTTPException(403, "پرداخت با کیف پول غیرفعال است")
                await session.refresh(user)
                order = await pay_with_wallet(session, order, user)
                await session.refresh(user)
                from app.services.commerce_extras import onboarding_steps

                return _no_store(
                    {
                        "ok": True,
                        "status": order.status,
                        "wallet": int(user.wallet_balance or 0),
                        "message": "پرداخت موفق",
                        "onboarding": onboarding_steps(),
                    }
                )
            method_map = {
                "card": PaymentMethod.CARD.value,
                "gateway": PaymentMethod.GATEWAY.value,
                "psp": PaymentMethod.PSP.value,
            }
            pay_method = method_map.get(method)
            if not pay_method:
                raise HTTPException(400, "روش پرداخت نامعتبر است")
            payment = await start_method_payment(session, order, user.id, pay_method)
            out: dict = {
                "ok": True,
                "payment_id": payment.id,
                "order_id": order.id,
                "amount": int(payment.amount or 0),
                "method": method,
                "status": "awaiting_receipt",
            }
            pay_info = _pay_methods_payload(ui)
            if method == "card":
                out["card"] = pay_info["card"]
                out["message"] = "مبلغ را واریز کنید و رسید را بارگذاری کنید"
            elif method == "gateway":
                out["gateway_text"] = pay_info["gateway_text"]
                out["message"] = "پس از پرداخت، رسید را بارگذاری کنید"
            elif method == "psp":
                settlement = await create_psp_checkout(
                    session,
                    payment,
                    reseller_id=None,
                    description=f"order:{order.id}",
                )
                out["checkout_url"] = settlement.checkout_url or ""
                out["settlement_id"] = settlement.id
                out["status"] = "awaiting_psp"
                out["message"] = "برای پرداخت درگاه را باز کنید"
            return _no_store(out)
        except HTTPException:
            raise
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="پرداخت ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini pay failed order=%s", order_id)
            raise HTTPException(500, "پرداخت ناموفق")

    @app.post("/api/mini/payment/{payment_id}/receipt")
    async def mini_attach_receipt(
        payment_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        file: UploadFile = File(...),
    ):
        from aiogram import Bot

        from app.config import get_settings
        from app.services.orders import attach_receipt
        from app.services.receipt_uploads import save_mini_receipt_upload
        from app.services.receipts import process_receipt

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        payment = await session.get(Payment, int(payment_id))
        if not payment or int(payment.user_id) != int(user.id):
            raise HTTPException(404)
        if payment.status != PaymentStatus.PENDING.value:
            raise HTTPException(400, "این پرداخت قابل بروزرسانی نیست")
        if payment.order_id:
            order = await session.get(Order, payment.order_id)
            if order and order.reseller_id is not None:
                raise HTTPException(403, "دسترسی ندارید")
        try:
            file_id = await save_mini_receipt_upload(file, payment_id=payment.id)
            payment = await attach_receipt(session, payment, file_id)
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="آپلود رسید ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini receipt upload failed payment=%s", payment_id)
            raise HTTPException(500, "آپلود رسید ناموفق")

        token = (get_settings().bot_token or "").strip()
        msg = "رسید ثبت شد و در صف بررسی است"
        if token:
            bot = Bot(token=token)
            try:
                status_text = await process_receipt(
                    session,
                    payment,
                    bot=bot,
                    user_tg_id=int(user.telegram_id),
                )
                if status_text:
                    msg = status_text
                else:
                    msg = "پرداخت تأیید و تحویل شد"
            except Exception:
                log.exception("mini process_receipt failed payment=%s", payment_id)
            finally:
                await bot.session.close()
        return _no_store({"ok": True, "payment_id": payment.id, "message": msg})

    @app.post("/api/mini/wallet/topup")
    async def mini_wallet_topup(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.orders import create_wallet_topup
        from app.services.payment_settlement import create_psp_checkout

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        try:
            amount = int(body.get("amount"))
        except (TypeError, ValueError):
            raise HTTPException(400, "مبلغ نامعتبر است")
        method = (body.get("method") or "card").strip().lower()
        ui = await get_all_settings(session)
        methods = {m["id"] for m in _pay_methods_payload(ui)["methods"] if m["id"] != "wallet"}
        if method not in methods:
            raise HTTPException(400, "روش شارژ نامعتبر است")
        method_map = {
            "card": PaymentMethod.CARD.value,
            "gateway": PaymentMethod.GATEWAY.value,
            "psp": PaymentMethod.PSP.value,
        }
        try:
            payment = await create_wallet_topup(
                session, user.id, amount, method=method_map[method]
            )
            out: dict = {
                "ok": True,
                "payment_id": payment.id,
                "amount": int(payment.amount or 0),
                "method": method,
            }
            pay_info = _pay_methods_payload(ui)
            if method == "card":
                out["card"] = pay_info["card"]
                out["message"] = "واریز کنید و رسید را بارگذاری کنید"
            elif method == "gateway":
                out["gateway_text"] = pay_info["gateway_text"]
            elif method == "psp":
                settlement = await create_psp_checkout(
                    session,
                    payment,
                    reseller_id=None,
                    description=f"topup:{payment.id}",
                )
                out["checkout_url"] = settlement.checkout_url or ""
                out["settlement_id"] = settlement.id
            return _no_store(out)
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="شارژ ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini topup failed user=%s", user.id)
            raise HTTPException(500, "شارژ ناموفق")

    @app.post("/api/mini/wallet/redeem")
    async def mini_redeem_code(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.ux20 import redeem_charge_code

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        code = (body.get("code") or "").strip()
        if not code:
            raise HTTPException(400, "کد لازم است")
        try:
            _row, amount = await redeem_charge_code(session, user=user, code=code)
            await session.refresh(user)
            return _no_store(
                {
                    "ok": True,
                    "amount": int(amount),
                    "wallet": int(user.wallet_balance or 0),
                    "message": "کد با موفقیت اعمال شد",
                }
            )
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="کد نامعتبر است")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini redeem failed user=%s", user.id)
            raise HTTPException(500, "اعمال کد ناموفق")

    @app.get("/api/mini/tickets")
    async def mini_list_tickets(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.tickets import list_user_tickets

        user = await load_mini_user(session, request)
        persona = resolve_mini_persona(user)
        if persona == "admin":
            raise HTTPException(403, "از بخش عملیات استفاده کنید")
        if not commerce_allowed_persona(persona) and persona != "reseller":
            raise HTTPException(403)
        # Sticky shop customers see their shop-scoped tickets; platform users see platform.
        shop_rid = int(user.reseller_id) if user.reseller_id else None
        tickets = await list_user_tickets(session, user.id, reseller_id=shop_rid)
        out = []
        for t in tickets:
            if int(t.user_id) != int(user.id):
                continue
            out.append(
                {
                    "id": t.id,
                    "subject": t.subject or "",
                    "status": t.status,
                    "created_at": t.created_at.astimezone(timezone.utc).isoformat()
                    if isinstance(t.created_at, datetime)
                    else None,
                }
            )
        return _no_store({"tickets": out})

    @app.post("/api/mini/tickets")
    async def mini_create_ticket(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.commerce_extras import diagnose_user_services
        from app.services.tickets import create_ticket

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        subject = (body.get("subject") or "پشتیبانی مینی‌اپ").strip()[:250]
        text = (body.get("body") or "").strip()
        if len(text) < 3:
            raise HTTPException(400, "متن پیام کوتاه است")
        diag = await diagnose_user_services(session, user)
        if body.get("diagnose_only"):
            return _no_store({"ok": True, "diagnose": diag})
        # Prepend diagnose tips for staff (not secret).
        if diag.get("tips"):
            text = "🔎 تشخیص خودکار:\n- " + "\n- ".join(diag["tips"][:5]) + "\n\n" + text
        # Scope to sticky shop when present — never dump shop traffic into platform queue.
        shop_rid = int(user.reseller_id) if user.reseller_id else None
        try:
            ticket = await create_ticket(
                session,
                user.id,
                subject,
                text,
                int(user.telegram_id),
                reseller_id=shop_rid,
            )
        except Exception:
            await rollback_quiet(session)
            log.exception("mini ticket create failed")
            raise HTTPException(500, "ثبت تیکت ناموفق")
        return _no_store(
            {
                "ok": True,
                "ticket_id": ticket.id,
                "diagnose": diag,
                "message": "تیکت ثبت شد",
            }
        )

    @app.get("/api/mini/service/{service_id}/health")
    async def mini_service_health(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.commerce_extras import health_check_service

        user = await load_mini_user(session, request)
        _require_commerce(user)
        svc = _owned_service_or_404(await session.get(UserService, service_id), user)
        try:
            data = await health_check_service(session, user=user, service=svc)
        except ValueError as exc:
            raise HTTPException(400, _safe_client_message(exc, fallback="بررسی ناموفق"))
        return _no_store(data)

    @app.post("/api/mini/service/{service_id}/auto-renew")
    async def mini_auto_renew(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.commerce_extras import set_auto_renew

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        svc = _owned_service_or_404(await session.get(UserService, service_id), user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        enabled = bool(body.get("enabled"))
        plan_id = body.get("plan_id")
        try:
            svc = await set_auto_renew(
                session,
                user=user,
                service=svc,
                enabled=enabled,
                plan_id=int(plan_id) if plan_id is not None else None,
            )
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="تنظیم تمدید ناموفق")
            ) from exc
        return _no_store(
            {
                "ok": True,
                "auto_renew_enabled": bool(svc.auto_renew_enabled),
                "auto_renew_plan_id": svc.auto_renew_plan_id,
            }
        )

    @app.post("/api/mini/service/{service_id}/pause")
    async def mini_pause_service(
        service_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.commerce_extras import pause_service

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        svc = _owned_service_or_404(await session.get(UserService, service_id), user)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        pause = bool(body.get("pause", True))
        try:
            svc = await pause_service(
                session,
                user=user,
                service=svc,
                pause=pause,
                reason=str(body.get("reason") or "miniapp")[:80],
            )
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="توقف سرویس ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini pause failed svc=%s", service_id)
            raise HTTPException(500, "توقف سرویس ناموفق")
        return _no_store(
            {
                "ok": True,
                "paused": svc.paused_at is not None,
                "message": "سرویس متوقف شد" if pause else "سرویس از سر گرفته شد",
            }
        )

    @app.post("/api/mini/emergency-credit")
    async def mini_emergency_credit(
        request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.commerce_extras import grant_emergency_credit

        user = await load_mini_user(session, request)
        await _require_commerce_ready(session, user)
        try:
            data = await grant_emergency_credit(session, user=user)
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="اعتبار اضطراری ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini emergency credit failed")
            raise HTTPException(500, "اعتبار اضطراری ناموفق")
        await session.refresh(user)
        data["wallet"] = int(user.wallet_balance or 0)
        data["ok"] = True
        return _no_store(data)

    # --- Ops (admin / reseller) ---

    @app.get("/api/mini/ops/pending-payments")
    async def mini_ops_pending(
        request: Request, session: AsyncSession = Depends(get_db)
    ):
        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        q = (
            select(Payment, Order, BotUser)
            .outerjoin(Order, Payment.order_id == Order.id)
            .join(BotUser, Payment.user_id == BotUser.id)
            .where(
                Payment.status == PaymentStatus.PENDING.value,
                Payment.receipt_file_id.is_not(None),
            )
            .order_by(Payment.id.desc())
            .limit(40)
        )
        rows = (await session.execute(q)).all()
        items = []
        for payment, order, payer in rows:
            if not mini_can_review_payment(
                persona=persona,
                reviewer=user,
                payment=payment,
                order=order,
                profile=profile,
            ):
                continue
            items.append(
                {
                    "id": payment.id,
                    "amount": int(payment.amount or 0),
                    "method": payment.method,
                    "is_wallet_topup": bool(payment.is_wallet_topup),
                    "order_id": payment.order_id,
                    "user_name": payer.full_name or payer.username or str(payer.telegram_id),
                    "has_local_receipt": str(payment.receipt_file_id or "").startswith(
                        "local:"
                    ),
                }
            )
        return _no_store({"payments": items})

    @app.post("/api/mini/ops/payments/{payment_id}/review")
    async def mini_ops_review(
        payment_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from aiogram import Bot

        from app.config import get_settings
        from app.services.delivery import send_delivery_to_user
        from app.services.orders import approve_payment

        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        payment = await session.get(Payment, int(payment_id))
        if not payment:
            raise HTTPException(404)
        order = await session.get(Order, payment.order_id) if payment.order_id else None
        if not mini_can_review_payment(
            persona=persona,
            reviewer=user,
            payment=payment,
            order=order,
            profile=profile,
        ):
            raise HTTPException(403, "دسترسی ندارید")
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        action = (body.get("action") or "").strip().lower()
        if action not in {"approve", "reject"}:
            raise HTTPException(400, "action نامعتبر است")
        try:
            if action == "approve":
                result_order = await approve_payment(
                    session, payment, int(user.telegram_id)
                )
                payer = await session.get(BotUser, payment.user_id)
                token = (get_settings().bot_token or "").strip()
                if token and payer is not None:
                    bot = Bot(token=token)
                    try:
                        await send_delivery_to_user(
                            bot,
                            int(payer.telegram_id),
                            session,
                            payment,
                            result_order,
                        )
                    except Exception as send_exc:
                        log.exception("mini ops delivery notify failed")
                        if result_order is not None:
                            try:
                                from app.services.ux20 import note_delivery_send_failure

                                await note_delivery_send_failure(
                                    session,
                                    order=result_order,
                                    payment=payment,
                                    error=str(send_exc),
                                )
                            except Exception:
                                pass
                    finally:
                        await bot.session.close()
                return _no_store({"ok": True, "message": "تأیید شد"})
            # Atomic reject — never overwrite APPROVED / never skip discount release.
            from app.services.orders import reject_payment

            await reject_payment(
                session,
                payment,
                int(user.telegram_id),
                note=(body.get("note") or "رد از مینی‌اپ")[:250],
            )
            return _no_store({"ok": True, "message": "رد شد"})
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="بررسی ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini ops review failed payment=%s", payment_id)
            raise HTTPException(500, "بررسی ناموفق")

    @app.get("/api/mini/ops/customers")
    async def mini_ops_customers(
        request: Request, session: AsyncSession = Depends(get_db)
    ):
        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        q = (request.query_params.get("q") or "").strip()
        query = select(BotUser).where(BotUser.is_blocked.is_(False))
        if persona == "admin":
            query = query.where(BotUser.reseller_id.is_(None))
        else:
            assert profile is not None
            query = query.where(BotUser.reseller_id == int(profile.user_id))
        if q:
            like = f"%{q[:64]}%"
            clauses = [BotUser.username.ilike(like), BotUser.full_name.ilike(like)]
            if q.isdigit():
                clauses.append(BotUser.telegram_id == int(q))
                clauses.append(BotUser.id == int(q))
            query = query.where(or_(*clauses))
        rows = list(
            (await session.execute(query.order_by(BotUser.id.desc()).limit(30)))
            .scalars()
            .all()
        )
        items = []
        for c in rows:
            if not mini_can_manage_customer(
                persona=persona, customer=c, profile=profile
            ):
                continue
            items.append(
                {
                    "id": c.id,
                    "name": c.full_name or c.username or str(c.telegram_id),
                    "telegram_id": c.telegram_id,
                    "wallet": int(c.wallet_balance or 0) if persona == "admin" else None,
                }
            )
        return _no_store({"customers": items})

    @app.post("/api/mini/ops/customers/{customer_id}/quick-renew")
    async def mini_ops_quick_renew(
        customer_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.users_quick import quick_renew_user

        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        customer = await session.get(BotUser, int(customer_id))
        if not customer or not mini_can_manage_customer(
            persona=persona, customer=customer, profile=profile
        ):
            raise HTTPException(404)
        if persona == "reseller" and profile is not None:
            from app.services.resellers import has_bot_perm

            if not has_bot_perm(profile, "users"):
                raise HTTPException(403, "دسترسی ندارید")
        try:
            _svc, label = await quick_renew_user(session, customer, staff=None)
            return _no_store({"ok": True, "message": f"تمدید شد: {label}"[:180]})
        except ValueError as exc:
            await rollback_quiet(session)
            raise HTTPException(
                400, _safe_client_message(exc, fallback="تمدید ناموفق")
            ) from exc
        except Exception:
            await rollback_quiet(session)
            log.exception("mini ops quick renew failed")
            raise HTTPException(500, "تمدید ناموفق")

    @app.get("/api/mini/ops/tickets")
    async def mini_ops_tickets(request: Request, session: AsyncSession = Depends(get_db)):
        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        q = select(Ticket).where(Ticket.status == TicketStatus.OPEN.value)
        if persona == "admin":
            q = q.where(Ticket.reseller_id.is_(None))
        else:
            assert profile is not None
            from app.services.resellers import has_bot_perm

            if not has_bot_perm(profile, "tickets"):
                raise HTTPException(403, "دسترسی ندارید")
            q = q.where(Ticket.reseller_id == int(profile.user_id))
        rows = list(
            (await session.execute(q.order_by(Ticket.id.desc()).limit(40))).scalars().all()
        )
        items = []
        for t in rows:
            if not mini_can_access_ticket(
                persona=persona, actor=user, ticket=t, profile=profile
            ):
                continue
            items.append(
                {
                    "id": t.id,
                    "subject": t.subject or "",
                    "user_id": t.user_id,
                    "status": t.status,
                }
            )
        return _no_store({"tickets": items})

    @app.post("/api/mini/ops/tickets/{ticket_id}/reply")
    async def mini_ops_ticket_reply(
        ticket_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from app.services.tickets import reply_ticket

        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        ticket = await session.get(Ticket, int(ticket_id))
        if not ticket or not mini_can_access_ticket(
            persona=persona, actor=user, ticket=ticket, profile=profile
        ):
            raise HTTPException(404)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "bad json")
        text = (body.get("body") or "").strip()
        if len(text) < 1:
            raise HTTPException(400, "متن خالی است")
        try:
            await reply_ticket(
                session,
                ticket,
                text,
                int(user.telegram_id),
                is_staff=True,
            )
            return _no_store({"ok": True, "message": "پاسخ ثبت شد"})
        except Exception:
            await rollback_quiet(session)
            log.exception("mini ops ticket reply failed")
            raise HTTPException(500, "پاسخ ناموفق")

    @app.get("/api/mini/ops/coaching")
    async def mini_ops_coaching(request: Request, session: AsyncSession = Depends(get_db)):
        from app.services.commerce_extras import reseller_coaching_stats

        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        if persona != "reseller" or profile is None:
            raise HTTPException(403, "فقط برای نماینده")
        stats = await reseller_coaching_stats(session, profile=profile)
        return _no_store({"ok": True, **stats})

    @app.get("/api/mini/ops/receipt/{payment_id}")
    async def mini_ops_receipt(
        payment_id: int, request: Request, session: AsyncSession = Depends(get_db)
    ):
        from fastapi.responses import Response

        from app.services.receipt_uploads import resolve_local_receipt_path

        user = await load_mini_user(session, request)
        persona, profile = await load_ops_context(session, user)
        payment = await session.get(Payment, int(payment_id))
        if not payment:
            raise HTTPException(404)
        order = await session.get(Order, payment.order_id) if payment.order_id else None
        if not mini_can_review_payment(
            persona=persona,
            reviewer=user,
            payment=payment,
            order=order,
            profile=profile,
        ):
            raise HTTPException(403, "دسترسی ندارید")
        path = resolve_local_receipt_path(payment.receipt_file_id)
        if not path:
            raise HTTPException(404, "رسید محلی نیست")
        from app.services.receipt_uploads import sniff_local_receipt_mime

        data = path.read_bytes()
        ctype = sniff_local_receipt_mime(path)
        return Response(
            content=data,
            media_type=ctype,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": f'inline; filename="receipt-{payment_id}"',
                "X-Content-Type-Options": "nosniff",
            },
        )


def commerce_allowed_persona(persona: str) -> bool:
    return (persona or "").strip() in {"user", "reseller"}
