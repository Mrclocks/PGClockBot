"""Text and keyboards for the admin gift-code and campaign panels (pure, no I/O besides the panel URL)."""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Mapping, Sequence

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot import keyboards as kb
from app.bot.keyboards import _ikb, _style
from app.db.models import CashbackRule, ChargeCode, TargetedCampaign
from app.services.campaigns import AUDIENCES
from app.services.cashback_rules import CashbackSettings
from app.services.formatting import format_number, format_toman

logger = logging.getLogger(__name__)

MARKETING_HUB_CALLBACK = "nv:adm:marketing"
GIFT_PANEL_CALLBACK = "nv:adm:gc"
CAMPAIGN_PANEL_CALLBACK = "nv:adm:cmp"
CASHBACK_PANEL_CALLBACK = "nv:adm:cb"
GIFT_SET_PATTERN = re.compile(r"^adm:gc:set:(\d{1,9}):([01])\Z")
CAMPAIGN_ACTION_PATTERN = re.compile(r"^adm:cmp:(start|pause):(\d{1,9})\Z")
CASHBACK_FEATURE_PATTERN = re.compile(r"^adm:cb:en:([01])\Z")
CASHBACK_RULE_PATTERN = re.compile(r"^adm:cb:set:(\d{1,9}):([01])\Z")
# Shop-bot mirrors (same actions, scoped by the reseller actor).
SHOP_GIFT_PANEL_CALLBACK = "res:gc"
SHOP_CAMPAIGN_PANEL_CALLBACK = "res:cmp"
SHOP_GIFT_SET_PATTERN = re.compile(r"^res:gc:set:(\d{1,9}):([01])\Z")
SHOP_CAMPAIGN_ACTION_PATTERN = re.compile(r"^res:cmp:(start|pause):(\d{1,9})\Z")
SHOP_CASHBACK_PANEL_CALLBACK = "res:cb"
SHOP_CASHBACK_FEATURE_PATTERN = re.compile(r"^res:cb:en:([01])\Z")
SHOP_CASHBACK_RULE_PATTERN = re.compile(r"^res:cb:set:(\d{1,9}):([01])\Z")

WEB_BUTTON_LABEL = "🌐 مدیریت کامل در وب‌پنل"

CAMPAIGN_STATUS_LABELS = {
    "draft": "پیش‌نویس",
    "running": "در حال ارسال",
    "paused": "متوقف",
    "completed": "تکمیل‌شده",
    "review": "نیازمند بررسی",
}
PRICE_ANNOUNCEMENT_AUDIENCE = "catalog_discount"
PRICE_ANNOUNCEMENT_LABEL = "اطلاع‌رسانی تغییر قیمت"


def panel_page_url(path: str) -> str | None:
    """Absolute panel URL for a Telegram URL button, or ``None`` when no public base is configured."""
    from app.services.setup_wizard import default_panel_base_url

    try:
        base = (default_panel_base_url() or "").rstrip("/")
    except Exception:  # best effort: the panel works without the shortcut button
        logger.warning("panel base url unavailable", exc_info=True)
        return None
    return f"{base}{path}" if base.startswith("http") else None


