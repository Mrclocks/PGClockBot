"""Admin backup / restore via Telegram bot."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from app.bot import keyboards as kb
from app.bot.auth import is_platform_admin as _is_admin
from app.bot.auth import require_bot_owner_handler
from app.bot.tg_utils import safe_edit_text
from app.db.models import BotUser
from app.services.redact import user_safe_error
from app.services.backup import (
    create_backup,
    delete_backup,
    get_backup_path,
    list_backups,
    restore_backup,
    save_uploaded_backup,
)
from app.services.updates import local_version

router = Router(name="admin_backup")


class BackupStates(StatesGroup):
    waiting_upload = State()
    confirm_restore = State()


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _hub_keyboard(backups: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Dynamic backup file rows only — static actions live on reply keyboard."""
    return kb.backup_files_keyboard(backups)


def _item_keyboard(backup_id: str) -> InlineKeyboardMarkup:
    """Per-file actions only — hub/list nav via reply keyboard."""
    return _kb(
        [
            [InlineKeyboardButton(text="⬇️ دریافت فایل", callback_data=f"adm:backup:dl:{backup_id}")],
            [
                InlineKeyboardButton(
                    text="♻️ ریستور (+.env)",
                    callback_data=f"adm:backup:restore:{backup_id}:1",
                )
            ],
            [
                InlineKeyboardButton(
                    text="♻️ ریستور (بدون .env)",
                    callback_data=f"adm:backup:restore:{backup_id}:0",
                )
            ],
            [InlineKeyboardButton(text="🗑 حذف", callback_data=f"adm:backup:del:{backup_id}")],
        ]
    )


def _hub_text(backups: list[dict]) -> str:
    lines = [
        "💾 <b>بکاپ / ریستور</b>",
        f"نسخه: <code>{local_version()}</code>",
        "",
        "بکاپ کامل شامل دیتابیس، آپلودها، یوزر وب و (اختیاری) فایل .env است.",
        "قبل از ریستور، بکاپ ایمنی خودکار ساخته می‌شود.",
        "",
    ]
    if not backups:
        lines.append("هنوز بکاپی نیست.")
    else:
        lines.append(f"تعداد: <b>{len(backups)}</b> (نمایش ۸ مورد اخیر)")
        for b in backups[:8]:
            env = " · .env" if b.get("include_env") else ""
            lines.append(
                f"• <code>{b['id']}</code> — {b.get('size_human')} — v{b.get('app_version')}{env}"
            )
    return "\n".join(lines)


