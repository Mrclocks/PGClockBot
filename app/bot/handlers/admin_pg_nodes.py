"""Admin PasarGuard nodes — list + ops parity with web /pg/nodes."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import keyboards as kb
from app.bot.auth import can_platform_pg_action, can_platform_pg_page
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.tg_utils import parse_bot_int, safe_edit_text
from app.db.models import BotUser
from app.services.formatting import node_status_fa
from app.services.redact import user_safe_error
from app.services.pasarguard import get_pg


async def _lasting_kb(session, db_user, classic):
    from app.bot.nav_chrome import lasting_staff_reply
    return await lasting_staff_reply(
        session, db_user, classic=classic,
        is_reseller_bot=False, reseller_owner_id=None,
    )

router = Router(name="admin_pg_nodes")


async def _require_nodes(db_user: BotUser, callback=None, message=None, *, action: str | None = None) -> bool:
    """Legacy platform-admin page gate (kept for source contracts).

    Migrated node handlers must use ``_pg_object_gate`` — never this function
    as the final authorization decision.
    """
    if not _is_admin(db_user):
        if callback is not None:
            await callback.answer("ادمین نیستید", show_alert=True)
        elif message is not None:
            await message.answer("ادمین نیستید.")
        return False
    if not await can_platform_pg_page(db_user, "pg_nodes"):
        if callback is not None:
            await callback.answer("به نودها دسترسی ندارید", show_alert=True)
        elif message is not None:
            await message.answer("به نودها دسترسی ندارید.")
        return False
    if action and not await can_platform_pg_action(db_user, "nodes", action):
        if callback is not None:
            await callback.answer("اجازه این عمل را ندارید", show_alert=True)
        elif message is not None:
            await message.answer("اجازه این عمل را ندارید.")
        return False
    return True


async def _pg_object_gate(
    db_user: BotUser,
    *,
    action: str,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
    callback=None,
    message=None,
    object_id: int | None = None,
    callback_data: str | None = None,
    notify: bool = True,
):
    """Principal + AuthzContext + object-scope gate (not ``_is_admin``)."""
    from app.services.bot_pg_object_authz import authorize_bot_pg_object_op

    data = callback_data
    if data is None and callback is not None:
        data = getattr(callback, "data", None)
    gate = await authorize_bot_pg_object_op(
        session,
        db_user=db_user,
        kind="nodes",
        action=action,  # type: ignore[arg-type]
        callback_data=data,
        object_id=object_id,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
    )
    if not gate.allowed and notify:
        if callback is not None:
            await callback.answer(gate.user_message, show_alert=True)
        elif message is not None:
            await message.answer(gate.user_message)
    return gate


def _unused_owner_client_import_anchor():
    """Source contract: this module still references get_pg(); live Owner client is the Principal bridge."""
    return get_pg()


class PgNodeStates(StatesGroup):
    create_name = State()
    create_address = State()
    create_port = State()
    create_core = State()
    create_api_key = State()
    create_server_ca = State()


def _err_msg(exc: Exception) -> str:
    return user_safe_error(exc, fallback="خطا در ارتباط با پاسارگارد")



def _nodes_list_kb(items: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="♻️ اتصال مجدد همه", callback_data="adm:pg:nreconall"),
            InlineKeyboardButton(text="➕ ساخت نود", callback_data="adm:pg:ncreate"),
        ]
    ]
    for n in items[:25]:
        if not isinstance(n, dict):
            continue
        nid = n.get("id")
        if nid is None:
            continue
        name = str(n.get("name") or n.get("address") or nid)[:28]
        status = node_status_fa(n.get("status") or n.get("connection_status"))
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{nid} {name} — {status}"[:60],
                    callback_data=f"adm:pg:n:{nid}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ بازگشت", callback_data="adm:pg")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _node_detail_kb(nid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="♻️ اتصال مجدد", callback_data=f"adm:pg:recon:{nid}"),
                InlineKeyboardButton(text="🔄 همگام‌سازی", callback_data=f"adm:pg:nsync:{nid}"),
            ],
            [
                InlineKeyboardButton(text="🧹 ریست مصرف", callback_data=f"adm:pg:nresetask:{nid}"),
                InlineKeyboardButton(text="⏯ تغییر وضعیت", callback_data=f"adm:pg:ntog:{nid}"),
            ],
            [
                InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:pg:ndelask:{nid}"),
            ],
            [InlineKeyboardButton(text="⬅️ لیست نودها", callback_data="adm:pg:nodes")],
        ]
    )


def _node_detail_text(n: dict) -> str:
    nid = n.get("id")
    name = html.escape(str(n.get("name") or "—"))
    addr = html.escape(str(n.get("address") or n.get("host") or "—"))
    port = n.get("port")
    status = node_status_fa(n.get("status") or n.get("connection_status"))
    conn = html.escape(str(n.get("connection_type") or "—"))
    lines = [
        f"🕸 <b>نود #{html.escape(str(nid))}</b>",
        "",
        f"نام: <b>{name}</b>",
        f"آدرس: <code>{addr}{(':' + str(port)) if port else ''}</code>",
        f"وضعیت: <b>{html.escape(status)}</b>",
        f"اتصال: <code>{conn}</code>",
    ]
    core = n.get("core_config_id")
    if core is not None:
        lines.append(f"هسته: <code>{html.escape(str(core))}</code>")
    return "\n".join(lines)


async def _render_nodes_list(callback: CallbackQuery, gate) -> None:
    from app.services.bot_pg_object_authz import list_scoped_pg_objects

    try:
        items = await list_scoped_pg_objects(gate, kind="nodes")
    except Exception as e:
        if callback.message:
            await safe_edit_text(callback.message, f"خطا: {_err_msg(e)}", reply_markup=None)
        return
    if not isinstance(items, list):
        items = []
    lines = ["🕸 <b>نودهای پاسارگارد</b>\n"]
    if not items:
        lines.append("نودی یافت نشد.")
    else:
        for n in items[:25]:
            if not isinstance(n, dict):
                continue
            nid = n.get("id")
            name = html.escape(str(n.get("name") or n.get("address") or nid))
            status = node_status_fa(n.get("status") or n.get("connection_status"))
            addr = html.escape(str(n.get("address") or n.get("host") or "—"))
            port = n.get("port")
            loc = f"{addr}{(':' + str(port)) if port else ''}"
            lines.append(f"#{nid} <b>{name}</b> — {html.escape(status)}\n<code>{loc}</code>")
    if callback.message:
        await safe_edit_text(
            callback.message,
            "\n\n".join(lines)[:3900],
            reply_markup=_nodes_list_kb(items),
        )


@router.callback_query(F.data == "adm:pg:nodes")
async def pg_nodes(
    callback: CallbackQuery,
    db_user: BotUser,
    state: FSMContext | None = None,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="list",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    if state is not None:
        await state.set_state(None)
    await callback.answer()
    await _render_nodes_list(callback, gate)


@router.callback_query(F.data.regexp(r"^adm:pg:n:\d+$"))
async def pg_node_detail(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="read",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    n = gate.pg_object
    nid = int((n or {}).get("id") or 0)
    await callback.answer()
    if not isinstance(n, dict) or nid <= 0:
        await callback.answer("یافت نشد", show_alert=True)
        return
    if callback.message:
        await safe_edit_text(
            callback.message,
            _node_detail_text(n),
            reply_markup=_node_detail_kb(nid),
        )


@router.callback_query(F.data.startswith("adm:pg:recon:"))
async def pg_recon(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="reconnect",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed or gate.pg_client is None or gate.pg_object is None:
        return
    node_id = int(gate.pg_object.get("id") or 0)
    try:
        await gate.pg_client.reconnect_node(node_id)
        await callback.answer("درخواست اتصال مجدد ارسال شد ✅", show_alert=True)
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data == "adm:pg:nreconall")
async def pg_recon_all(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="reconnect",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed or gate.pg_client is None:
        return
    try:
        await gate.pg_client.reconnect_all_nodes()
        await callback.answer("اتصال مجدد همه نودها ارسال شد ✅", show_alert=True)
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.startswith("adm:pg:nsync:"))
async def pg_node_sync(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="reconnect",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed or gate.pg_client is None or gate.pg_object is None:
        return
    node_id = int(gate.pg_object.get("id") or 0)
    try:
        await gate.pg_client.sync_node(node_id)
        await callback.answer("همگام‌سازی انجام شد ✅", show_alert=True)
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.regexp(r"^adm:pg:nresetask:\d+$"))
async def pg_node_reset_ask(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    nid = int((gate.pg_object or {}).get("id") or 0)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"🧹 مصرف نود <b>#{nid}</b> ریست شود؟",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="✅ بله، ریست",
                            callback_data=f"adm:pg:nreset:{nid}",
                        )
                    ],
                    [InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"adm:pg:n:{nid}")],
                ]
            ),
        )


@router.callback_query(F.data.regexp(r"^adm:pg:nreset:\d+$"))
async def pg_node_reset(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed or gate.pg_client is None or gate.pg_object is None:
        return
    node_id = int(gate.pg_object.get("id") or 0)
    try:
        await gate.pg_client.reset_node(node_id)
        await callback.answer("مصرف نود ریست شد ✅", show_alert=True)
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)
        return
    if callback.message:
        try:
            n = await gate.pg_client.get_node(node_id)
            await safe_edit_text(
                callback.message,
                _node_detail_text(n) if isinstance(n, dict) else f"نود #{node_id}",
                reply_markup=_node_detail_kb(node_id),
            )
        except Exception:
            pass


@router.callback_query(F.data.regexp(r"^adm:pg:ntog:\d+$"))
async def pg_node_toggle(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_object_authz import sanitize_pg_object_write_payload

    gate = await _pg_object_gate(
        db_user,
        action="update",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed or gate.pg_client is None or gate.pg_object is None:
        return
    node_id = int(gate.pg_object.get("id") or 0)
    try:
        node = gate.pg_object
        st = str((node or {}).get("status") or "").lower()
        new_status = "connected" if st in {"disabled", "error", "limited"} else "disabled"
        await gate.pg_client.modify_node(
            node_id, sanitize_pg_object_write_payload({"status": new_status})
        )
        await callback.answer("وضعیت نود تغییر کرد ✅", show_alert=True)
        if callback.message:
            n = await gate.pg_client.get_node(node_id)
            await safe_edit_text(
                callback.message,
                _node_detail_text(n) if isinstance(n, dict) else f"نود #{node_id}",
                reply_markup=_node_detail_kb(node_id),
            )
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)


@router.callback_query(F.data.regexp(r"^adm:pg:ndelask:\d+$"))
async def pg_node_del_ask(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="delete",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    nid = int((gate.pg_object or {}).get("id") or 0)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"🗑 نود <b>#{nid}</b> حذف شود؟\nاین عمل برگشت‌ناپذیر است.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🗑 بله، حذف کن",
                            callback_data=f"adm:pg:ndel:{nid}",
                        )
                    ],
                    [InlineKeyboardButton(text="⬅️ انصراف", callback_data=f"adm:pg:n:{nid}")],
                ]
            ),
        )


@router.callback_query(F.data.regexp(r"^adm:pg:ndel:\d+$"))
async def pg_node_delete(
    callback: CallbackQuery,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="delete",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed or gate.pg_client is None or gate.pg_object is None:
        return
    node_id = int(gate.pg_object.get("id") or 0)
    try:
        await gate.pg_client.delete_node(node_id)
        await callback.answer("نود حذف شد ✅", show_alert=True)
    except Exception as e:
        await callback.answer(_err_msg(e), show_alert=True)
        return
    list_gate = await _pg_object_gate(
        db_user,
        action="list",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
        notify=False,
    )
    if list_gate.allowed:
        await _render_nodes_list(callback, list_gate)


# —— Create node wizard ——


@router.callback_query(F.data == "adm:pg:ncreate")
async def pg_node_create_start(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    await state.set_state(PgNodeStates.create_name)
    await state.update_data(node_create={})
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            "➕ <b>ساخت نود</b>\n\nنام نود را بفرستید:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ انصراف", callback_data="adm:pg:nodes")]
                ]
            ),
        )
        await callback.message.answer("نام نود:", reply_markup=kb.cancel_reply())


@router.message(PgNodeStates.create_name)
async def pg_node_create_name(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        await message.answer("انصراف.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        await state.clear()
        return
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    name = (message.text or "").strip()
    if not name:
        await message.answer("نام الزامی است.")
        return
    data = (await state.get_data()).get("node_create") or {}
    data["name"] = name
    await state.update_data(node_create=data)
    await state.set_state(PgNodeStates.create_address)
    await message.answer("آدرس نود (IP یا دامنه):", reply_markup=kb.cancel_reply())


@router.message(PgNodeStates.create_address)
async def pg_node_create_address(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        await message.answer("انصراف.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        await state.clear()
        return
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    address = (message.text or "").strip()
    if not address:
        await message.answer("آدرس الزامی است.")
        return
    data = (await state.get_data()).get("node_create") or {}
    data["address"] = address
    await state.update_data(node_create=data)
    await state.set_state(PgNodeStates.create_port)
    await message.answer(
        "پورت (عدد — یا «-» برای رد کردن):",
        reply_markup=kb.cancel_reply(),
    )


@router.message(PgNodeStates.create_port)
async def pg_node_create_port(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        await message.answer("انصراف.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        await state.clear()
        return
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    raw = (message.text or "").strip()
    data = (await state.get_data()).get("node_create") or {}
    if raw not in {"-", "—", ""}:
        try:
            data["port"] = parse_bot_int(raw)
        except ValueError:
            await message.answer("پورت نامعتبر — عدد بفرستید یا «-».")
            return
    await state.update_data(node_create=data)
    await state.set_state(None)
    await message.answer(
        "نوع اتصال را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="grpc", callback_data="adm:pg:nconn:grpc"),
                    InlineKeyboardButton(text="rest", callback_data="adm:pg:nconn:rest"),
                ],
                [InlineKeyboardButton(text="❌ انصراف", callback_data="adm:pg:nodes")],
            ]
        ),
    )


@router.callback_query(F.data.startswith("adm:pg:nconn:"))
async def pg_node_create_conn(
    callback: CallbackQuery,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        callback=callback,
    )
    if not gate.allowed:
        return
    conn = callback.data.rsplit(":", 1)[-1]
    if conn not in {"grpc", "rest"}:
        await callback.answer("نامعتبر", show_alert=True)
        return
    data = (await state.get_data()).get("node_create") or {}
    if not data.get("name") or not data.get("address"):
        await callback.answer("ابتدا ساخت نود را شروع کنید", show_alert=True)
        return
    data["connection_type"] = conn
    await state.update_data(node_create=data)
    await state.set_state(PgNodeStates.create_core)
    await callback.answer()
    if callback.message:
        await safe_edit_text(
            callback.message,
            f"نوع اتصال: <code>{conn}</code>\nشناسه هسته (core_config_id) را بفرستید یا «-»:",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="❌ انصراف", callback_data="adm:pg:nodes")]
                ]
            ),
        )
        await callback.message.answer("شناسه هسته یا «-»:", reply_markup=kb.cancel_reply())


@router.message(PgNodeStates.create_core)
async def pg_node_create_core(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        await message.answer("انصراف.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        await state.clear()
        return
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    raw = (message.text or "").strip()
    data = (await state.get_data()).get("node_create") or {}
    if raw not in {"-", "—", ""}:
        try:
            data["core_config_id"] = parse_bot_int(raw)
        except ValueError:
            await message.answer("عدد معتبر یا «-» بفرستید.")
            return
    await state.update_data(node_create=data)
    await state.set_state(PgNodeStates.create_api_key)
    await message.answer("API Key (یا «-» برای رد):", reply_markup=kb.cancel_reply())


@router.message(PgNodeStates.create_api_key)
async def pg_node_create_api_key(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    if kb.is_cancel_text(message.text):
        await message.answer("انصراف.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        await state.clear()
        return
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed:
        await state.clear()
        return
    raw = (message.text or "").strip()
    data = (await state.get_data()).get("node_create") or {}
    if raw not in {"-", "—", ""}:
        data["api_key"] = raw
    await state.update_data(node_create=data)
    await state.set_state(PgNodeStates.create_server_ca)
    await message.answer(
        "Server CA (گواهی — چندخطی مجاز؛ یا «-» برای رد):",
        reply_markup=kb.cancel_reply(),
    )


@router.message(PgNodeStates.create_server_ca)
async def pg_node_create_server_ca(
    message: Message,
    state: FSMContext,
    db_user: BotUser,
    session: AsyncSession | None = None,
    is_reseller_bot: bool = False,
    reseller_profile_id: int | None = None,
    reseller_owner_id: int | None = None,
):
    from app.services.bot_pg_object_authz import (
        list_scoped_pg_objects,
        sanitize_pg_object_write_payload,
    )

    if kb.is_cancel_text(message.text):
        await message.answer("انصراف.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        await state.clear()
        return
    gate = await _pg_object_gate(
        db_user,
        action="create",
        session=session,
        is_reseller_bot=is_reseller_bot,
        reseller_profile_id=reseller_profile_id,
        reseller_owner_id=reseller_owner_id,
        message=message,
    )
    if not gate.allowed or gate.pg_client is None:
        await state.clear()
        return
    raw = (message.text or "").strip()
    data = dict((await state.get_data()).get("node_create") or {})
    if raw not in {"-", "—", ""}:
        data["server_ca"] = raw
    name = str(data.get("name") or "").strip()
    address = str(data.get("address") or "").strip()
    if not name or not address:
        await state.set_state(None)
        await message.answer("اطلاعات ناقص — از ابتدا شروع کنید.", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
        return
    payload: dict = {
        "name": name,
        "address": address,
        "connection_type": data.get("connection_type") or "grpc",
    }
    if data.get("port") is not None:
        payload["port"] = int(data["port"])
    if data.get("api_key"):
        payload["api_key"] = data["api_key"]
    if data.get("server_ca"):
        payload["server_ca"] = data["server_ca"]
    if data.get("core_config_id") is not None:
        payload["core_config_id"] = int(data["core_config_id"])
    payload = sanitize_pg_object_write_payload(payload)
    await state.set_state(None)
    try:
        await gate.pg_client.create_node(payload)
    except Exception as e:
        await message.answer(
            f"ساخت نود ناموفق: {_err_msg(e)}\n"
            "علت محتمل: فیلدهای الزامی ناقص (api_key/گواهی).",
            reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()),
        )
        return
    await message.answer("نود ساخته شد ✅", reply_markup=await _lasting_kb(session, db_user, kb.admin_product_reply_keyboard()))
    try:
        items = await list_scoped_pg_objects(gate, kind="nodes")
        await message.answer(
            "🕸 <b>نودهای پاسارگارد</b>\n\nلیست به‌روز شد.",
            reply_markup=_nodes_list_kb(items),
        )
    except Exception as e:
        await message.answer(f"نود ساخته شد — لیست: {_err_msg(e)}")
