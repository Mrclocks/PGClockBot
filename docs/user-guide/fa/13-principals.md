# Principal و ساختار سازمانی

## معرفی

Principal مدل هویت سازمانی برای تعریف Owner و اعضای سلسله‌مراتب سازمانی است.

## Owner

Owner واقعی در ریشه سازمان قرار دارد و Principal سطح صفر و فعال است. صرف `role=admin` بودن، Owner بودن را ثابت نمی‌کند.

## سطح و parent

Depth و Parent تعیین می‌کنند Principal در چه سطحی از سازمان قرار دارد و چه scopeای دارد.

## هویت‌های مرتبط

Principal می‌تواند Web Identity و credential مرتبط با PasarGuard داشته باشد. این هویت‌ها باید به همان Principal و Scope خودش محدود بمانند.

## Provision

ساخت یا تغییر منابعی که به Principal وابسته‌اند باید از Gateهای Provision و Scope عبور کند.

## مثال

یک Principal سطح پایین نمی‌تواند با جعل شناسه Principal بالاتر، خود را Owner معرفی کند؛ وضعیت، depth، parent و identity باید از سمت سرور بررسی شوند.
