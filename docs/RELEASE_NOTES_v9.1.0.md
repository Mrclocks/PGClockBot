# PGClockBot v9.1.0 — Release Notes

**Tag:** `v9.1.0`  
**App version:** `9.1.0`  
**Restore point:** tag `restore/pre-v9.1.0-v9.0.4` (@ `v9.0.4`)

---

## Bot / Panel

1. **قوانین ربات** — سه گیت جدا (ورود / خرید کاربر / خرید نماینده) با سوئیچ، متن، دکمه و پذیرش مجدد
2. **رنگ دکمه موافقت** — در تب قوانین و دسته «قوانین» در رنگ‌بندی دکمه‌ها
3. **ایزوله فروشگاه** — پذیرش قوانین per-shop؛ ایموجی پریمیوم از ربات حفظ می‌شود

## Deploy

مایگریشن DB: `0025_terms_acceptances`

```bash
alembic upgrade head
```

Hard refresh once.
