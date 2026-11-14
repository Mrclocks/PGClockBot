"""Sidebar as each kind of staff member sees it, for tests that check menu rules."""

from __future__ import annotations

from app.services.panel_nav import NavViewer, build_sidebar

ALL_PG = frozenset(
    {"pg_overview", "pg_users", "pg_admins", "pg_nodes", "pg_groups", "pg_hosts", "pg_templates", "pg_inbounds"}
)


def sidebar(
    role: str = "admin",
    *,
    perms: tuple[str, ...] = (),
    pg: frozenset[str] = frozenset(),
    owner: bool = False,
    add_rep: bool = False,
    manage_rep: bool = False,
    marketing: tuple[str, ...] = (),
    path: str = "/home",
    tab: str = "",
) -> dict[str, list[tuple[str, str]]]:
    """``{section key: [(label, href), ...]}`` in display order."""
    viewer = NavViewer(
        role=role,
        perms=frozenset(perms),
        pg_perms=pg,
        is_owner=owner,
        can_add_representative=add_rep,
        can_manage_representatives=manage_rep,
        marketing_hrefs=marketing,
    )
    return {s.key: [(i.label, i.href) for i in s.items] for s in build_sidebar(viewer, path=path, tab=tab)}


def owner_sidebar(**kw) -> dict[str, list[tuple[str, str]]]:
    return sidebar(pg=ALL_PG, owner=True, manage_rep=True, marketing=("/loyalty?tab=overview",), **kw)


def labels(section: list[tuple[str, str]]) -> list[str]:
    return [label for label, _ in section]


def hrefs(section: list[tuple[str, str]]) -> list[str]:
    return [href for _, href in section]
