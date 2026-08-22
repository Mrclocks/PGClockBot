# Backup و Restore

## Backup

Backup برای بازیابی داده‌ها پس از خرابی، Migration یا خطای عملیاتی ضروری است.

## Restore

Restore باید با احتیاط انجام شود چون می‌تواند داده فعلی را جایگزین کند یا ساختار DB را تغییر دهد.

## قبل از Restore

1. از وضعیت فعلی Backup بگیرید.
2. نسخه برنامه را بررسی کنید.
3. Engine دیتابیس را بررسی کنید.
4. از سازگاری Migrationها مطمئن شوید.
5. در محیط Production عملیات را در زمان مناسب انجام دهید.

## SQLite و PostgreSQL

سیستم Migrationهای Alembic و پشتیبانی از Engineهای مختلف دارد. Backup/Restore باید مطابق Engine واقعی انجام شود.

## مثال

اگر قبل از Update یک Backup معتبر دارید، در صورت خرابی Migration می‌توان مسیر Recovery را بر اساس نسخه و Engine دنبال کرد.

## هشدار

Backup موفق به معنی Restore تست‌شده نیست. برای داده‌های حساس، فرآیند Recovery باید دوره‌ای آزمایش شود.
