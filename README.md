# 🕐 PGClockBot

![PGClockBot preview](preview.png)

فروش اشتراک، نمایندگی، کیف پول و عملیات پاسارگارد؛ رابط ربات فارسی، پنل وب روی پورت `9000`.

**نسخه پایدار `0.5.0`** روی شاخهٔ `main` — نصب پیش‌فرض همین کانال است. شاخهٔ `dev` برای پیش‌نسخه‌های آزمایشی است.

---

## ✨ قابلیت‌ها

### 💳 فروش و پرداخت
- پلن‌های کاربر و خرید مهمان؛ کیف پول، کارت‌به‌کارت، درگاه لینک، رمزارز، استارز
- درگاه آنلاین (مثل SunPay) و چند API به‌ازای هر فروشگاه
- تمدید، سرویس‌های من، هشدار انقضا/حجم و پیشنهاد شارژ
- تمدید خودکار و افزایش خودکار زمان/حجم از کیف پول (per-service)
- ابطال پرداخت تأییدشده و پیگیری سفارش توسط خود مشتری

### 📱 مینی‌اپ تلگرام
- مینی‌اپ مشتری: خرید، کیف پول، تیکت، تخفیف، راهنما، باشگاه
- مینی‌اپ فروشگاه برای نماینده: فروش پلن، کیف فروشگاه، پشتیبانی
- برند هر فروشگاه (نام، لوگو، رنگ) و دامنهٔ جدا

### 🏪 نمایندگی
- پلن و فروشگاه نماینده؛ سهم / PAYG؛ ربات اختصاصی
- سقف حجم، فروش بیش از سقف با hold، همگام با پنل
- مدیریت نمایندگان از وب و بات؛ جستجوی نماینده در ربات

### 🛡️ پاسارگارد
- کاربران، ادمین، گروه، هاست، نود، تمپلیت
- سهمیه و نقش زنده از پاسارگارد
- ساخت کاربر از تمپلیت یا سفارشی

### 🖥️ پنل وب و ربات
- داشبورد، سفارش‌ها، تیکت، تنظیمات، راهنمای درون‌پنل (`/help`)
- یک کیبورد منو و یک صفحه در چت؛ «کیبورد همیشه باز» اختیاری
- جستجوی مشتری با نام/یوزرنیم/لینک اشتراک

---

## 🚀 نصب سریع

**منابع پیشنهادی:** ۲ گیگ رم و ۲ هسته CPU · Ubuntu 22.04+

### قبل از نصب این‌ها را آماده کنید
1. **دامنه** متصل به آی‌پی سرور
2. **توکن ربات**، **یوزرنیم ربات** و **آیدی ادمین** تلگرام
3. **اطلاعات ورود پنل پاسارگارد** (آدرس، یوزر، رمز)

### یک‌خطی (کانال پایدار / `main`)
```bash
bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/main/get.sh)
```

نصب پیش‌فرض از **`main` (پایدار 0.5)** است. برای کانال توسعه:

```bash
PGCLOCK_CHANNEL=dev bash <(curl -fsSL https://raw.githubusercontent.com/Mrclocks/PGClockBot/dev/get.sh)
```

بعد از نصب، راه‌اندازی در مرورگر کامل می‌شود: `http://SERVER_IP:9000/` (یا دامنهٔ شما).

### پایگاه داده (PostgreSQL)
نصب تازه فقط با **PostgreSQL** انجام می‌شود (SQLite برای نصب جدید ساخته نمی‌شود). اسکریپت نصب
در صورت نبود، PostgreSQL محلی را به‌صورت خودکار آماده می‌کند، سرویس را روشن می‌کند، کاربر و پایگاه
داده می‌سازد و `DATABASE_URL` را در `.env` می‌نویسد. `postgresql-client` هم برای پشتیبان‌گیری نصب می‌شود.

برای استفاده از PostgreSQL بیرونی یا مدیریت‌شده، پیش از نصب آدرس آن را بدهید؛ مقدار باید با
`postgresql://`، `postgresql+asyncpg://` یا `postgres://` شروع شود و از سرور قابل دسترس باشد:

```bash
PGCLOCK_DATABASE_URL='postgresql+asyncpg://USER:PASS@HOST:5432/DBNAME' bash pgclock.sh install main
```

اگر `.env` قبلی هنوز به SQLite اشاره می‌کند، نصب متوقف می‌شود و از شما می‌خواهد `DATABASE_URL`
را به PostgreSQL تغییر دهید.

### منوی اسکریپت (`pgclock.sh`)
دستور بالا منو را باز می‌کند. داخل پوشهٔ پروژه هم:

```bash
bash pgclock.sh
```

| گزینه | کار |
|------|-----|
| `1` Install main | نصب کانال پایدار (پیشنهادی) |
| `2` Install dev | نصب کانال توسعه |
| `3` Update main | آپدیت از `origin/main` (`.env` حفظ می‌شود) |
| `4` Update dev | آپدیت از `origin/dev` |
| `5` Edit .env | ویرایش توکن / پنل / پورت |
| `6` Web panel | آدرس پنل، ریست رمز، health |
| `7` Service | وضعیت / استارت / ری‌استارت / لاگ |
| `8` Status | خلاصهٔ سلامت |
| `9` Doctor | تشخیص فقط‌خواندنی |
| `10` Uninstall | حذف کامل |
| `11` Setup Xray | هستهٔ تست اتصال |
| `0` Exit | خروج |

میانبر بدون منو:

```bash
bash pgclock.sh install main
bash pgclock.sh update main
bash pgclock.sh status
bash pgclock.sh doctor
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
