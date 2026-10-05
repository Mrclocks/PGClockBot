# 🕐 PGClockBot

![PGClockBot preview](preview.png)

**v0.1.14** — ربات فروش و پنل مدیریت برای **PasarGuard**

فروش اشتراک، نمایندگی، کیف پول و عملیات پاسارگارد؛ رابط ربات فارسی، پنل وب روی پورت `9000`.

> ⚠️ **نسخه بتا** — امکان وجود باگ در این نسخه هست. برای محیط حساس با احتیاط استفاده کنید.

---

## ✨ قابلیت‌ها

### 💳 فروش و پرداخت
- پلن‌های کاربر و خرید مهمان
- کیف پول، کارت‌به‌کارت، درگاه، رمزارز، استارز
- تمدید، سرویس‌های من، هشدار انقضا و حجم

### 🏪 نمایندگی
- پلن و فروشگاه نماینده
- سهم / PAYG، ربات اختصاصی
- مدیریت نمایندگان از وب و بات

### 🛡️ پاسارگارد
- کاربران، ادمین، گروه، هاست، نود، تمپلیت
- سهمیه و نقش زنده از پاسارگارد
- ساخت کاربر از تمپلیت یا سفارشی

### 🖥️ پنل وب
- داشبورد، سفارش‌ها، تیکت، تنظیمات
- راهنمای درون‌پنل (`/help`)
- مینی‌اپ تلگرام (اختیاری، HTTPS)

---

## 🚀 نصب سریع

**منابع پیشنهادی:** ۲ گیگ رم و ۲ هسته CPU · Ubuntu 22.04+

### قبل از نصب این‌ها را آماده کنید
1. **دامنه** متصل به آی‌پی سرور
2. **توکن ربات**، **یوزرنیم ربات** و **آیدی ادمین** تلگرام
3. **اطلاعات ورود پنل پاسارگارد** (آدرس، یوزر، رمز)

### یک‌خطی
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)
```

بعد از نصب، راه‌اندازی در مرورگر کامل می‌شود: `http://SERVER_IP:9000/` (یا دامنهٔ شما).

### منوی اسکریپت (`pgclock.sh`)
دستور بالا منو را باز می‌کند. داخل پوشهٔ پروژه هم:

```bash
bash pgclock.sh
```

| گزینه | کار |
|------|-----|
| `1` Install | نصب خاموش → ادامه در ویزارد `/setup` |
| `2` Update | به‌روزرسانی کد (`.env` حفظ می‌شود) |
| `3` Edit .env | ویرایش توکن / پنل / پورت |
| `4` Web panel | آدرس پنل، ریست رمز، health |
| `5` Service | وضعیت / استارت / ری‌استارت / لاگ |
| `6` Status | خلاصهٔ سلامت |
| `7` Uninstall | حذف کامل |
| `0` Exit | خروج |

میانبر بدون منو:

```bash
bash pgclock.sh install
bash pgclock.sh update
bash pgclock.sh status
bash pgclock.sh help
```

---

## 🛠️ CLI سراسری (`pgclock`)

بعد از نصب (یک‌بار با sudo):

```bash
sudo bash scripts/install_global_cli.sh
```

از هر مسیری:

```bash
pgclock status
pgclock start
pgclock stop
pgclock restart
pgclock logs              # ۱۰۰ خط آخر
pgclock logs -f           # دنبال کردن لاگ
pgclock health
pgclock backup --note "pre-update"
pgclock backup --list
pgclock restore <backup_id> -y
pgclock migrate
pgclock doctor
```

بدون نصب سراسری، از ریشهٔ پروژه:

```bash
export PGCLOCK_HOME=/path/to/PGClockBot
./scripts/pgclock status
```

---

## 📄 لایسنس

هر گونه کپی‌برداری بدون ذکر نام و فروش غیرمجاز است.

[MIT](LICENSE) © Mrclocks
