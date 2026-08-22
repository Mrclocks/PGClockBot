# Troubleshooting

## ورود Web انجام نمی‌شود

رمز، وضعیت حساب، Session و محدودیت تلاش‌های ناموفق را بررسی کنید.

## Bot کار نمی‌کند

Token، وضعیت سرویس Bot و تنظیمات Update Mode را بررسی کنید.

## PasarGuard متصل نیست

Base URL، credential و وضعیت واقعی حساب PG را بررسی کنید. Login موفق به معنی دسترسی کامل نیست.

## منوی یک نقش نمایش داده نمی‌شود

Role، ACL زنده، Scope و credential اختصاصی آن نقش را بررسی کنید. مخفی بودن منو نباید با اعطای دستی URL دور زده شود.

## Reseller داده‌ای نمی‌بیند

Shop Scope و ارتباط حساب با `bot_user_id`/tenant را بررسی کنید.

## عملیات PG خطا می‌دهد

بررسی کنید credential اختصاصی نقش وجود دارد و Role PasarGuard مجوز همان عملیات را دارد.

## Update مشکل ایجاد کرده است

Version، Migration و Backup قبل از Update را بررسی کنید و سپس Recovery را بر اساس Engine و نسخه انجام دهید.

## اصل عیب‌یابی

اول مشخص کنید مشکل از Authentication، Authorization، Scope، External Service، Database یا UI است؛ سپس همان لایه را بررسی کنید.
