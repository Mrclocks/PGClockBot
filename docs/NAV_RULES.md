# Telegram menu navigation rules

These rules apply to every bot handler that presents a menu or typed-input prompt.

## Inline tap → edit the same message

When the user taps an inline button (`CallbackQuery`), update **that** bot message in place (`edit_text` / `present_inline_only` / `ask_text(..., edit=True)`). Do not send a second panel that replaces or duplicates the first.

## Typed input → `ask_text`

Free-text (and similar) prompts started from an inline button must use `app.bot.nav_input.ask_text` with:

- `edit=True` so the same panel becomes the prompt
- an inline «انصراف» via `nv:cancel:{code}`
- a registered `CancelEntry` that reopens the previous panel

Finish with `finish_text_step` so the typed reply edits the prompt into the next panel. Photo/receipt flows store the prompt with `remember_prompt` and edit it after the media arrives.

## New messages — allow-list only

A callback handler may send a **new** chat message only for:

- subscription link / QR / photo / file delivery
- receipts and payment invoices
- notifications to other chats (admins, other users)
- the one-time main-keyboard healing reply after a legacy `cancel_reply` keyboard

Everything else stays on the edited panel.

## Reply keyboard — main menu + contact only

`ReplyKeyboardMarkup` is reserved for:

- the stable level-0 main menu
- `request_contact` (trial phone share)

Do not attach `cancel_reply()` (or any other reply submenu) from an inline callback. Keep legacy reply «انصراف» handling for stale keyboards; when FSM still knows a `cancel_code`, reopen that panel instead of dumping the user on home.
