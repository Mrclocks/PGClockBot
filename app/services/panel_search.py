"""Web panel search: pages, settings tabs and single settings, limited to what the viewer may open.

Built from the sidebar registry and the settings catalogue, so a result never leads to a page the
viewer cannot open. Matching reuses the Telegram settings search (Persian folding, one-typo tolerance).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.services.panel_nav import NavViewer, build_sidebar
from app.services.resellers import RESELLER_SETTINGS_TABS
from app.services.settings_search import SettingsSearchEntry, search_settings
from app.services.settings_tab_groups import group_settings_tabs
from app.services.users import (
    PANEL_SETTINGS_TABS,
    SETTING_GROUPS,
    SETTINGS_DOMAIN_REDIRECTS,
    SETTINGS_TABS,
    TAB_SETTING_GROUPS,
)

PANEL_SEARCH_LIMIT = 12
OWNER_SETTINGS_BASE = "/settings"
SHOP_SETTINGS_BASE = "/shop-settings"
SYSTEM_SETTINGS_LABEL = "تنظیمات سیستم"
# Settings that live in a modal on their own page; the modal opens from the redirect target.
MODAL_SETTINGS_LABELS: dict[str, str] = {
    "payment": "تنظیمات مالی",
    "billing": "تنظیمات PAYG",
    "supports": "تنظیمات پشتیبانی",
    "loyalty": "تنظیمات دعوت دوستان",
}


@dataclass(frozen=True, slots=True)
class PanelSearchHit:
    label: str
    where: str
    href: str


def _entry(label: str, where: str, href: str) -> SettingsSearchEntry:
    # section_id carries the link; section_title is the place shown under the label.
    return SettingsSearchEntry(label=label, section_id=href, section_title=where, sub_id="", sub_title="")


def _field_labels(tab_key: str) -> list[str]:
    return [str(field[1]) for group_name in TAB_SETTING_GROUPS.get(tab_key, []) for field in SETTING_GROUPS.get(group_name, [])]


def _tab_entries(tab_key: str, tab_label: str, href: str, *, tab_where: str, area: str) -> list[SettingsSearchEntry]:
    """The tab itself plus every setting field on it, all linking to the tab."""
    entries = [_entry(tab_label, tab_where, href)]
    entries.extend(_entry(label, f"{area} › {tab_label}", href) for label in _field_labels(tab_key))
    return entries


def _settings_tab_entries(base: str, tabs: Sequence[tuple[str, str]], area: str) -> list[SettingsSearchEntry]:
    entries: list[SettingsSearchEntry] = []
    for group in group_settings_tabs(tabs, ""):
        for tab in group.tabs:
            if tab.sections:
                entries.append(_entry(tab.label, f"{area} › {group.label}", tab.href or f"{base}?tab={tab.key}"))
            for section in tab.sections or (tab,):
                href = section.href or f"{base}?tab={section.key}"
                where = f"{area} › {tab.label}" if tab.sections else f"{area} › {group.label}"
                entries.extend(_tab_entries(section.key, section.label, href, tab_where=where, area=area))
    return entries


def _system_entries() -> list[SettingsSearchEntry]:
    entries: list[SettingsSearchEntry] = []
    for key, label in PANEL_SETTINGS_TABS:
        href = f"{OWNER_SETTINGS_BASE}?tab={key}"
        entries.extend(_tab_entries(key, label, href, tab_where=SYSTEM_SETTINGS_LABEL, area=SYSTEM_SETTINGS_LABEL))
    return entries


def _modal_entries(page_labels: dict[str, str]) -> list[SettingsSearchEntry]:
    """Modal settings, only when the page that hosts the modal is in the viewer's sidebar."""
    entries: list[SettingsSearchEntry] = []
    for key, label in MODAL_SETTINGS_LABELS.items():
        href = SETTINGS_DOMAIN_REDIRECTS[key]
        page = page_labels.get(urlsplit(href).path)
        if page:
            entries.extend(_tab_entries(key, label, href, tab_where=page, area=page))
    return entries


def search_entries(viewer: NavViewer) -> list[SettingsSearchEntry]:
    """Searchable places for ``viewer``; settings appear only where its sidebar shows the settings page."""
    items = [(section.label, item) for section in build_sidebar(viewer, path="/") for item in section.items]
    entries = [_entry(item.label, where, item.href) for where, item in items]
    # Modal settings (payment, support, referral) come before the long text and colour tabs.
    entries += _modal_entries({urlsplit(item.href).path: item.label for _, item in items})
    keys = {item.key for _, item in items}
    if "bot_settings" in keys:
        entries += _settings_tab_entries(OWNER_SETTINGS_BASE, SETTINGS_TABS, "تنظیمات ربات")
    if "shop_settings" in keys:
        entries += _settings_tab_entries(SHOP_SETTINGS_BASE, RESELLER_SETTINGS_TABS, "تنظیمات فروشگاه")
    if "system_settings" in keys:
        entries += _system_entries()
    return entries


def panel_search(viewer: NavViewer, query: str, *, limit: int = PANEL_SEARCH_LIMIT) -> list[PanelSearchHit]:
    """Best matches for ``query`` without duplicates (same label and link)."""
    seen: set[tuple[str, str]] = set()
    hits: list[PanelSearchHit] = []
    for entry in search_settings(search_entries(viewer), query, limit=limit * 3):
        key = (entry.label, entry.section_id)
        if key in seen:
            continue
        seen.add(key)
        hits.append(PanelSearchHit(entry.label, entry.section_title, entry.section_id))
        if len(hits) >= limit:
            break
    return hits
