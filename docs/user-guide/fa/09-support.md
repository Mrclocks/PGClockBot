# پشتیبانی و Ticket

## Ticket کاربر

Ticket برای ارتباط کاربر با پشتیبانی است و وضعیت‌هایی مانند Open، Answered و Closed دارد.

## Ticket پنل

`PanelTicket` مسیر پشتیبانی داخلی برای ارتباط Reseller یا pg_staff با Platform Admin است و وضعیت‌های Open، In Progress، Answered و Closed دارد.

## تفاوت دو نوع Ticket

Ticket کاربر برای پشتیبانی مشتری است؛ Panel Ticket برای عملیات و پشتیبانی بین نقش‌های مدیریتی است. این دو را نباید یکی فرض کرد.

## Scope

Ticket فروشگاه باید به همان tenant محدود شود. Reseller نباید Ticket فروشگاه دیگری را ببیند.

## مثال

اگر یک Reseller درباره مشکل اتصال PasarGuard سؤال دارد، می‌تواند از Panel Ticket با Platform Admin ارتباط بگیرد؛ این Ticket با Ticket مشتریان عادی متفاوت است.
