from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.tg_utils import safe_edit_text
from app.config import get_settings
from app.db.models import BotUser, UserService
from app.services.formatting import format_message, format_toman, service_card
from app.services.orders import get_plan, list_active_plans
from app.services.pasarguard import get_pg
from app.services.users import get_all_settings
from app.services.redact import user_safe_error
from app.services.service_live_info import fetch_live_service_info

router = Router(name="services")

class CancellationStates(StatesGroup):
    reason = State()

@router.callback_query(F.data.startswith("svc:cancel:"))
async def svc_cancel(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.service_cancellations import cancellation_status
    from app.services.users import current_shop_reseller_id
    try:
        service_id = int(callback.data.split(":")[-1])
        status = await cancellation_status(session, db_user, service_id, shop_id=current_shop_reseller_id())
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    text = "📝 درخواست لغو سرویس\nاپراتور مبلغ اعتبار برگشتی را تعیین می‌کند. پس از تأیید، سرویس غیرفعال و اعتبار به کیف پول همین فروشگاه برمی‌گردد. ثبت درخواست به‌تنهایی سرویس را غیرفعال نمی‌کند."
    buttons = []
    if status:
        text += f"\n\nدرخواست #{status['id']}: {html.escape(status['label'])}"
        if status["operator_note"]:
            text += "\n" + html.escape(status["operator_note"])
        if status["refund_amount"] is not None:
            text += "\nاعتبار برگشتی: " + format_toman(status["refund_amount"])
    if not status or status["status"] in {"rejected", "withdrawn"}:
        buttons.append([InlineKeyboardButton(text="ثبت درخواست و دلیل لغو", callback_data=f"svc:cancelnew:{service_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="به‌روزرسانی درخواست", callback_data=f"svc:cancel:{service_id}")])
    buttons.append([InlineKeyboardButton(text="بازگشت به سرویس", callback_data=f"svc:view:{service_id}")])
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@router.callback_query(F.data.startswith("svc:cancelnew:"))
async def svc_cancel_new(callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext):
    from app.services.service_cancellations import owned_cancellation_service
    from app.services.users import current_shop_reseller_id
    try:
        service_id = int(callback.data.split(":")[-1])
        await owned_cancellation_service(session, db_user, service_id, current_shop_reseller_id())
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await state.update_data(cancellation_service_id=service_id)
    await state.set_state(CancellationStates.reason)
    if callback.message:
        await callback.message.answer("دلیل درخواست لغو را بنویسید (حداکثر ۱۰۰۰ نویسه).", reply_markup=kb.cancel_reply())
    await callback.answer()

@router.message(CancellationStates.reason, F.text)
async def svc_cancel_reason(message: Message, session: AsyncSession, db_user: BotUser, state: FSMContext):
    from app.services.service_cancellations import request_cancellation
    from app.services.users import current_shop_reseller_id
    data = await state.get_data()
    try:
        row = await request_cancellation(session, db_user, int(data.get("cancellation_service_id") or 0), shop_id=current_shop_reseller_id(), reason=message.text)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await state.set_state(None)
    await message.answer(f"درخواست #{row['id']} ثبت شد. نتیجه و اعتبار برگشتی را از «درخواست لغو سرویس» ببینید.", reply_markup=kb.service_actions_reply_keyboard(await get_all_settings(session)))

@router.callback_query(F.data == "campaign:optout")
async def campaign_optout(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.campaigns import set_marketing_preference
    from app.services.users import current_shop_reseller_id
    await set_marketing_preference(session, db_user.id, shop_id=current_shop_reseller_id(), enabled=False)
    await callback.answer("پیام‌های پیشنهادی این فروشگاه قطع شد. برای فعال‌سازی: /marketing", show_alert=True)

@router.message(Command("marketing"))
async def marketing_settings(message: Message, session: AsyncSession, db_user: BotUser):
    await message.answer("دریافت پیام‌های پیشنهادی همین فروشگاه:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="فعال", callback_data="campaign:optin"),
        InlineKeyboardButton(text="غیرفعال", callback_data="campaign:optout"),
    ]]))

@router.callback_query(F.data == "campaign:optin")
async def campaign_optin(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.campaigns import set_marketing_preference
    from app.services.users import current_shop_reseller_id
    await set_marketing_preference(session, db_user.id, shop_id=current_shop_reseller_id(), enabled=True)
    await callback.answer("دریافت پیام‌های پیشنهادی فعال شد.", show_alert=True)

async def _show_automation(callback, session, db_user, service_id):
    from app.services.service_automation import (
        ACTIONS, CHOICE_FIELDS, LABELS, automation_choices,
        get_automation, owned_automation_service, selected_choice,
    )
    service = await owned_automation_service(session, db_user, service_id)
    row = await get_automation(session, service)
    choices = await automation_choices(session)
    await session.commit()
    lines = [
        f"سرویس: <b>{html.escape(service.pg_username)}</b>",
        "هزینه با قیمت فعلی پلن یا بسته، فقط از کیف پول همین فروشگاه پرداخت می‌شود.",
        "تمدید کامل پس از پایان زمان یا حجم انجام می‌شود. اگر افزایش خودکار همان مورد روشن باشد، بسته انتخاب‌شده اولویت دارد.",
    ]
    buttons = []
    if row.needs_review:
        lines.append(f"⚠️ سفارش #{row.pending_order_id} نیاز به بررسی پشتیبانی دارد؛ اجرای خودکار متوقف است.")
    for action in ACTIONS:
        enabled = getattr(row, f"{action}_enabled")
        choice_id = getattr(row, CHOICE_FIELDS[action])
        # Keep stale settings reachable even after the last pack is removed.
        if action != "renew" and not (choices[action] or enabled or choice_id or getattr(row, f"{action}_notice")):
            continue
        choice = await selected_choice(session, row, action)
        label = f"{html.escape(choice.name)} — {format_toman(choice.price)}" if choice else "انتخاب نشده / در دسترس نیست"
        lines.append(f"\n{'✅' if enabled else '▫️'} <b>{LABELS[action]}</b>\n{label}")
        if enabled and not choice:
            lines.append("⚠️ پلن یا بسته قبلی حذف یا غیرفعال شده؛ گزینه جدید انتخاب کنید.")
        buttons.append([
            InlineKeyboardButton(text=f"{'☑️' if enabled else '⬜️'} {LABELS[action]}", callback_data=f"svc:autotoggle:{service_id}:{action}"),
            InlineKeyboardButton(text="انتخاب پلن" if action == "renew" else "انتخاب بسته", callback_data=f"svc:autopick:{service_id}:{action}"),
        ])
    lines.append("\nبا روشن کردن هر گزینه، خرید تکرارشونده از کیف پول را فعال می‌کنید. موجودی ناکافی باعث خرید نمی‌شود؛ پس از شارژ دوباره تلاش می‌شود.")
    buttons.append([InlineKeyboardButton(text="بازگشت به سرویس", callback_data=f"svc:view:{service_id}")])
    if callback.message:
        await safe_edit_text(callback.message, format_message("⚙️ تنظیمات خودکار سرویس", "\n".join(lines)), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("svc:auto:"))
async def svc_auto(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    try:
        service_id = int(callback.data.split(":")[-1])
        await _show_automation(callback, session, db_user, service_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer()

@router.callback_query(F.data.startswith("svc:autopick:"))
async def svc_auto_pick(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.service_automation import ACTIONS, LABELS, automation_choices, owned_automation_service
    try:
        _, _, service_id, action = callback.data.split(":")
        service_id = int(service_id)
        if action not in ACTIONS:
            raise ValueError("تنظیم نامعتبر است")
        await owned_automation_service(session, db_user, service_id)
        choices = (await automation_choices(session))[action]
        if not choices:
            raise ValueError("پلن یا بسته فعالی موجود نیست")
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    from app.services.service_addons import amount_label
    buttons = [[InlineKeyboardButton(
        text=f"{p.name}{' — +' + amount_label(p) if action != 'renew' else ''} — {format_toman(p.price)}",
        callback_data=f"svc:autoset:{service_id}:{action}:{p.id}",
    )] for p in choices]
    buttons.append([InlineKeyboardButton(text="بازگشت به تنظیمات", callback_data=f"svc:auto:{service_id}")])
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, format_message(
            f"⚙️ {LABELS[action]}",
            "با انتخاب گزینه، این قابلیت روشن می‌شود و در هر بار پایان زمان یا حجم، هزینه از کیف پول پرداخت خواهد شد.",
        ), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("svc:autotoggle:"))
async def svc_auto_toggle(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.service_automation import (
        ACTIONS, configure_automation, get_automation,
        owned_automation_service, selected_choice,
    )
    try:
        _, _, service_id, action = callback.data.split(":")
        service_id = int(service_id)
        if action not in ACTIONS:
            raise ValueError("تنظیم نامعتبر است")
        service = await owned_automation_service(session, db_user, service_id)
        row = await get_automation(session, service)
        enabled = not getattr(row, f"{action}_enabled")
        if enabled and await selected_choice(session, row, action) is None:
            await session.commit()
            # Open selection without enabling an incomplete configuration.
            from app.bot.handlers.reply_nav import _SoftCallback
            await svc_auto_pick(_SoftCallback(callback.message, f"svc:autopick:{service_id}:{action}"), session, db_user)
            await callback.answer()
            return
        await configure_automation(session, db_user, service_id, action, enabled=enabled)
        await _show_automation(callback, session, db_user, service_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("ذخیره شد")

@router.callback_query(F.data.startswith("svc:autoset:"))
async def svc_auto_set(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.service_automation import configure_automation
    try:
        _, _, service_id, action, choice_id = callback.data.split(":")
        service_id = int(service_id)
        await configure_automation(session, db_user, service_id, action, enabled=True, choice_id=int(choice_id))
        await _show_automation(callback, session, db_user, service_id)
    except ValueError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("انتخاب شد و قابلیت روشن شد")

@router.callback_query(F.data == "svc:list")
async def svc_list(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    await callback.answer()
    ui = await get_all_settings(session)
    result = await session.execute(
        select(UserService)
        .where(UserService.bot_user_id == db_user.id)
        .order_by(UserService.id.desc())
    )
    services = list(result.scalars().all())
    if not services:
        if callback.message:
            from app.services.rich_text import outbound_setting_text

            text, send_kw = outbound_setting_text(
                ui.get("empty_services_text")
                or "هنوز سرویسی ندارید.\nاز بخش «خرید سرویس» شروع کنید."
            )
            await safe_edit_text(
                callback.message,
                text,
                reply_markup=kb.back_home(ui),
                **send_kw,
            )
        return
    if callback.message:
        from app.bot.nav_inline import services_list_keyboard

        markup = (
            services_list_keyboard(services, ui)
        )
        await safe_edit_text(
            callback.message,
            "📦 <b>سرویس‌های شما</b>",
            reply_markup=markup,
        )

@router.callback_query(F.data.startswith("svc:view:"))
async def svc_view(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
):
    ui = await get_all_settings(session)
    svc_id = int(callback.data.split(":")[-1])
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    info = await fetch_live_service_info(svc, client_factory=get_pg)
    if info.get("status") and "expire" in info:
        from app.services.bot_user_admin import sync_service_quota_cache
        sync_service_quota_cache(svc, info=info)
        await session.commit()
    info.setdefault("username", svc.pg_username)
    text = format_message("📦 سرویس شما", service_card(info))
    if callback.message:
        from app.bot.nav_inline import service_card_keyboard

        await safe_edit_text(
            callback.message,
            text,
            reply_markup=service_card_keyboard(svc_id, ui),
        )
    if state is not None:
        from app.bot import menu_nav as nav

        await state.update_data(**{nav.SERVICE_ID: svc_id})
        await nav.set_nav_level(state, nav.NAV_SERVICE, push=True)

@router.callback_query(F.data.startswith("guide:svc:"))
async def svc_guide(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    """Open per-platform guides when the catalog has entries; else plain guide_text."""
    from app.bot.handlers.guides import _audience_for_user, show_guides_list
    from app.services.connection_guides import get_connection_guides, guides_for_audience
    from app.services.rich_text import outbound_setting_text, rich_plain_text

    try:
        svc_id = int((callback.data or "").split(":")[-1])
    except (TypeError, ValueError):
        await callback.answer("نامعتبر", show_alert=True)
        return
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    aud = await _audience_for_user(session, db_user)
    items = guides_for_audience(await get_connection_guides(session), aud)
    if items:
        await show_guides_list(callback, session, db_user, svc_id=svc_id, audience=aud)
        return
    ui = await get_all_settings(session)
    raw = ui.get("guide_text")
    if not rich_plain_text(raw).strip():
        raw = (
            "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، "
            "فیلد «متن راهنما» را پر کنید."
        )
    text, send_kw = outbound_setting_text(raw, title="📘 آموزش اتصال")
    back = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.get("btn_back") or "⬅️ بازگشت",
                    callback_data=f"svc:view:{svc_id}",
                )
            ]
        ]
    )
    await callback.answer()
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=back, **send_kw)

@router.callback_query(F.data.startswith("svc:link:"))
async def svc_link(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    from app.services.delivery import send_subscription_qr_photo
    from app.services.users import on

    ui = await get_all_settings(session)
    svc_id = int(callback.data.split(":")[-1])
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    url = svc.subscription_url or ""
    sub_info = await fetch_live_service_info(svc, client_factory=get_pg)
    parts = ["🔗 لینک و QR اشتراک"]
    if url and on(ui.get("show_sub_link_in_text", "1")):
        from app.services.formatting import copyable

        parts.append(copyable(url))
    elif not url:
        parts.append("لینک موجود نیست.")
    sub_info.setdefault("username", svc.pg_username)
    parts.append(service_card(sub_info))
    text = format_message("📱 اشتراک", "\n\n".join(parts))
    if callback.message:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=ui.get("btn_back") or "⬅️ بازگشت",
                        callback_data=f"svc:view:{svc_id}",
                    )
                ]
            ]
        )
        try:
            await safe_edit_text(callback.message, text, reply_markup=markup)
        except Exception:
            await callback.message.answer(text, reply_markup=markup)
    if url:
        await send_subscription_qr_photo(
            callback.bot,
            db_user.telegram_id,
            url,
            ui,
            info=sub_info if not sub_info.get("error") else None,
            username=svc.pg_username,
        )

@router.callback_query(F.data.startswith("svc:renew:"))
async def svc_renew(callback: CallbackQuery, session: AsyncSession, db_user: BotUser):
    ui = await get_all_settings(session)
    svc_id = int(callback.data.split(":")[-1])
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if (svc.remark or "").strip() == "linked":
        await callback.answer(
            "سرویس متصل‌شده فقط مشاهده است؛ تمدید از این مسیر ممکن نیست",
            show_alert=True,
        )
        return
    plans = await list_active_plans(session, include_trial=False)
    if not plans:
        await callback.answer("پلنی نیست", show_alert=True)
        return
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=f"{p.name} — {format_toman(p.price, get_settings().currency)}",
                callback_data=f"svc:renewpay:{svc_id}:{p.id}",
            )
        ]
        for p in plans
    ]

    rows.append(
        [
            InlineKeyboardButton(
                text=ui.get("btn_back") or "⬅️ بازگشت",
                callback_data=f"svc:view:{svc_id}",
            )
        ]
    )
    hint = "پلن تمدید را انتخاب کنید:"
    if callback.message:
        await safe_edit_text(
            callback.message,
            hint,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

@router.callback_query(F.data.startswith("svc:renewpay:"))
async def svc_renew_preview(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser, state: FSMContext
):
    from app.services.service_renewals import preview_renewal

    _, _, svc_id, plan_id = callback.data.split(":")
    svc = await session.get(UserService, int(svc_id))
    plan = await get_plan(session, int(plan_id))
    if not svc or not plan or svc.bot_user_id != db_user.id:
        await callback.answer("نامعتبر", show_alert=True)
        return
    try:
        preview = await preview_renewal(session, user_id=db_user.id, service=svc, plan=plan)
    except Exception as exc:
        await callback.answer(user_safe_error(exc), show_alert=True)
        return
    await state.update_data(renewal_preview={
        "service_id": svc.id, "plan_id": plan.id, "price": preview["price"], "terms": preview["terms"], "request_key": preview["request_key"],
    })
    lines = [f"پلن: {plan.name}", f"مبلغ: {format_toman(preview['price'], get_settings().currency)}", *preview["lines"], *preview["warnings"], "", preview["notice"]]
    if callback.message:
        await safe_edit_text(
            callback.message, format_message("🔄 پیش‌نمایش تمدید", html.escape("\n".join(lines))),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="تأیید و ادامه", callback_data=f"svc:renewconfirm:{svc.id}:{plan.id}")],
                [InlineKeyboardButton(text="انتخاب پلن دیگر", callback_data=f"svc:renew:{svc.id}")],
            ]),
        )
    await callback.answer()

