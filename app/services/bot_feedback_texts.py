"""Short feedback texts that many bot handlers send (alerts, toasts and replies).

One definition per text keeps the wording identical everywhere it appears, so
a later rewording changes every screen at once and shows up in the bot UI
snapshots. These texts are fixed; admin-editable texts live in settings.
Business-rule errors raised by services keep their own messages there.
"""

from __future__ import annotations

# Access
NOT_ADMIN = "این بخش فقط برای ادمین است"
NOT_ADMIN_REPLY = "این بخش فقط برای ادمین است."
NOT_RESELLER = "نماینده نیستید"
RESELLERS_ONLY = "فقط نمایندگان"
NO_ACCESS = "دسترسی ندارید"
NO_ACCESS_REPLY = "دسترسی ندارید."
ACTION_NOT_ALLOWED = "اجازه این عمل را ندارید"
ACTION_NOT_ALLOWED_REPLY = "اجازه این عمل را ندارید."
SYSTEM_OWNER_REQUIRED = "این کار فقط با دسترسی مالک سیستم ممکن است"
RESELLER_ACCESS_NOT_FOUND = "دسترسی نماینده یافت نشد."
NO_SHOP_SETTINGS_ACCESS = "دسترسی تنظیمات فروشگاه ندارید."
REPRESENTATIVES_DISABLED = "قابلیت ساخت نماینده برای این حساب فعال نیست"
PREVIEW_ONLY_ON_SHOP_BOT = "پیش‌نمایش فقط روی ربات فروشگاه شما فعال است."
PG_ADMIN_NOT_SET = "حساب ادمین پاسارگارد تنظیم نشده است"
BOT_TOKEN_NOT_SET = "توکن ربات ثبت نشده است"

# Missing or invalid items
NOT_FOUND = "یافت نشد"
INVALID = "نامعتبر"
CATEGORY_NOT_FOUND = "دسته یافت نشد."
CATEGORY_INVALID = "دسته نامعتبر است."
NO_PLANS = "پلنی نیست"
NO_PLANS_IN_CATEGORY = "پلنی در این دسته نیست."
NO_PLANS_FOR_SALE = "در حال حاضر پلنی برای فروش فعال نیست."
NO_RESELLER_PLANS = "در حال حاضر پلن نمایندگی فعالی تعریف نشده است.\nبعداً دوباره بررسی کنید."
NO_GROUPS = "هنوز گروهی ساخته نشده است"
NO_TEMPLATES = "هنوز تمپلیتی ساخته نشده است"
NO_PAYMENT_METHOD = "هیچ روش پرداختی فعال نیست"
NO_PENDING_RECEIPT = "رسید معلقی نیست."
NO_PENDING_CANCELLATION = "درخواست لغو معلقی نیست."
NO_SERVICES_YET = "هنوز سرویسی ندارید.\nاز بخش «خرید سرویس» شروع کنید."
NOT_CREATED_YET = "هنوز ساخته نشده."
NO_TICKETS = "تیکتی ندارید."
TICKET_CLOSED = "تیکت بسته است"
TICKET_ALREADY_CLOSED = "تیکت قبلاً بسته شده است."
ALREADY_CLOSED = "قبلاً بسته شده"
GUIDE_TEXT_NOT_SET = (
    "متنی برای راهنما تنظیم نشده. از وب‌پنل → تنظیمات ربات → متن‌ها، فیلد «متن راهنما» را پر کنید."
)
JOIN_NOT_CONFIRMED = "عضویت تأیید نشد — ربات باید ادمین کانال باشد"

# Input prompts and validation
ENTER_VALID_NUMBER = "عدد معتبر بفرستید."
ENTER_VALID_NUMBER_SHORT = "عدد معتبر بفرستید"
ENTER_ONE_VALID_NUMBER = "یک عدد معتبر بفرستید."
NAME_REQUIRED = "نام را خالی نگذارید."
PICK_AT_LEAST_ONE_GROUP = "حداقل یک گروه انتخاب کنید"
START_PLAN_CREATION_FIRST = "اول ساخت پلن را از «افزودن پلن» شروع کنید"
PICK_SECTION_BELOW = "یک بخش را از دکمه‌های زیر انتخاب کنید."
PICK_OPTION_BELOW = "یک گزینه را از دکمه‌های زیر انتخاب کنید."
PICK_CATEGORY_BELOW = "ابتدا <b>دسته</b> را از دکمه‌های زیر پیام انتخاب کنید."
PICK_TEMPLATE = "تمپلیت را انتخاب کنید:"
PICK_GROUPS = "گروه(ها) را انتخاب کنید:"
PICK_CARD = "یکی از کارت‌ها را انتخاب کنید:"
PICK_GATEWAY = "یکی از درگاه‌ها را انتخاب کنید:"
PICK_AMOUNT = "مبلغ را انتخاب کنید یا «مبلغ دیگر» را بزنید:"
PICK_PLAN_TYPE = "➕ <b>افزودن پلن</b>\nنوع پلن را انتخاب کنید:"
ENTER_PLAN_NAME = "نام پلن را بفرستید:"
ENTER_PRICE_TOMAN = "قیمت را به تومان بفرستید:"
ENTER_VOLUME_GB = "حجم را به گیگابایت بفرستید.\nبرای نامحدود <code>0</code> بفرستید."
ENTER_NEW_TEXT = "متن جدید را بفرستید.\nبرای انصراف: انصراف"

# Results
CANCELLED = "لغو شد."
CANCELLED_SHORT = "لغو شد"
CANCELLED_BACK_TO_HUB = "لغو شد. به پنل مدیریت برگشتید."
SAVED = "ذخیره شد"
SAVED_CHECK = "ذخیره شد ✅"
SAVED_WITH_ICON = "✅ ذخیره شد."
COLOR_SAVED = "رنگ ذخیره شد"
UPDATED = "به‌روز شد"
DELETED = "حذف شد"
CONNECTION_DELETED = "اتصال حذف شد"
REJECTED = "رد شد"
RENEWED = "تمدید شد"
SENT = "ارسال شد ✅"
MESSAGE_SENT = "پیام ارسال شد ✅"
TICKET_CLOSED_DONE = "تیکت بسته شد ✅"
MAIN_KEYBOARD_RESTORED = "کیبورد اصلی بازیابی شد."
