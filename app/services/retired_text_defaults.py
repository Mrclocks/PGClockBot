"""Default texts shipped by earlier releases, so untouched copies follow the current wording.

Defaults are written to the database on first start. A stored value that still equals an
earlier default was never edited, so it is shown with the current default instead; a value
an admin or shop changed stays as it is. Old button labels also keep matching the button on
keyboards that were sent before the rewording.
"""

from __future__ import annotations

from collections.abc import Mapping

RETIRED_TEXT_DEFAULTS: dict[str, tuple[str, ...]] = {
    "btn_adm_hub_product": ("📦 محصول و PG",),
    "btn_adm_res_apps": ("📋 درخواست‌های منتظر",),
    "btn_adm_res_add": ("➕ افزودن دستی",),
    "btn_adm_plans_addons": ("⏱ بسته حجم/زمان",),
    "btn_adm_reports": ("📈 گزارشات",),
    "screen_title_a_product": ("📦 محصول و PG",),
    "screen_title_a_users": ("👥 کاربران بات",),
    "screen_caption_a_home": ("یک گروه را از دکمه‌های زیر انتخاب کنید.",),
    "screen_caption_a_people": ("کاربران و نمایندگان.",),
    "screen_caption_a_product": ("پلن‌ها و عملیات پاسارگارد.",),
    "screen_caption_a_system": ("تنظیمات، بکاپ و پیش‌نمایش.",),
    "screen_caption_a_settings": (
        (
        "یک بخش را از دکمه‌های زیر انتخاب کنید.\n"
        "ظاهر، رنگ، گزارش روزانه → وب‌پنل."
        ),
    ),
    "screen_caption_a_backup": (
        (
            "بکاپ کامل = دیتابیس کل پلتفرم (همه فروشگاه‌ها) + آپلودها + یوزر وب.\n"
            "پیش‌فرض بدون .env است (مثل زمان‌بندی).\n"
            "بکاپ با .env فقط بعد از تأیید امنیتی جداگانه — توکن/اسرار داخل فایل می‌رود.\n"
            "حتی بدون .env، فایل ورود وب‌پنل در بکاپ هست.\n"
            "قبل از ریستور، بکاپ ایمنی خودکار (با .env) ساخته می‌شود."
        ),
    ),
    "screen_caption_a_broadcast": ("مخاطب را از دکمه‌های زیر انتخاب کنید:",),
    "screen_caption_a_plans": ("مخاطب یا ابزار کاتالوگ را از دکمه‌های زیر انتخاب کنید.",),
    "screen_caption_a_plans_users": ("فهرست پلن‌ها را با «مشاهده پلن‌ها» ببینید؛ افزودن/کاتالوگ از دکمه‌های زیر.",),
    "screen_caption_a_plans_resellers": ("فهرست پلن‌ها را با «مشاهده پلن‌ها» ببینید؛ افزودن/کاتالوگ از دکمه‌های زیر.",),
}


def refresh_retired_defaults(settings: dict[str, str], defaults: Mapping[str, str]) -> dict[str, str]:
    """Replace each stored value that is still an earlier default with the current default."""
    for key, previous in RETIRED_TEXT_DEFAULTS.items():
        if key in defaults and settings.get(key) in previous:
            settings[key] = defaults[key]
    return settings


def retired_labels(key: str) -> tuple[str, ...]:
    """Earlier default labels of a button, still accepted from keyboards sent before."""
    return RETIRED_TEXT_DEFAULTS.get(key, ())