@router.callback_query(F.data.startswith("svc:renewconfirm:"))
async def svc_renew_pay(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    _, _, svc_id, plan_id = callback.data.split(":")
    svc = await session.get(UserService, int(svc_id))
    plan = await get_plan(session, int(plan_id))
    if not svc or not plan or svc.bot_user_id != db_user.id:
        await callback.answer("نامعتبر", show_alert=True)
        return
    if (svc.remark or "").strip() == "linked":
        await callback.answer(
            "سرویس متصل‌شده فقط مشاهده است؛ تمدید از این مسیر ممکن نیست",
            show_alert=True,
        )
        return
    if plan.price > 0 and not kb.any_checkout_method_enabled(ui):
        await callback.answer("هیچ روش پرداختی فعال نیست", show_alert=True)
        return

    from app.services.orders import renew_service_with_plan
    from app.services.service_renewals import renewal_terms

    try:
        quote = (await state.get_data()).get("renewal_preview") or {}
        expected = {"service_id": svc.id, "plan_id": plan.id, "price": plan.price, "terms": renewal_terms(plan)}
        if not quote.get("request_key") or any(quote.get(k) != v for k, v in expected.items()):
            raise ValueError("پلن تغییر کرده یا پیش‌نمایش معتبر نیست؛ دوباره پلن تمدید را انتخاب کنید")
        order = await renew_service_with_plan(
            session,
            user_id=db_user.id,
            service=svc,
            plan=plan,
            request_key=quote["request_key"],
        )
        await state.update_data(renewal_preview=None)
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return

    await callback.answer()

    if order.amount <= 0:
        from app.services.orders import pay_with_wallet

        try:
            order = await pay_with_wallet(session, order, db_user)
        except Exception as e:
            if callback.message:
                await safe_edit_text(
                    callback.message,
                    f"❌ {user_safe_error(e)}",
                    reply_markup=None,
                )
            return
        if callback.message:
            await safe_edit_text(callback.message, 
                format_message("✅ تمدید رایگان", f"سفارش #{order.id}"),
                reply_markup=None,
            )
        return

    text = format_message(
        f"🔄 تمدید — سفارش #{order.id}",
        f"مبلغ: <b>{format_toman(order.amount, get_settings().currency)}</b>\nروش پرداخت را از کیبورد پایین انتخاب کنید:",
    )
    if callback.message:
        from app.bot.menu_nav import present_order_pay

        try:
            await safe_edit_text(callback.message, text, reply_markup=None)
        except Exception:
            await callback.message.answer(text)
        await present_order_pay(
            callback.message,
            session,
            db_user,
            order.id,
            state=state,
            text="💳 روش پرداخت را از کیبورد پایین انتخاب کنید:",
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )

@router.callback_query(F.data.startswith("svc:addon:"))
async def svc_addon(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    """List shop-scoped volume/duration packs for an owned service."""
    from app.services.service_addons import amount_label, kind_label, list_shop_packs

    ui = await get_all_settings(session)
    parts = (callback.data or "").split(":")
    # svc:addon:{svc_id} or svc:addon:{svc_id}:{kind}
    if len(parts) < 3:
        await callback.answer("نامعتبر", show_alert=True)
        return
    svc_id = int(parts[2])
    kind_filter = parts[3] if len(parts) > 3 else None
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if (svc.remark or "").strip() == "linked":
        await callback.answer(
            "سرویس متصل‌شده فقط مشاهده است؛ خرید افزونه ممکن نیست",
            show_alert=True,
        )
        return
    packs = await list_shop_packs(session, active_only=True, kind=kind_filter)
    if not packs:
        await callback.answer("بسته‌ای برای خرید فعال نیست", show_alert=True)
        return
    await callback.answer()
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{'📦' if p.kind == 'volume' else '⏱'} {p.name} "
                    f"(+{amount_label(p)}) — {format_toman(p.price, get_settings().currency)}"
                ),
                callback_data=f"svc:addonpay:{svc_id}:{p.id}",
            )
        ]
        for p in packs
    ]
    if kind_filter is None:
        has_vol = any(p.kind == "volume" for p in packs)
        has_dur = any(p.kind == "duration" for p in packs)
        filter_row = []
        if has_vol:
            filter_row.append(
                InlineKeyboardButton(
                    text="فقط حجم", callback_data=f"svc:addon:{svc_id}:volume"
                )
            )
        if has_dur:
            filter_row.append(
                InlineKeyboardButton(
                    text="فقط زمان", callback_data=f"svc:addon:{svc_id}:duration"
                )
            )
        if filter_row:
            rows.insert(0, filter_row)
    else:
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    text="همه بسته‌ها", callback_data=f"svc:addon:{svc_id}"
                )
            ],
        )
    title = "افزونه سرویس"
    if kind_filter:
        title = f"افزونه {kind_label(kind_filter)}"

    rows.append(
        [
            InlineKeyboardButton(
                text=ui.get("btn_back") or "⬅️ بازگشت",
                callback_data=f"svc:view:{svc_id}",
            )
        ]
    )
    body = "بسته را انتخاب کنید؛ پس از پرداخت به همین سرویس اضافه می‌شود."
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(f"➕ {title}", body),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

