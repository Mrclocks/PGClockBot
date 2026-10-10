# Bot nav maturity (0.2.47)

Operator-facing notes for the Option B inline-only follow-ups that land as sequential PRs into `dev`. Feature PRs do **not** bump `VERSION` or `RELEASE_NOTES_FA`; maintainers cut the release and paste the draft bullets below.

## Scope (what landed)

| PR | Branch theme | Behavior |
|----|--------------|----------|
| 1 | Tenancy kwargs | `is_reseller_bot` / `reseller_owner_id` required on keyboard heal helpers — shop bots no longer fall back to platform chrome |
| 2 | Service card | `svc:cancel` + `guide:svc` catalog fallback; cancel/guides stay on the service card |
| 3 | Welcome | One welcome message (no inline CTAs on welcome); `menu:home` fallback; trial-first shop picker; shop-bot apply gate |
| 4 | Heal / legacy | `heal_main_reply`; empty-FSM legacy taps reopen services |
| 5 | Customer `ask_text` | Wallet amount, wholesale qty, ticket subject/body use inline `nv:cancel:{code}` — main ReplyKeyboard stays put |
| 6 | Admin counters | Platform + shop hubs show pending receipts/tickets (Persian badges) + cancel count **summary-only** |

**Deferred (not in 0.2.47):** full admin/reseller `cancel_reply` migration (~remaining sites), bot cancel inbox, maintenance toggle / daily-report P3.

## Security invariants

- Every heal / main-keyboard path takes explicit shop-bot context; defaults that implied platform are gone.
- Admin pending counters use finance-grade tenancy (`wallet_shop_id` + `Order.reseller_id` for receipts; sticky ticket `reseller_id` + customer fallback; `pending_cancellation_count(shop_id)`).
- Platform (`shop_id=None`) never includes shop-tenant receipts, tickets, or cancellations.
- Shop A never sees shop B or platform queue.
- Demo customers are excluded from payment/ticket badges.
- Regression: `tests/test_nav_admin_counters_0_2_47.py` (+ tenancy / service-card / welcome / heal / ask_text suites) listed in `scripts/security_test_manifest.txt` where they are isolation-critical.

## Operator UX notes

- **Welcome:** `/start` is one message; CTAs live on home / shop, not on the welcome body.
- **Free-text steps (customer):** Cancel is an inline button under the prompt (`nv:cancel:…`). Legacy reply label «انصراف» still works where older handlers remain.
- **Admin / reseller hubs:** Body may show `⏳ صف: رسید … · تیکت … · لغو …`. Payment and ticket buttons get `(N)` badges. Cancel is count-only until a bot cancel inbox exists (use the web finance tab).

## Rollback

No schema migrations in this wave. To undo after a bad deploy of the 0.2.47 tip:

1. Note the pre-release tag maintainers create (pattern: `restore/pre-v0.2.47-v0.2.46`).
2. Check out / deploy that restore tag (or the previous released commit on `main`).
3. Restart the bot/web service (`bash pgclock.sh service` / unit restart).
4. Smoke: `/start` on platform + one shop bot; open admin ops hub; open reseller manage hub; submit a wallet custom amount and cancel via inline.

If only one PR in the stack is suspect, prefer reverting that PR on `dev` rather than rolling the whole release; tenancy (PR1) should stay if later PRs depend on required kwargs.

## Draft release notes (Persian — for maintainers)

Paste into `RELEASE_NOTES_FA["0.2.47"]` when cutting the release (do not land these in a feature PR):

```text
"سخت‌سازی کانتکست فروشگاه: کیبورد/ترمیم منو دیگر به‌اشتباه منوی پلتفرم را روی ربات نماینده نشان نمی‌دهد",
"کارت سرویس: لغو و راهنما روی همان کارت؛ کاتالوگ راهنما به‌عنوان پشتیبان",
"خوش‌آمد یک‌پیامی؛ خرید آزمایشی اول در انتخاب فروشگاه؛ درخواست نمایندگی فقط روی ربات فروشگاه",
"ورودی متن مشتری (مبلغ کیف، تعداد عمده، تیکت) بدون تعویض ReplyKeyboard — انصراف اینلاین",
"نشان صف ادمین/نماینده: رسید و تیکت با رقم فارسی؛ شمارش لغو فقط خلاصه (بدون نشتی بین فروشگاه‌ها)",
"ریستور: restore/pre-v0.2.47-v0.2.46",
```