@router.callback_query(F.data == "adm:backup")
@require_bot_owner_handler
async def backup_hub(callback: CallbackQuery, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await state.clear()
    await callback.answer()
    backups = await asyncio.to_thread(list_backups)
    if callback.message:
        await safe_edit_text(
            callback.message,
            _hub_text(backups),
            reply_markup=_hub_keyboard(backups),
        )


@router.callback_query(F.data.startswith("adm:backup:create"))
@require_bot_owner_handler
async def backup_create(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    include_env = not callback.data.endswith(":noenv")
    await callback.answer("در حال ساخت…")
    try:
        result = await asyncio.to_thread(
            create_backup,
            note="telegram admin",
            include_env=include_env,
            created_by=f"tg:{db_user.telegram_id}",
        )
    except Exception as e:
        if callback.message:
            await callback.message.answer(f"❌ ساخت بکاپ ناموفق:\n{user_safe_error(e)}")
        return
    path = Path(result["path"])
    caption = (
        f"✅ بکاپ آماده\n"
        f"<code>{result['filename']}</code>\n"
        f"حجم: {result['size_human']}\n"
        f"نسخه: {result.get('app_version')}\n"
        f".env: {'بله' if result.get('include_env') else 'خیر'}"
    )
    if callback.message:
        try:
            await callback.message.answer_document(
                FSInputFile(path, filename=path.name),
                caption=caption,
            )
        except Exception:
            await callback.message.answer(
                caption + f"\n\nدانلود از وب‌پنل: تنظیمات ← بکاپ\nمسیر: <code>{path}</code>"
            )
        backups = await asyncio.to_thread(list_backups)
        await callback.message.answer(
            _hub_text(backups),
            reply_markup=_hub_keyboard(backups),
        )


@router.callback_query(F.data.startswith("adm:backup:item:"))
@require_bot_owner_handler
async def backup_item(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    backup_id = callback.data.split(":", 3)[-1]
    path = get_backup_path(backup_id)
    if not path:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    backups = {b["id"]: b for b in await asyncio.to_thread(list_backups)}
    b = backups.get(backup_id) or {"id": backup_id, "filename": path.name}
    text = (
        f"📦 <b>بکاپ</b>\n"
        f"<code>{b.get('filename') or path.name}</code>\n"
        f"شناسه: <code>{backup_id}</code>\n"
        f"حجم: {b.get('size_human', '—')}\n"
        f"نسخه: {b.get('app_version', '—')}\n"
        f".env: {'بله' if b.get('include_env') else 'خیر'}\n"
        f"{b.get('note') or ''}"
    )
    if callback.message:
        await safe_edit_text(callback.message, text, reply_markup=_item_keyboard(backup_id))


@router.callback_query(F.data.startswith("adm:backup:dl:"))
@require_bot_owner_handler
async def backup_download(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    backup_id = callback.data.split(":", 3)[-1]
    path = get_backup_path(backup_id)
    if not path:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer("ارسال فایل…")
    if callback.message:
        try:
            await callback.message.answer_document(
                FSInputFile(path, filename=path.name),
                caption=f"⬇️ {path.name}",
            )
        except Exception as e:
            await callback.message.answer(
                f"❌ ارسال ممکن نشد (حجم زیاد؟):\n{user_safe_error(e)}"
            )


@router.callback_query(F.data.startswith("adm:backup:del:"))
@require_bot_owner_handler
async def backup_delete(callback: CallbackQuery, db_user: BotUser):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    backup_id = callback.data.split(":", 3)[-1]
    ok = await asyncio.to_thread(delete_backup, backup_id)
    await callback.answer("حذف شد" if ok else "ناموفق", show_alert=not ok)
    backups = await asyncio.to_thread(list_backups)
    if callback.message:
        await safe_edit_text(
            callback.message,
            _hub_text(backups),
            reply_markup=_hub_keyboard(backups),
        )


@router.callback_query(F.data.startswith("adm:backup:restore:"))
@require_bot_owner_handler
async def backup_restore_ask(callback: CallbackQuery, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    # adm:backup:restore:{id}:{0|1}
    parts = callback.data.split(":")
    backup_id = parts[3]
    restore_env = parts[4] == "1" if len(parts) > 4 else True
    path = get_backup_path(backup_id)
    if not path:
        await callback.answer("یافت نشد", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BackupStates.confirm_restore)
    await state.update_data(backup_id=backup_id, restore_env=restore_env)
    if callback.message:
        await callback.message.answer(
            "⚠️ ریستور همه داده‌های فعلی را جایگزین می‌کند.\n"
            f"بکاپ: <code>{backup_id}</code>\n"
            f".env: {'بله' if restore_env else 'خیر'}\n\n"
            "برای تأیید همین پیام را بفرستید:\n<code>RESTORE</code>\n"
            "برای انصراف: انصراف",
            reply_markup=kb.cancel_reply(),
        )


@router.message(BackupStates.confirm_restore)
@require_bot_owner_handler
async def backup_restore_confirm(message: Message, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await state.clear()
        return
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_system_reply_keyboard())
        return
    if text != "RESTORE":
        await message.answer(
            "برای تأیید دقیقاً <code>RESTORE</code> را بفرستید یا انصراف.",
            reply_markup=kb.cancel_reply(),
        )
        return
    data = await state.get_data()
    await state.clear()
    backup_id = data.get("backup_id")
    restore_env = bool(data.get("restore_env"))
    path = get_backup_path(str(backup_id or ""))
    if not path:
        await message.answer("بکاپ یافت نشد.")
        return
    await message.answer("⏳ در حال ریستور… سرویس ممکن است ری‌استارت شود.")
    from app.db.session import engine

    await engine.dispose()
    result = await asyncio.to_thread(
        restore_backup,
        path,
        restore_env=restore_env,
        safety_backup=True,
        restart=True,
        actor=f"tg:{db_user.telegram_id}",
    )
    if not result.get("ok"):
        await message.answer(f"❌ ریستور ناموفق:\n{result.get('error')}")
        return
    msg = "✅ ریستور انجام شد."
    if result.get("safety_id"):
        msg += f"\nبکاپ ایمنی: <code>{result['safety_id']}</code>"
    if result.get("restart_scheduled"):
        msg += "\nسرویس در حال راه‌اندازی مجدد است."
    await message.answer(msg)


@router.callback_query(F.data == "adm:backup:upload")
@require_bot_owner_handler
async def backup_upload_ask(callback: CallbackQuery, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await callback.answer("ادمین نیستید", show_alert=True)
        return
    await callback.answer()
    await state.set_state(BackupStates.waiting_upload)
    if callback.message:
        await callback.message.answer(
            "فایل ZIP بکاپ را در همین گفتگو بفرستید.",
            reply_markup=kb.cancel_reply(),
        )


@router.message(BackupStates.waiting_upload, F.document)
@require_bot_owner_handler
async def backup_upload_file(message: Message, db_user: BotUser, state: FSMContext):
    if not _is_admin(db_user):
        await state.clear()
        return
    doc = message.document
    if not doc:
        return
    name = (doc.file_name or "").lower()
    if not name.endswith(".zip"):
        await message.answer("فقط فایل .zip بکاپ پذیرفته می‌شود.")
        return
    await message.answer("⏳ در حال دریافت و بررسی…")
    try:
        buf = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        tmp_path = Path(buf.name)
        buf.close()
        await message.bot.download(doc, destination=tmp_path)
        content = tmp_path.read_bytes()
        tmp_path.unlink(missing_ok=True)
        result = await asyncio.to_thread(
            save_uploaded_backup,
            content,
            filename=doc.file_name or "",
        )
    except Exception as e:
        await state.clear()
        await message.answer(f"❌ آپلود ناموفق:\n{user_safe_error(e)}")
        return
    await state.clear()
    if not result.get("ok"):
        await message.answer(f"❌ {result.get('error')}")
        return
    await message.answer(
        f"✅ آپلود شد: <code>{result.get('filename')}</code>\n"
        f"شناسه: <code>{result.get('id')}</code>",
        reply_markup=_item_keyboard(result["id"]),
    )


@router.message(BackupStates.waiting_upload)
async def backup_upload_cancel(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if kb.is_cancel_text(text):
        await state.clear()
        await message.answer("لغو شد.", reply_markup=kb.admin_system_reply_keyboard())
        return
    await message.answer("یک فایل ZIP بفرستید یا انصراف.", reply_markup=kb.cancel_reply())