@router.callback_query(F.data.startswith("svc:addonpay:"))
async def svc_addon_pay(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    ui = await get_all_settings(session)
    parts = (callback.data or "").split(":")
    if len(parts) < 4:
        await callback.answer("نامعتبر", show_alert=True)
        return
    svc_id = int(parts[2])
    pack_id = int(parts[3])
    svc = await session.get(UserService, svc_id)
    from app.db.models import ServiceAddonPack
    from app.services.service_addons import (
        amount_label,
        create_addon_order,
        kind_label,
        pack_matches_shop,
    )
    from app.services.users import current_shop_reseller_id

    pack = await session.get(ServiceAddonPack, pack_id)
    if not svc or not pack or svc.bot_user_id != db_user.id:
        await callback.answer("نامعتبر", show_alert=True)
        return
    if not pack_matches_shop(pack, current_shop_reseller_id()):
        await callback.answer("این بسته در این فروشگاه نیست", show_alert=True)
        return
    if pack.price > 0 and not kb.any_checkout_method_enabled(ui):
        await callback.answer("هیچ روش پرداختی فعال نیست", show_alert=True)
        return
    try:
        order = await create_addon_order(
            session, user_id=db_user.id, service=svc, pack=pack
        )
    except Exception as e:
        await callback.answer(user_safe_error(e), show_alert=True)
        return
    await callback.answer()

    if order.amount <= 0:
        from app.services.orders import mark_order_free_paid, revert_failed_free_delivery
        from app.services.service_addons import apply_service_addon

        await mark_order_free_paid(session, order, db_user.id)
        try:
            order = await apply_service_addon(session, order)
        except Exception as e:
            try:
                await revert_failed_free_delivery(session, order)
            except Exception:
                pass
            if callback.message:
                await safe_edit_text(
                    callback.message,
                    f"❌ {user_safe_error(e)}",
                    reply_markup=None,
                )
            return
        if callback.message:
            await safe_edit_text(
                callback.message,
                format_message(
                    "✅ افزونه رایگان",
                    f"سفارش #{order.id}\n{kind_label(pack.kind)}: +{amount_label(pack)}",
                ),
                reply_markup=None,
            )
        return

    text = format_message(
        f"➕ افزونه — سفارش #{order.id}",
        f"{pack.name}\n"
        f"{kind_label(pack.kind)}: +{amount_label(pack)}\n"
        f"مبلغ: <b>{format_toman(order.amount, get_settings().currency)}</b>\n"
        "روش پرداخت را از کیبورد پایین انتخاب کنید:",
    )
    if callback.message:
        from app.bot.menu_nav import present_order_pay

        try:
            await safe_edit_text(callback.message, text, reply_markup=None)
        except Exception:
            await callback.message.answer(text)
        await present_order_pay(
            callback.message,
            session,
            db_user,
            order.id,
            state=state,
            text="💳 روش پرداخت را از کیبورد پایین انتخاب کنید:",
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )

@router.callback_query(F.data.startswith("svc:delask:"))
async def svc_delete_ask(
    callback: CallbackQuery, session: AsyncSession, db_user: BotUser
):
    svc_id = int(callback.data.split(":")[-1])
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    label = html.escape(svc.pg_username or f"#{svc_id}")
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🗑 بله، حذف شود",
                    callback_data=f"svc:del:{svc_id}",
                ),
                InlineKeyboardButton(
                    text="انصراف",
                    callback_data=f"svc:view:{svc_id}",
                ),
            ]
        ]
    )
    if callback.message:
        await safe_edit_text(
            callback.message,
            format_message(
                "⚠️ حذف سرویس",
                (
                    f"سرویس <b>{label}</b> برای همیشه حذف شود؟\n"
                    "لینک اشتراک از کار می‌افتد و برگشت‌ناپذیر است."
                ),
            ),
            reply_markup=markup,
        )

