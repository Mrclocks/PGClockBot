"""The command catalogue of one viewer: every menu button and setting they may open, for search.

Menus are read from the real keyboard builders, walked from the panel home the way a person taps
through them. A button the viewer's permissions hide is not on their keyboard, so it is not in their
catalogue either, and a button added to a menu later is found without listing it here.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.bot import nav_inline as hubs
from app.bot.edit_mode import without_editor
from app.bot.reseller_hub_groups import RESELLER_GROUPS, RESELLER_HOME_CALLBACK
from app.bot.screen_registry import SCREENS, Audience, ScreenContext
from app.services.command_search import CommandEntry, CommandKind
from app.services.resellers import has_bot_perm

logger = logging.getLogger(__name__)

ADMIN_HOME_CALLBACK = "nv:adm:home"
ADMIN_SETTINGS_CALLBACK = "nv:adm:settings"
SHOP_SETTINGS_CALLBACK = "res:st:hub"
SHOP_SETTINGS_PERMISSION = "shop_settings"
PATH_SEPARATOR = " › "
LABELS_SECTION_KIND = "labels"
CALLBACK_MAX_BYTES = 64
# Leaving the panel, and the search entries themselves, are never results.
_SKIPPED_CALLBACKS = frozenset({"menu:home", "nv:adm:close", "adm:st:find"})
_SKIPPED_PREFIXES = ("cmd:", "nv:cancel:")

SettingsSections = Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class MenuNode:
    """A menu screen: its title in result paths, how to build it, and other callbacks that open it."""

    title: str
    build: Callable[[], InlineKeyboardMarkup]
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MenuWalk:
    entries: list[CommandEntry]
    # Callback of every menu reached → its path, so settings can be placed under their menu.
    paths: dict[str, str]


def _is_result_target(target: str | None) -> bool:
    return bool(target) and target not in _SKIPPED_CALLBACKS and not target.startswith(_SKIPPED_PREFIXES)


def _build(node: MenuNode) -> InlineKeyboardMarkup | None:
    try:
        with without_editor():
            return node.build()
    except Exception:  # one broken menu must not take search down; its buttons are left out
        logger.warning("command catalogue could not build menu %s", node.title, exc_info=True)
        return None


def walk_menus(root: str, nodes: Mapping[str, MenuNode]) -> MenuWalk:
    """Every button reachable from ``root``, shallowest first; back buttons are left out."""
    canonical = {alias: callback for callback, node in nodes.items() for alias in (callback, *node.aliases)}
    entries: list[CommandEntry] = []
    paths = {root: nodes[root].title}
    queue: deque[tuple[str, frozenset[str]]] = deque([(root, frozenset({root}))])
    while queue:
        current, ancestors = queue.popleft()
        markup = _build(nodes[current])
        if markup is None:
            continue
        for row in markup.inline_keyboard:
            for button in row:
                target = button.callback_data
                if not _is_result_target(target):
                    continue
                node_callback = canonical.get(target, target)
                if node_callback in ancestors:
                    continue
                entries.append(CommandEntry(button.text, paths[current], target, CommandKind.MENU))
                if node_callback in nodes and node_callback not in paths:
                    paths[node_callback] = paths[current] + PATH_SEPARATOR + nodes[node_callback].title
                    queue.append((node_callback, ancestors | {node_callback}))
    return MenuWalk(entries=entries, paths=paths)


def settings_entries(
    sections: SettingsSections,
    *,
    callback_prefix: str,
    base_path: str,
    hidden_subs: frozenset[str] = frozenset(),
) -> Iterator[CommandEntry]:
    """One entry per settings subsection and per field in it; a field opens its subsection."""
    for sec_id, section in sections.items():
        section_path = base_path + PATH_SEPARATOR + str(section["title"])
        field_kind = CommandKind.LABEL if section.get("kind") == LABELS_SECTION_KIND else CommandKind.SETTING
        for sub_id, sub_title, payload, *_ in section.get("subs") or []:
            target = f"{callback_prefix}:st:sub:{sec_id}:{sub_id}"
            if sub_id in hidden_subs or len(target.encode()) > CALLBACK_MAX_BYTES:
                continue
            yield CommandEntry(sub_title, section_path, target, field_kind)
            if isinstance(payload, list):
                field_path = section_path + PATH_SEPARATOR + sub_title
                for key, label, _kind in payload:
                    yield CommandEntry(label, field_path, target, field_kind, terms=key)


def _admin_nodes(ui: dict[str, str]) -> dict[str, MenuNode]:
    ctx = ScreenContext(ui=ui)
    return {
        spec.reopen_callback: MenuNode(spec.title, lambda spec=spec: spec.build(ctx))
        for spec in SCREENS.values()
        if spec.audience is Audience.ADMIN and spec.reopen_callback != "menu:home"
    }


def admin_command_entries(ui: dict[str, str], settings_sections: SettingsSections) -> list[CommandEntry]:
    """The platform owner's catalogue: the admin panel menus, then every owner setting."""
    walk = walk_menus(ADMIN_HOME_CALLBACK, _admin_nodes(ui))
    base = walk.paths.get(ADMIN_SETTINGS_CALLBACK, SCREENS["a.settings"].title)
    return [*walk.entries, *settings_entries(settings_sections, callback_prefix="adm", base_path=base)]


