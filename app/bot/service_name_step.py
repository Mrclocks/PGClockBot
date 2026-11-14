"""The required name step between choosing a plan and the payment screen."""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.services.custom_service_name import NAME_RULES

ORDER_KEY = "service_name_order_id"
RANDOM_BUTTON_TEXT = "🎲 اسم تصادفی"
CANCEL_BUTTON_TEXT = "❌ انصراف"
PROMPT = (
    "✏️ <b>نام سرویس</b>\n"
    "نامی که می‌خواهید روی سرویس باشد را بفرستید، یا «اسم تصادفی» را بزنید.\n"
    f"<i>{NAME_RULES}</i>"
)


class ServiceNameStates(StatesGroup):
    name = State()


def service_name_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=RANDOM_BUTTON_TEXT, callback_data=f"pay:namernd:{int(order_id)}")],
            # pay:back leaves checkout and reopens the plan step, like the payment screen's back.
            [InlineKeyboardButton(text=CANCEL_BUTTON_TEXT, callback_data=f"pay:back:{int(order_id)}")],
        ]
    )


async def show_service_name_step(
    message: Message, state: FSMContext | None, order_id: int, *, summary: str | None
) -> None:
    # Local import: nav_inline loads the keyboard and hub modules, which import menu_nav.
    from app.bot.nav_inline import present_inline_only

    body = f"{summary.strip()}\n\n{PROMPT}" if summary and summary.strip() else PROMPT
    await present_inline_only(message, text=body, inline=service_name_keyboard(order_id))
    if state is not None:
        await state.set_state(ServiceNameStates.name)
        await state.update_data(**{ORDER_KEY: int(order_id)})
