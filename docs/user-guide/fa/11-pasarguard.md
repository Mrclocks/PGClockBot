# PasarGuard

## معرفی

بخش PasarGuard برای مدیریت منابع سرویس VPN است و شامل نمای کلی، User، Template، Group، Host، Inbound، Node و در سطح مالکیت، Admin است.

## PG User

PG User می‌تواند به سرویس کاربر متصل باشد و اطلاعاتی مانند Traffic، Expiry، Template و Group داشته باشد.

## Template و Group

Template برای تعریف الگوی تنظیمات کاربر و Group برای سازمان‌دهی منابع استفاده می‌شود.

## Host، Inbound و Node

این منابع اجزای زیرساخت PasarGuard هستند و سطح دسترسی هر حساب باید مطابق Role واقعی PasarGuard باشد.

## PG Admin

مدیریت ادمین‌های PasarGuard قابلیت حساس و Owner-only است و Roleهای محدود نباید از طریق UI یا API به آن ارتقا پیدا کنند.

## Credential

Platform، Reseller، pg_staff و Principal می‌توانند مسیرهای credential متفاوت داشته باشند. حساب محدود نباید با credential Owner عملیات انجام دهد.

## مثال

Reseller A می‌تواند PG Userهای محدوده خودش را مدیریت کند، اما نباید با تغییر user_id به PG User متعلق به Reseller B دسترسی بگیرد.

## اصل مهم

دو کنترل مستقل باید برقرار باشند: مجوز پنل و Scope/Role واقعی PasarGuard. داشتن یکی بدون دیگری نباید باعث دسترسی بیشتر شود.