@dataclass(frozen=True, slots=True)
class ShopViewer:
    """What the shop panel home is built from for this person (see ``reseller_hub.ResellerHub``)."""

    profile: Any
    ui: dict[str, str]
    can_add_representative: bool
    show_owner_creds: bool


def _shop_nodes(viewer: ShopViewer) -> dict[str, MenuNode]:
    flags = {"can_add_representative": viewer.can_add_representative, "show_owner_creds": viewer.show_owner_creds}
    nodes = {
        RESELLER_HOME_CALLBACK: MenuNode(
            SCREENS["r.home"].title, lambda: hubs.reseller_manage_hub_keyboard(viewer.profile, viewer.ui, **flags)
        ),
        SHOP_SETTINGS_CALLBACK: MenuNode(
            SCREENS["r.settings"].title,
            lambda: hubs.reseller_settings_hub_keyboard(viewer.ui),
            aliases=("nv:res:ra:res_settings",),
        ),
        "res:plans": MenuNode(
            SCREENS["r.plans"].title, lambda: hubs.reseller_plans_hub_keyboard(viewer.ui), aliases=("nv:res:ra:res_plans",)
        ),
        "shp:bc:hub": MenuNode(SCREENS["r.broadcast"].title, lambda: hubs.shop_broadcast_hub_keyboard(viewer.ui)),
    }
    for group in RESELLER_GROUPS:
        nodes[group.callback] = MenuNode(
            SCREENS[group.screen_id].title,
            lambda group_id=group.id: hubs.reseller_group_hub_keyboard(group_id, viewer.profile, viewer.ui, **flags),
        )
    return nodes


def shop_command_entries(
    viewer: ShopViewer, settings_sections: SettingsSections, sub_permissions: Mapping[str, str]
) -> list[CommandEntry]:
    """A shop panel's catalogue: the menus its permissions show, and its shop settings when allowed."""
    walk = walk_menus(RESELLER_HOME_CALLBACK, _shop_nodes(viewer))
    settings_path = walk.paths.get(SHOP_SETTINGS_CALLBACK)
    if settings_path is None or not has_bot_perm(viewer.profile, SHOP_SETTINGS_PERMISSION):
        return walk.entries
    hidden = frozenset(sub for sub, perm in sub_permissions.items() if not has_bot_perm(viewer.profile, perm))
    settings = settings_entries(settings_sections, callback_prefix="res", base_path=settings_path, hidden_subs=hidden)
    return [*walk.entries, *settings]
