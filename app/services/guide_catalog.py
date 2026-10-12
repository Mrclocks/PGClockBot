"""In-panel help catalog — short summaries + links into /help (or DOCS_BASE_URL).

Source of truth for topic metadata used by:
- ``page_title(..., help='plans')`` popovers in the web panel
- ``scripts/build_guide.py`` nav / search index
- ``docs/guide/*.md`` front-matter ``id`` fields

Keep summaries short (1–2 sentences). Full teaching lives in Markdown under docs/guide/.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# Roles that may see a topic in docs nav / filters.
# Panel "?" icons still show for whoever can open that page.
ROLE_ALL = ("owner", "admin", "reseller", "pg_staff", "principal")

TOPICS: dict[str, dict[str, Any]] = {
    "start": {
        "title": "شروع سریع",
        "summary": "نصب، ویزارد اولیه و اولین ورود به وب‌پنل را قدم‌به‌قدم یاد بگیرید.",
        "slug": "start",
        "panel": "/",
        "roles": list(ROLE_ALL),
        "nav_group": "شروع",
        "icon": "rocket",
        "aliases": ["نصب", "راه‌اندازی", "ویزارد", "setup", "install"],
    },
    "roles": {
        "title": "نقش‌ها و دسترسی‌ها",
        "summary": "مالک، نماینده، ادمین پاسارگارد و نمایندهٔ زیرمجموعه چه چیزی می‌بینند.",
        "slug": "roles",
        "panel": "/home",
        "roles": list(ROLE_ALL),
        "nav_group": "شروع",
        "icon": "shield",
        "aliases": ["دسترسی", "ادمین", "نماینده", "مالک", "pg_staff"],
    },
    "home": {
        "title": "داشبورد وب پنل",
        "summary": "نمای کلی وضعیت سرور، اعلان‌های مهم و میانبرهای روزمره از صفحهٔ داشبورد.",
        "slug": "home",
        "panel": "/home",
        "roles": list(ROLE_ALL),
        "nav_group": "وب پنل",
        "icon": "home",
        "aliases": ["خانه", "داشبورد", "overview"],
    },
    "inbox": {
        "title": "اعلان‌ها و مرکز اقدام",
        "summary": "رسیدهای منتظر، تیکت‌ها و هشدارهای انقضا/حجم را از یک جا ببینید و جمع کنید.",
        "slug": "inbox",
        "panel": "/inbox",
        "roles": list(ROLE_ALL),
        "nav_group": "وب پنل",
        "icon": "bell",
        "aliases": ["اعلان", "هشدار", "مرکز اقدام", "inbox"],
    },
    "settings-panel": {
        "title": "تنظیمات وب پنل",
        "summary": "بکاپ، امنیت ورود، PWA، SSL و آپدیت نسخه — تنظیمات خودِ پنل، نه ربات فروشگاه.",
        "slug": "settings-panel",
        "panel": "/settings?tab=backup",
        "roles": ["owner", "admin", "reseller", "pg_staff", "principal"],
        "nav_group": "وب پنل",
        "icon": "sliders",
        "aliases": ["بکاپ", "امنیت", "ssl", "pwa", "آپدیت", "رمز عبور"],
    },
    "bot-dashboard": {
        "title": "نمای کلی ربات",
        "summary": "آمار فروشگاه، سفارش‌ها و سلامت ربات تلگرام را یک‌جا ببینید.",
        "slug": "bot-dashboard",
        "panel": "/dashboard",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "gauge",
        "aliases": ["آمار ربات", "نمای کلی", "dashboard"],
    },
    "users": {
        "title": "کاربران ربات",
        "summary": "مشتریان تلگرام، موجودی کیف پول، سرویس‌ها و فیلترهای انقضا/حجم کم.",
        "slug": "users",
        "panel": "/users",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "users",
        "aliases": ["مشتری", "کاربر بات", "کیف پول"],
    },
    "finance": {
        "title": "مدیریت مالی",
        "summary": "سفارش‌ها، رسید کارت‌به‌کارت، درگاه و تأیید/رد پرداخت‌ها.",
        "slug": "finance",
        "panel": "/finance",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "wallet",
        "aliases": ["پرداخت", "رسید", "سفارش", "کارت به کارت", "درگاه"],
    },
    "plans": {
        "title": "پلن‌ها",
        "summary": "ساخت و ویرایش پلن فروش، اتصال به تمپلیت پاسارگارد و کد هدیه.",
        "slug": "plans",
        "panel": "/plans",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "layers",
        "aliases": ["تعرفه", "پکیج", "تمپلیت", "هدیه"],
    },
    "tickets": {
        "title": "پشتیبانی",
        "summary": "تیکت‌های کاربران ربات و گفتگوی داخلی نماینده با مالک.",
        "slug": "tickets",
        "panel": "/tickets",
        "roles": ["owner", "admin", "reseller", "pg_staff"],
        "nav_group": "پنل ربات",
        "icon": "life-buoy",
        "aliases": ["تیکت", "پشتیبانی", "چت"],
    },
    "broadcast": {
        "title": "پیام گروهی",
        "summary": "ارسال پیام همگانی به کاربران ربات با فیلتر مخاطب.",
        "slug": "broadcast",
        "panel": "/broadcast",
        "roles": ["owner", "admin"],
        "nav_group": "پنل ربات",
        "icon": "megaphone",
        "aliases": ["همگانی", "برودکست", "اعلان گروهی"],
    },
    "loyalty": {
        "title": "باشگاه مشتریان",
        "summary": "امتیاز، سطح، پاداش و گردونه — نگهداشت مشتری بعد از خرید.",
        "slug": "loyalty",
        "panel": "/loyalty",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "star",
        "aliases": ["امتیاز", "گردونه", "سطح", "پاداش"],
    },
    "message-variables": {
        "title": "متغیرهای پیام",
        "summary": "جایگزین‌هایی مثل {name} و {amount} که در متن‌های ربات پر می‌شوند.",
        "slug": "message-variables",
        "panel": "/message-variables",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "braces",
        "aliases": ["متغیر", "پلیس‌هولدر", "قالب پیام"],
    },
    "settings-bot": {
        "title": "تنظیمات ربات و فروشگاه",
        "summary": "متن‌ها، دکمه‌ها، پرداخت، منو، ظاهر و اعلان‌های ربات تلگرام.",
        "slug": "settings-bot",
        "panel": "/settings",
        "roles": ["owner", "admin", "reseller"],
        "nav_group": "پنل ربات",
        "icon": "bot",
        "aliases": ["تنظیمات فروشگاه", "متن ربات", "پرداخت", "منو"],
    },
    "resellers": {
        "title": "نمایندگان",
        "summary": "ساخت نماینده، پلن نمایندگی، دسترسی وب و ربات شخصی نماینده.",
        "slug": "resellers",
        "panel": "/resellers",
        "roles": ["owner", "admin", "principal"],
        "nav_group": "پنل ربات",
        "icon": "network",
        "aliases": ["نمایندگی", "ریسلر", "زیرمجموعه"],
    },
    "reseller-applications": {
        "title": "درخواست‌های نمایندگی",
        "summary": "بررسی و تأیید درخواست‌هایی که کاربر از داخل ربات فرستاده است.",
        "slug": "reseller-applications",
        "panel": "/reseller-applications",
        "roles": ["owner", "admin"],
        "nav_group": "پنل ربات",
        "icon": "inbox",
        "aliases": ["درخواست نماینده", "تأیید نمایندگی"],
    },
    "pg-overview": {
        "title": "نمای کلی پاسارگارد",
        "summary": "وضعیت اتصال به پنل پاسارگارد و میانبر بخش‌های زیرمجموعه.",
        "slug": "pg-overview",
        "panel": "/pg",
        "roles": ["owner", "admin", "pg_staff", "reseller"],
        "nav_group": "پاسارگارد",
        "icon": "server",
        "aliases": ["pasarguard", "اتصال pg"],
    },
    "pg-users": {
        "title": "کاربران پاسارگارد",
        "summary": "مدیریت یوزرهای VPN روی پاسارگارد از داخل وب‌پنل ربات.",
        "slug": "pg-users",
        "panel": "/pg/users",
        "roles": ["owner", "admin", "pg_staff", "reseller"],
        "nav_group": "پاسارگارد",
        "icon": "user",
        "aliases": ["یوزر vpn", "حجم", "انقضا"],
    },
    "pg-admins": {
        "title": "ادمین پاسارگارد",
        "summary": "ساخت ادمین فرعی، اعطای وب‌پنل و تبدیل به نماینده فروشگاه.",
        "slug": "pg-admins",
        "panel": "/pg/admins",
        "roles": ["owner", "admin"],
        "nav_group": "پاسارگارد",
        "icon": "key",
        "aliases": ["ادمین فرعی", "sudo", "نقش pg"],
    },
    "pg-nodes": {
        "title": "نودها",
        "summary": "مشاهده و مدیریت نودهای پاسارگارد و وضعیت سلامت آن‌ها.",
        "slug": "pg-nodes",
        "panel": "/pg/nodes",
        "roles": ["owner", "admin", "pg_staff"],
        "nav_group": "پاسارگارد",
        "icon": "cpu",
        "aliases": ["سرور", "نود", "node"],
    },
    "pg-groups": {
        "title": "گروه‌ها",
        "summary": "گروه‌های اینباند پاسارگارد برای اتصال پلن‌ها و محدودیت دسترسی.",
        "slug": "pg-groups",
        "panel": "/pg/groups",
        "roles": ["owner", "admin", "pg_staff"],
        "nav_group": "پاسارگارد",
        "icon": "boxes",
        "aliases": ["گروه", "group"],
    },
    "pg-inbounds": {
        "title": "اینباندها",
        "summary": "فهرست اینباندها و ارتباطشان با گروه‌ها و تمپلیت‌ها.",
        "slug": "pg-inbounds",
        "panel": "/pg/inbounds",
        "roles": ["owner", "admin", "pg_staff"],
        "nav_group": "پاسارگارد",
        "icon": "git-branch",
        "aliases": ["inbound", "ورود"],
    },
    "pg-hosts": {
        "title": "هاست‌ها",
        "summary": "هاست‌های نمایشی اشتراک و تنظیمات مرتبط در پاسارگارد.",
        "slug": "pg-hosts",
        "panel": "/pg/hosts",
        "roles": ["owner", "admin", "pg_staff"],
        "nav_group": "پاسارگارد",
        "icon": "globe",
        "aliases": ["host", "هاست"],
    },
    "pg-templates": {
        "title": "تمپلیت‌های کاربر",
        "summary": "تمپلیت‌های آماده پاسارگارد که پلن‌های فروشگاه به آن‌ها وصل می‌شوند.",
        "slug": "pg-templates",
        "panel": "/pg/templates",
        "roles": ["owner", "admin", "pg_staff", "reseller"],
        "nav_group": "پاسارگارد",
        "icon": "layout",
        "aliases": ["تمپلیت", "template"],
    },
    "funnel": {
        "title": "رفتار کاربر",
        "summary": "مسیر کاربر از ورود تا خرید را ببینید و نقاط ریزش را پیدا کنید.",
        "slug": "funnel",
        "panel": "/funnel",
        "roles": ["owner", "admin"],
        "nav_group": "پنل ربات",
        "icon": "filter",
        "aliases": ["فانل", "ریزش", "تبدیل"],
    },
    "troubleshooting": {
        "title": "عیب‌یابی سریع",
        "summary": "ربات جواب نمی‌دهد، لاگین وب، پورت ۹۰۰۰، پاسارگارد و خطاهای رایج دیگر.",
        "slug": "troubleshooting",
        "panel": "/home",
        "roles": list(ROLE_ALL),
        "nav_group": "کمک",
        "icon": "wrench",
        "aliases": ["خطا", "مشکل", "لاگ", "health", "doctor"],
    },
    "deploy-docs": {
        "title": "انتشار راهنما روی دامنه خودتان",
        "summary": "چطور خروجی راهنما را روی سرور و دامنه شخصی بالا بیاورید.",
        "slug": "deploy-docs",
        "icon": "upload",
        "panel": "/help/",
        "roles": ["owner", "admin"],
        "nav_group": "کمک",
        "aliases": ["دامنه", "nginx", "host", "github pages", "docs"],
    },
}


NAV_ORDER: list[str] = [
    "start",
    "roles",
    "home",
    "inbox",
    "settings-panel",
    "bot-dashboard",
    "users",
    "finance",
    "plans",
    "tickets",
    "broadcast",
    "loyalty",
    "message-variables",
    "settings-bot",
    "resellers",
    "reseller-applications",
    "funnel",
    "pg-overview",
    "pg-users",
    "pg-admins",
    "pg-nodes",
    "pg-groups",
    "pg-inbounds",
    "pg-hosts",
    "pg-templates",
    "troubleshooting",
    "deploy-docs",
]


def topic(topic_id: str) -> dict[str, Any] | None:
    raw = TOPICS.get((topic_id or "").strip())
    if not raw:
        return None
    return {"id": topic_id, **raw}


def nav_topics() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tid in NAV_ORDER:
        t = topic(tid)
        if t:
            out.append(t)
    return out


def docs_base_url() -> str:
    """Public docs origin when set; otherwise same-origin /help."""
    try:
        from app.config import get_settings

        base = (get_settings().docs_base_url or "").strip().rstrip("/")
        return base
    except Exception:
        return ""


def help_href(topic_id: str) -> str:
    """Absolute-or-relative URL to a guide page for panel links."""
    t = topic(topic_id)
    if not t:
        return "/help/"
    slug = t["slug"]
    base = docs_base_url()
    if base:
        return f"{base}/{slug}/"
    return f"/help/{slug}/"


@lru_cache(maxsize=1)
def panel_help_payload() -> dict[str, dict[str, str]]:
    """Compact map for templates/JS: id → title, summary, href."""
    out: dict[str, dict[str, str]] = {}
    for tid in TOPICS:
        t = topic(tid)
        if not t:
            continue
        out[tid] = {
            "title": t["title"],
            "summary": t["summary"],
            "href": help_href(tid),
        }
    return out