@router.callback_query(F.data.startswith("svc:del:"))
async def svc_delete(
    callback: CallbackQuery,
    session: AsyncSession,
    db_user: BotUser,
    state: FSMContext | None = None,
    is_reseller_bot: bool = False,
    reseller_owner_id: int | None = None,
):
    # Match svc:del:{id} only — not svc:delask:
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or parts[1] != "del":
        await callback.answer("نامعتبر", show_alert=True)
        return
    svc_id = int(parts[2])
    svc = await session.get(UserService, svc_id)
    if not svc or svc.bot_user_id != db_user.id:
        await callback.answer("یافت نشد", show_alert=True)
        return
    from app.services.bot_user_admin import admin_delete_service

    # Linked subscription shares are read-only attachments — never delete the
    # underlying PasarGuard user (could belong to an admin / another customer).
    delete_pg = (svc.remark or "").strip() != "linked"
    try:
        await admin_delete_service(session, svc, delete_pg=delete_pg)
    except Exception as e:
        await callback.answer(user_safe_error(e, limit=160), show_alert=True)
        return
    await callback.answer(
        "اتصال حذف شد" if not delete_pg else "سرویس حذف شد",
        show_alert=True,
    )
    ui = await get_all_settings(session)
    if state is not None:
        from app.bot import menu_nav as nav

        data = await state.get_data()
        if int(data.get(nav.SERVICE_ID) or 0) == svc_id:
            await state.update_data(**{nav.SERVICE_ID: None})
    if callback.message:
        from app.bot.menu_nav import restore_main_reply

        done = format_message("✅ حذف شد", f"سرویس #{svc_id} حذف شد.")
        await safe_edit_text(callback.message, done, reply_markup=None)
        # Heal main ReplyKeyboard with a real confirmation (not filler chrome).
        await restore_main_reply(
            callback.message,
            session,
            db_user,
            text="سرویس حذف شد — از منوی پایین ادامه دهید.",
            state=state,
            is_reseller_bot=is_reseller_bot,
            reseller_owner_id=reseller_owner_id,
        )
