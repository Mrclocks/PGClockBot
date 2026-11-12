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
- a screen opened from a button under a notice (see below)
- the one-time main-keyboard healing reply after a legacy `cancel_reply` keyboard

Everything else stays on the edited panel.

## «بازگشت» — back to where the person came from

Build every back button with `back_button(...)` / `with_inline_back(...)` (label key `btn_back`) and
the screen's fixed parent. `app.bot.panel_history` keeps, per panel message, the read-only screens it
showed, and the update that opens a screen builds its back buttons with the screen before it (the
second page of a list, the stuck order or cancellation card the customer card was opened from).
Fixed parents still apply when the history does not know the way (after a restart, after a typed
reply shown in the panel).

«بازگشت» sends the recorded callback again, so only read-only screens are recorded: a screen is
added to `VIEW_SCREENS` / `VIEW_FAMILIES` only when its handler changes nothing (refreshing a cache
or clearing an unfinished prompt is fine). Never add an approve, delete, toggle or payment callback.

## Buttons under a notice — open a panel, keep the notice

A notice (a staff alert, a review item, a report) is a record and is never edited into another
screen. Its buttons that open a read-only screen (customer card, service card, order, review
card) carry `nto:<screen callback>` (`app.services.notice_links.notice_markup`, applied where
notices are sent). The bot opens that screen in a new message under the notice; on that first
screen, and on any screen of the panel at the same level (the card shown again after an action),
«بازگشت» closes the panel and the notice is on screen again. Buttons that
act (approve, reject, reply, close) keep acting on the notice. A new read-only screen that a
notice links to is added to `NOTICE_SCREEN_PREFIXES` (or `NOTICE_SCREENS` for a hub) and to the
panel history's view screens. Staff reports and alerts build their buttons with
`app.services.staff_notice_buttons`.

## Reply keyboard — main menu + contact only

`ReplyKeyboardMarkup` is reserved for:

- the stable level-0 main menu
- `request_contact` (trial phone share)

Do not attach `cancel_reply()` (or any other reply submenu) from an inline callback. Keep legacy reply «انصراف» handling for stale keyboards; when FSM still knows a `cancel_code`, reopen that panel instead of dumping the user on home.
