"""The list page of a settings area (DESIGN_SYSTEM.md §6-2).

A settings area opens on a list: one titled card per group and one row per settings page, each with an icon
and a one-line description. The rows come from the same grouped tabs the role already holds
(``settings_tab_groups.group_settings_tabs``), so the list never shows a page the role may not open.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.settings_tab_groups import SettingsTab, SettingsTabGroup
from app.services.users import PANEL_SETTINGS_TABS

# ``/settings?tab=system`` is the list of the system settings area; the pages keep their own keys.
SYSTEM_LIST_TAB = "system"
SECURITY_TAB = SettingsTab("security", "امنیت", "/security")

# One line per settings page: what you change there, in the words of its own fields.
TAB_DESCRIPTIONS: dict[str, str] = {
    "identity": "پیام خوش‌آمد، نام و پروفایل ربات در تلگرام",
    "welcome": "پیام خوش‌آمد و نام فروشگاه",
    "appearance": "نام، توضیح و عکس ربات در تلگرام",
    "brand": "رنگ و لوگوی فروشگاه",
    "menu": "چیدمان و ترتیب دکمه‌های منوی اصلی",
    "miniapp": "ظاهر و فعال بودن مینی‌اپ",
    "links": "دکمه‌های لینک کانال، پشتیبانی و راهنما",
    "messages": "متن پیام‌هایی که ربات می‌فرستد",
    "buttons": "متن و رنگ دکمه‌های ربات",
    "colors": "رنگ دکمه‌های ربات",
    "screens": "تیتر و توضیح بالای هر منو",
    "variables": "متغیرهایی که در متن پیام‌ها به کار می‌روند",
    "delivery": "نام سرویس، QR اشتراک و آموزش اتصال",
    "naming": "قالب نام سرویس‌های ساخته‌شده",
    "qr": "QR لینک اشتراک",
    "guides": "آموزش اتصال برای هر سیستم‌عامل",
    "limits": "قوانین خرید و محدودیت‌های فروش",
    "service_import": "افزودن سرویس‌های قبلی کاربران",
    "notifications": "هشدارهای ربات به ادمین‌ها",
    "daily_report": "گزارش روزانهٔ فروش",
    "bot": "توکن و اتصال ربات اختصاصی فروشگاه",
}

# Icons from _icons.html; a tab without its own falls back to the icon of its area.
TAB_ICONS: dict[str, str] = {
    "identity": "bot",
    "welcome": "bot",
    "appearance": "bot",
    "brand": "palette",
    "menu": "menu",
    "miniapp": "phone",
    "links": "external",
    "messages": "message",
    "buttons": "plans",
    "colors": "palette",
    "screens": "template",
    "variables": "custom",
    "delivery": "cart",
    "naming": "template",
    "qr": "qr",
    "guides": "help",
    "limits": "shield",
    "service_import": "download",
    "notifications": "inbox",
    "daily_report": "sales",
    "bot": "bot",
}


SYSTEM_DESCRIPTIONS: dict[str, str] = {
    "general": "پورت و نشانی وب‌پنل و تنظیمات پایه",
    "backup": "خروجی و ورودی تنظیمات و بکاپ زمان‌بندی‌شده",
    "pwa": "نوار موبایل، نام و رنگ برند وب‌پنل",
    "ssl": "دامنهٔ وب‌پنل و مینی‌اپ و وضعیت گواهی",
    "bot": "توکن ربات، دریافت پیام و اتصال پاسارگارد",
    "update": "نسخهٔ نصب‌شده و به‌روزرسانی پنل",
    "security": "هویت ادمین وب و تلگرام، نام کاربری و رمز",
}
SYSTEM_ICONS: dict[str, str] = {
    "general": "gear",
    "backup": "download",
    "pwa": "palette",
    "ssl": "globe",
    "bot": "connection",
    "update": "refresh",
    "security": "shield",
}


@dataclass(frozen=True, slots=True)
class HubRow:
    label: str
    href: str
    description: str
    icon: str


@dataclass(frozen=True, slots=True)
class HubSection:
    label: str
    rows: tuple[HubRow, ...]


@dataclass(frozen=True, slots=True)
class SettingsPlace:
    """Where a settings page sits: its group and its own entry in the list."""

    group: SettingsTabGroup
    tab: SettingsTab


def tab_href(tab: SettingsTab, base: str) -> str:
    return tab.href or f"{base}?tab={tab.key}"


def hub_sections(
    groups: tuple[SettingsTabGroup, ...],
    base: str,
    *,
    area_icon: str,
    descriptions: dict[str, str] | None = None,
    icons: dict[str, str] | None = None,
) -> tuple[HubSection, ...]:
    """One section per group, one row per tab, in the order the groups already have."""
    descriptions = TAB_DESCRIPTIONS if descriptions is None else descriptions
    icons = TAB_ICONS if icons is None else icons
    return tuple(
        HubSection(
            label=group.label,
            rows=tuple(
                HubRow(
                    label=tab.label,
                    href=tab_href(tab, base),
                    description=descriptions.get(tab.key, ""),
                    icon=icons.get(tab.key, area_icon),
                )
                for tab in group.tabs
            ),
        )
        for group in groups
    )


def settings_place(groups: tuple[SettingsTabGroup, ...], active: str) -> SettingsPlace | None:
    """The group and list entry of ``active`` (a tab key or a section of a merged tab), if any."""
    for group in groups:
        for tab in group.tabs:
            if tab.shows(active):
                return SettingsPlace(group=group, tab=tab)
    return None


def system_settings_groups(active: str = "") -> tuple[SettingsTabGroup, ...]:
    """The system settings area as one untitled group: the web panel's own pages and security."""
    tabs = (*(SettingsTab(key, label) for key, label in PANEL_SETTINGS_TABS), SECURITY_TAB)
    return (SettingsTabGroup(key="system", label="", tabs=tabs, is_active=any(tab.shows(active) for tab in tabs)),)


def system_hub_sections() -> tuple[HubSection, ...]:
    return hub_sections(
        system_settings_groups(), "/settings", area_icon="gear", descriptions=SYSTEM_DESCRIPTIONS, icons=SYSTEM_ICONS
    )
