"""Reseller panel groups: which hub entry lives in which group, and where «بازگشت» leads.

The shop panel shows four groups instead of one long list. Every entry keeps its
``nv:res:ra:<action>`` callback, so buttons on older messages still open the same screen;
only the back button of a screen changes, to its group (one step up).
"""

from __future__ import annotations

from dataclasses import dataclass

RESELLER_HOME_CALLBACK = "nv:res:home"
GROUP_CALLBACK_PREFIX = "nv:res:grp:"


@dataclass(frozen=True, slots=True)
class ResellerGroup:
    id: str
    label_key: str
    actions: tuple[str, ...]

    @property
    def callback(self) -> str:
        return f"{GROUP_CALLBACK_PREFIX}{self.id}"

    @property
    def screen_id(self) -> str:
        return f"r.{self.id}"


RESELLER_GROUPS: tuple[ResellerGroup, ...] = (
    ResellerGroup("daily", "btn_res_grp_daily", ("res_dash", "res_orders", "res_payments", "res_tickets")),
    ResellerGroup("customers", "btn_res_grp_customers", ("res_users", "res_add_rep")),
    ResellerGroup(
        "shop",
        "btn_res_grp_shop",
        ("res_plans", "res_loyalty", "res_broadcast", "res_settings"),
    ),
    ResellerGroup(
        "account",
        "btn_res_grp_account",
        ("res_billing", "res_addon_packs", "res_renew", "res_buy_gb", "res_buy_users", "res_reports", "res_stats"),
    ),
)
_BY_ID = {group.id: group for group in RESELLER_GROUPS}
_BY_ACTION = {action: group for group in RESELLER_GROUPS for action in group.actions}
# Screens reached from a queue shortcut rather than a hub entry.
_BY_ACTION["res_cancellations"] = _BY_ID["daily"]


def reseller_group(group_id: str) -> ResellerGroup | None:
    return _BY_ID.get(group_id)


def group_entries(group: ResellerGroup, entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The hub entries of ``group``, in the group's order."""
    by_action = dict(entries)
    return [(action, by_action[action]) for action in group.actions if by_action.get(action)]


def back_callback_for(action: str) -> str:
    """Where «بازگشت» on the screen behind ``action`` leads: its group, else the panel."""
    group = _BY_ACTION.get(action)
    return group.callback if group else RESELLER_HOME_CALLBACK
