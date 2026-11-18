"""Customer message texts offered in the owner and shop bot settings.

The same keys and labels as the web «متن پیام‌ها» tab, grouped into screens of a few fields so a
bot settings screen stays short. Both bots list these; the rules for saving a text (rich text and
premium emoji) are the ones every other text field uses.
"""

from __future__ import annotations

# (key, short label, kind) — the shape of every bot settings field.
Field = tuple[str, str, str]

CUSTOMER_GUIDE_TEXTS: list[Field] = [
    ("guide_text", "متن راهنما (/help)", "textarea"),
    ("faq_text", "متن سوالات متداول", "textarea"),
    ("empty_services_text", "وقتی سرویسی ندارد", "textarea"),
    ("shop_empty_text", "وقتی پلنی نیست", "textarea"),
]

CUSTOMER_RESULT_TEXTS: list[Field] = [
    ("delivery_title", "عنوان پیام تحویل سرویس", "text"),
    ("payment_ok_title", "عنوان تأیید پرداخت", "text"),
    ("wallet_success_title", "عنوان شارژ کیف پول", "text"),
    ("wallet_success_text", "متن شارژ کیف پول", "textarea"),
]

# What customers see on «وضعیت اتصال»; the admin prompts of monitoring belong to the main bot's Owner.
CONNECTION_STATUS_CUSTOMER_TEXTS: list[Field] = [
    ("connection_status_title", "عنوان وضعیت اتصال", "text"),
    ("connection_status_empty", "فهرست وضعیت خالی", "textarea"),
    ("connection_status_error", "خطای دریافت وضعیت", "textarea"),
]

# The bot's own name and descriptions in Telegram; saving pushes them to Telegram (bot_appearance).
PROFILE_BOT_FIELDS: list[Field] = [
    ("bot_tg_name", "نام ربات", "text"),
    ("bot_tg_short_description", "کپشن / درباره (پروفایل)", "text"),
    ("bot_tg_description", "متن وسط صفحهٔ چت خالی", "textarea"),
]

# Subsections of «نام و متن‌ها»: (sub id, title, fields).
MESSAGE_TEXT_SUBS: list[tuple[str, str, list[Field]]] = [
    ("guide_texts", "راهنما و حالت خالی", CUSTOMER_GUIDE_TEXTS),
    ("result_texts", "تحویل، پرداخت و کیف پول", CUSTOMER_RESULT_TEXTS),
]
