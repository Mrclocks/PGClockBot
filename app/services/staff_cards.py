"""Shared Telegram HTML cards for staff customer views (platform admin + shop bot).

These strings are fixed product copy — not admin-editable settings — so they live
here once and keep the bot screens consistent and readable.
"""

from __future__ import annotations

import html
from typing import Sequence

from app.services.formatting import (
    copyable,
    format_message,
    info_block,
    kv_line,
    rtl_text,
)


def role_label_fa(role: str | None) -> str:
    """Persian label for a BotUser.role value."""
    key = (role or "").strip()
    return {
        "user": "کاربر",
        "reseller": "نماینده",
        "admin": "مدیر",
        "pg_staff": "ادمین پاسارگارد",
    }.get(key, key or "—")


def format_staff_customer_card(
    *,
    flags: str,
    display_name: str,
    telegram_id: int,
    username: str | None,
    wallet_text: str,
    service_count: int,
    is_blocked: bool,
    wallet_label: str = "کیف پول",
    role: str | None = None,
    risk_tag: str | None = None,
    alert_line: str = "",
    service_summary: Sequence[str] | None = None,
) -> str:
    """Pretty profile card for a customer the staff member may manage."""
    name = html.escape((display_name or "").strip() or "—")
    title = f"{flags} {name}".strip()
    rows = [
        kv_line("🆔", "آیدی تلگرام", copyable(telegram_id)),
        kv_line(
            "👤",
            "یوزرنیم",
            copyable(f"@{username}") if username else "—",
        ),
    ]
    if role is not None:
        rows.append(kv_line("🏷", "نقش", html.escape(role_label_fa(role))))
    if risk_tag is not None:
        rows.append(kv_line("🎯", "تگ ریسک", html.escape(risk_tag)))
    rows.extend(
        [
            kv_line("💰", wallet_label, wallet_text),
            kv_line("📦", "سرویس‌ها", str(int(service_count))),
            kv_line("🚫", "مسدود", "بله" if is_blocked else "خیر"),
        ]
    )
    body_parts = [info_block(rows)]
    if alert_line:
        body_parts.append(alert_line.strip())
    if service_summary:
        body_parts.append("━━━━━━━━━━━━")
        body_parts.append("<b>سرویس‌ها</b>")
        body_parts.append("\n\n".join(service_summary))
    return format_message(title, "\n\n".join(p for p in body_parts if p))


def format_subscription_owner_card(
    *,
    display_name: str,
    username: str | None,
    telegram_id: int,
    wallet_text: str,
    is_blocked: bool,
    service_username: str,
    service_id: int,
    plan_name: str | None,
    status_text: str,
    quota_lines: Sequence[str] | None = None,
) -> str:
    """Result card after staff search by subscription link."""
    name = html.escape(display_name)
    uname = f" (@{html.escape(username)})" if username else ""
    rows = [
        kv_line("👤", "مشتری", f"<b>{name}</b>{uname}"),
        kv_line("🆔", "آیدی تلگرام", copyable(telegram_id)),
        kv_line("💰", "کیف پول", wallet_text),
        kv_line("🚫", "مسدود", "بله" if is_blocked else "خیر"),
        "",
        kv_line("📦", "سرویس", f"{copyable(service_username)} (#{int(service_id)})"),
        kv_line("📋", "پلن", html.escape(plan_name) if plan_name else "—"),
        kv_line("📶", "وضعیت", html.escape(status_text)),
    ]
    for line in quota_lines or ():
        rows.append(line)
    return format_message("🔎 صاحب اشتراک", info_block(rows))


def format_services_list_header(user_id: int, *, subject: str = "مشتری") -> str:
    label = (subject or "مشتری").strip() or "مشتری"
    return rtl_text(f"📦 <b>سرویس‌های {html.escape(label)} #{int(user_id)}</b>")


def format_adjust_prompt(service_id: int, *, days: int, gb: float) -> str:
    gb_disp = int(gb) if float(gb) == int(gb) else gb
    body = info_block(
        [
            kv_line("📅", "روز", f"<b>{days}</b>"),
            kv_line("📦", "گیگ", f"<b>{gb_disp}</b>"),
            "",
            "مثبت = افزایش، منفی = کاهش.",
            "برای ورود دستی روی عدد بزنید.",
        ]
    )
    return format_message(f"⏱ تغییر مانده سرویس #{int(service_id)}", body)