def _footer_rows(
    ui: Mapping | None,
    web_url: str | None,
    *,
    back_callback: str = MARKETING_HUB_CALLBACK,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if web_url:
        rows.append([_ikb(WEB_BUTTON_LABEL, url=web_url, ui=dict(ui or {}))])
    rows.append([kb.back_button(dict(ui or {}), back_callback)])
    return rows


def _gift_amount(code: ChargeCode) -> str:
    if code.kind == "discount":
        return f"تخفیف {format_number(int(code.percent or 0))}٪"
    return f"شارژ {format_toman(int(code.amount or 0))}"


def gift_codes_panel(
    codes: Sequence[ChargeCode],
    details: Mapping[int, Mapping[str, str]],
    *,
    ui: Mapping | None = None,
    web_url: str | None = None,
    action_prefix: str = "adm",
    back_callback: str = MARKETING_HUB_CALLBACK,
) -> tuple[str, InlineKeyboardMarkup]:
    """Code list with an on/off button each; ``action_prefix`` is ``adm`` (platform) or ``res`` (shop bot)."""
    lines = ["🎁 <b>کدهای هدیه</b>", "با دکمه‌ی هر کد، آن را روشن یا خاموش کنید.", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for code in codes:
        state = details.get(code.id, {}).get("state") or ("فعال" if code.is_active else "خاموش")
        uses = f"{code.used_count}/{code.max_uses}" if code.max_uses is not None else str(code.used_count)
        lines.append(
            f"<code>{html.escape(code.code)}</code> — {_gift_amount(code)} — {html.escape(state)} — مصرف {uses}"
        )
        target = 0 if code.is_active else 1
        icon = "⏸ خاموش" if code.is_active else "▶️ روشن"
        rows.append(
            [
                _ikb(
                    f"{icon}: {code.code}",
                    callback_data=f"{action_prefix}:gc:set:{code.id}:{target}",
                    ui=ui,
                    style=_style(dict(ui or {}), "reject" if code.is_active else "confirm"),
                )
            ]
        )
    if not codes:
        lines.append("هنوز کدی ساخته نشده. ساخت کد از وب‌پنل انجام می‌شود.")
    rows.extend(_footer_rows(ui, web_url, back_callback=back_callback))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _campaign_title(campaign: TargetedCampaign) -> str:
    if campaign.audience == PRICE_ANNOUNCEMENT_AUDIENCE:
        return PRICE_ANNOUNCEMENT_LABEL
    return AUDIENCES.get(campaign.audience, "کمپین")


def campaigns_panel(
    campaigns: Sequence[TargetedCampaign],
    progress: Mapping[int, Mapping[str, int]],
    *,
    ui: Mapping | None = None,
    web_url: str | None = None,
    action_prefix: str = "adm",
    back_callback: str = MARKETING_HUB_CALLBACK,
) -> tuple[str, InlineKeyboardMarkup]:
    """Recent campaigns with start/pause; ``action_prefix`` is ``adm`` (platform) or ``res`` (shop bot)."""
    lines = ["📣 <b>کمپین‌های هدفمند</b>", "وضعیت آخرین کمپین‌ها؛ ساخت کمپین از وب‌پنل انجام می‌شود.", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for campaign in campaigns:
        counts = progress.get(campaign.id, {})
        total = sum(counts.values())
        status = CAMPAIGN_STATUS_LABELS.get(campaign.status, "نامشخص")
        lines.append(
            f"#{campaign.id} — {html.escape(_campaign_title(campaign))} — {status}"
            f" — {format_number(counts.get('sent', 0))}/{format_number(total)} ارسال‌شده"
        )
        if campaign.status in {"draft", "paused"}:
            rows.append(
                [
                    _ikb(
                        f"▶️ شروع/ادامه #{campaign.id}",
                        callback_data=f"{action_prefix}:cmp:start:{campaign.id}",
                        ui=ui,
                        style=_style(dict(ui or {}), "confirm"),
                    )
                ]
            )
        elif campaign.status == "running":
            rows.append(
                [
                    _ikb(
                        f"⏸ توقف #{campaign.id}",
                        callback_data=f"{action_prefix}:cmp:pause:{campaign.id}",
                        ui=ui,
                        style=_style(dict(ui or {}), "reject"),
                    )
                ]
            )
    if not campaigns:
        lines.append("هنوز کمپینی ساخته نشده.")
    rows.extend(_footer_rows(ui, web_url, back_callback=back_callback))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _rule_reward(rule: CashbackRule) -> str:
    if rule.mode == "fixed":
        return f"ثابت {format_toman(int(rule.fixed_amount or 0))}"
    percent = int(rule.percent_bps or 0) / 100
    return f"{format_number(percent)}٪"


def cashback_panel(
    settings: CashbackSettings,
    rules: Sequence[CashbackRule],
    *,
    budget_used_amount: int = 0,
    ui: Mapping | None = None,
    web_url: str | None = None,
    back_callback: str = MARKETING_HUB_CALLBACK,
    feature_prefix: str = "adm:cb",
    rule_prefix: str = "adm:cb",
) -> tuple[str, InlineKeyboardMarkup]:
    """Light cashback panel: feature toggle + per-rule enable; create/edit stays in the web panel."""
    status = "روشن" if settings.enabled else "خاموش"
    combine = "بهترین قانون" if settings.combine == "best" else "جمع قانون‌ها"
    lines = [
        "🎁 <b>کش‌بک</b>",
        f"وضعیت: <b>{html.escape(status)}</b> — ترکیب: {html.escape(combine)}",
        f"نگهداری قبل از واریز: {format_number(settings.hold_hours)} ساعت",
    ]
    if settings.monthly_budget > 0:
        lines.append(
            f"بودجه ماه: {format_toman(budget_used_amount)} / {format_toman(settings.monthly_budget)}"
        )
    lines.extend(["", "با دکمه‌ها کش‌بک یا هر قانون را روشن/خاموش کنید. ساخت قانون از وب‌پنل است.", ""])
    rows: list[list[InlineKeyboardButton]] = [
        [
            _ikb(
                "⏸ خاموش‌کردن کش‌بک" if settings.enabled else "▶️ روشن‌کردن کش‌بک",
                callback_data=f"{feature_prefix}:en:{0 if settings.enabled else 1}",
                ui=ui,
                style=_style(dict(ui or {}), "reject" if settings.enabled else "confirm"),
            )
        ]
    ]
    for rule in rules:
        name = html.escape(rule.name or f"#{rule.id}")
        state = "فعال" if rule.enabled else "خاموش"
        lines.append(f"#{rule.id} — {name} — {_rule_reward(rule)} — {state}")
        target = 0 if rule.enabled else 1
        icon = "⏸ خاموش" if rule.enabled else "▶️ روشن"
        rows.append(
            [
                _ikb(
                    f"{icon}: #{rule.id}",
                    callback_data=f"{rule_prefix}:set:{rule.id}:{target}",
                    ui=ui,
                    style=_style(dict(ui or {}), "reject" if rule.enabled else "confirm"),
                )
            ]
        )
    if not rules:
        lines.append("هنوز قانونی ساخته نشده. ساخت قانون از وب‌پنل انجام می‌شود.")
    rows.extend(_footer_rows(ui, web_url, back_callback=back_callback))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)
