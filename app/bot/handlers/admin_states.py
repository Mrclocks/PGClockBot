"""FSM states of the platform admin screens in the bot.

Stored states are named after the class (``AdminStates:add_plan_name``), not the module, so states
saved before this class moved keep working.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    add_plan_name = State()
    add_plan_price = State()
    add_plan_days = State()
    add_plan_gb = State()
    add_plan_link = State()  # waiting for mode after basics
    add_plan_color = State()  # Telegram button color before category/save
    add_plan_category = State()  # optional PlanCategory before save
    plan_edit_field = State()
    make_reseller = State()
    ticket_reply = State()
    user_search = State()
    reseller_search = State()
    user_wallet_credit = State()
    user_wallet_reason = State()
    user_message = State()
    revoke_reseller_reason = State()
    block_user_reason = State()
    delete_user_reason = State()
    broadcast_text = State()
    broadcast_audience = State()
    svc_adjust_days_input = State()
    svc_adjust_gb_input = State()
    reseller_cap_days_input = State()
    reseller_cap_gb_input = State()
