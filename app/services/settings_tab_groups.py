"""Group the flat bot-settings tab list into a few labelled sections.

The bot settings page (platform owner and shop owner variants) lists many tabs.
Grouping only changes how the existing tab keys are presented; the keys, the
``?tab=`` URLs and the set of tabs each role receives are untouched. A tab that
no group claims falls into a trailing group so it can never disappear.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.authz import authz_from_staff, can_shop

FALLBACK_GROUP_KEY = "more"
FALLBACK_GROUP_LABEL = "سایر"

MESSAGE_VARIABLES_TAB_KEY = "variables"
MESSAGE_VARIABLES_TAB = (MESSAGE_VARIABLES_TAB_KEY, "متغیرهای پیام")
MESSAGE_VARIABLES_PERMISSION = "shop_settings"
# Tabs that open their own page instead of ``<settings base>?tab=<key>``.
_TAB_PAGES = {MESSAGE_VARIABLES_TAB_KEY: "/message-variables"}

# (group key, label, tab keys in display order). Related tabs sit together:
# what the bot looks like, what it says, what a sale delivers, what it reports.
_GROUP_LAYOUT: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("look", "ظاهر ربات", ("welcome", "appearance", "brand", "menu", "miniapp", "links")),
    ("texts", "متن‌ها", ("messages", "buttons", "colors", "screens", MESSAGE_VARIABLES_TAB_KEY)),
    ("service", "فروش و سرویس", ("naming", "qr", "guides", "limits", "service_import")),
    ("alerts", "هشدار و گزارش", ("notifications", "daily_report")),
    # A reseller's own shop bot (the Owner's bot connection is a system setting).
    ("connect", "ربات اختصاصی", ("bot",)),
)


# Small tabs about one subject share one entry; each keeps its own ``?tab=`` key, form and save
# button and shows as a section inside the merged tab. (merged key, label, member keys)
_MERGED_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("identity", "هویت ربات", ("welcome", "appearance")),
    ("buttons", "دکمه‌ها", ("buttons", "colors")),
    ("delivery", "تحویل سرویس", ("naming", "qr", "guides")),
)
# Section names inside a merged tab, where the old tab name no longer fits.
_SECTION_LABELS = {
    "welcome": "خوش‌آمد",
    "appearance": "پروفایل تلگرام",
    "buttons": "متن دکمه‌ها",
}


@dataclass(frozen=True, slots=True)
class SettingsTab:
    key: str
    label: str
    href: str | None = None
    sections: tuple[SettingsTab, ...] = ()

    def shows(self, active: str) -> bool:
        """True when ``active`` is this tab or one of its sections."""
        return active == self.key or any(section.key == active for section in self.sections)


@dataclass(frozen=True, slots=True)
class SettingsTabGroup:
    key: str
    label: str
    tabs: tuple[SettingsTab, ...]
    is_active: bool


def can_open_message_variables(staff: dict) -> bool:
    """Same rule as the ``/message-variables`` route, so the tab never points at a denied page."""
    return can_shop(authz_from_staff(staff), MESSAGE_VARIABLES_PERMISSION)


def tabs_with_message_variables(
    tabs: Sequence[tuple[str, str]], staff: dict
) -> list[tuple[str, str]]:
    """``tabs`` plus the message-variables tab when ``staff`` may open that page."""
    merged = list(tabs)
    if can_open_message_variables(staff):
        merged.append(MESSAGE_VARIABLES_TAB)
    return merged


def group_settings_tabs(
    tabs: Sequence[tuple[str, str]], active: str
) -> tuple[SettingsTabGroup, ...]:
    """Split ``tabs`` into groups, keeping only tabs the caller already holds."""
    available, alias = _merge_tabs({key: SettingsTab(key, label, _TAB_PAGES.get(key)) for key, label in tabs})
    claimed: set[str] = set()
    groups: list[SettingsTabGroup] = []
    for group_key, label, keys in _GROUP_LAYOUT:
        merged_keys = dict.fromkeys(alias.get(key, key) for key in keys)
        members = tuple(available[key] for key in merged_keys if key in available)
        claimed.update(tab.key for tab in members)
        if members:
            groups.append(_build_group(group_key, label, members, active))
    leftovers = tuple(tab for key, tab in available.items() if key not in claimed)
    if leftovers:
        groups.append(_build_group(FALLBACK_GROUP_KEY, FALLBACK_GROUP_LABEL, leftovers, active))
    return tuple(groups)


def resolve_merged_tab(key: str) -> str:
    """The tab key to render for ``key``: a merged tab opens its first section."""
    for merged_key, _label, members in _MERGED_TABS:
        if key == merged_key:
            return members[0]
    return key


def _merge_tabs(available: dict[str, SettingsTab]) -> tuple[dict[str, SettingsTab], dict[str, str]]:
    """Fold merged members into one tab placed where its first member was.

    A merge needs at least two members the caller holds; otherwise the tab stays as it was.
    Returns the new tabs by key and a member-key -> merged-key map.
    """
    alias: dict[str, str] = {}
    folded: dict[str, SettingsTab] = {}
    for merged_key, label, members in _MERGED_TABS:
        held = [available[key] for key in members if key in available]
        if len(held) < 2:
            continue
        sections = tuple(SettingsTab(tab.key, _SECTION_LABELS.get(tab.key, tab.label), tab.href) for tab in held)
        folded[held[0].key] = SettingsTab(merged_key, label, None, sections)
        alias.update({tab.key: merged_key for tab in held})
    result: dict[str, SettingsTab] = {}
    for key, tab in available.items():
        if key in folded:
            result[folded[key].key] = folded[key]
        elif key not in alias:
            result[key] = tab
    return result, alias


def _build_group(
    key: str, label: str, members: tuple[SettingsTab, ...], active: str
) -> SettingsTabGroup:
    return SettingsTabGroup(
        key=key,
        label=label,
        tabs=members,
        is_active=any(tab.shows(active) for tab in members),
    )
