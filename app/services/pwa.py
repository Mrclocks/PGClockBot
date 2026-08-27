"""Progressive Web App (installable panel) helpers."""

from __future__ import annotations

import logging
from io import BytesIO
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import DATA_DIR, ROOT_DIR

logger = logging.getLogger(__name__)

DEFAULT_NAME = "MrClockBot"
DEFAULT_SHORT_NAME = "MrClockBot"
DEFAULT_DESCRIPTION = "پنل مدیریت فروشگاه"
PWA_DIR = DATA_DIR / "pwa"
ICON_NAME = "icon.png"
SETTING_NAME = "pwa_name"
SETTING_SHORT = "pwa_short_name"
SETTING_DESC = "pwa_description"
SETTING_ICON = "pwa_icon"  # relative filename under data/pwa, or empty for default

STATIC_LOGO = ROOT_DIR / "app" / "web" / "static" / "logo.png"
SIZES = (192, 512)


def ensure_pwa_dir() -> Path:
    PWA_DIR.mkdir(parents=True, exist_ok=True)
    return PWA_DIR


def custom_icon_path() -> Path | None:
    path = PWA_DIR / ICON_NAME
    return path if path.is_file() else None


def source_icon_path() -> Path:
    custom = custom_icon_path()
    if custom:
        return custom
    if STATIC_LOGO.is_file():
        return STATIC_LOGO
    # last resort: any existing generated icon
    for size in SIZES:
        candidate = PWA_DIR / f"icon-{size}.png"
        if candidate.is_file():
            return candidate
    return STATIC_LOGO


def _resize_png(src: Path, size: int, *, maskable: bool = False) -> bytes:
    from PIL import Image

    img = Image.open(src).convert("RGBA")
    if maskable:
        # Safe zone ~80% for maskable icons
        canvas = Image.new("RGBA", (size, size), (9, 9, 11, 255))
        inner = int(size * 0.8)
        img = img.resize((inner, inner), Image.Resampling.LANCZOS)
        offset = (size - inner) // 2
        canvas.paste(img, (offset, offset), img)
        out = canvas
    else:
        out = img.resize((size, size), Image.Resampling.LANCZOS)

    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def icon_bytes(size: int, *, maskable: bool = False) -> bytes:
    src = source_icon_path()
    if not src.is_file():
        # 1x1 transparent fallback
        from PIL import Image

        buf = BytesIO()
        Image.new("RGBA", (size, size), (9, 9, 11, 255)).save(buf, format="PNG")
        return buf.getvalue()
    try:
        return _resize_png(src, size, maskable=maskable)
    except Exception:
        logger.exception("PWA icon resize failed")
        return src.read_bytes() if src.suffix.lower() == ".png" else b""


def save_uploaded_icon(data: bytes) -> str:
    ensure_pwa_dir()
    dest = PWA_DIR / ICON_NAME
    # Normalize via PIL so we always store PNG
    from PIL import Image

    img = Image.open(BytesIO(data)).convert("RGBA")
    # Downscale huge uploads
    max_edge = 1024
    if max(img.size) > max_edge:
        img.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    img.save(dest, format="PNG", optimize=True)
    # Bust cached sized icons
    for size in SIZES:
        for name in (f"icon-{size}.png", f"icon-{size}-maskable.png", "apple-touch-icon.png"):
            p = PWA_DIR / name
            if p.is_file():
                p.unlink(missing_ok=True)
    return ICON_NAME


def clear_custom_icon() -> None:
    ensure_pwa_dir()
    for name in (
        ICON_NAME,
        "icon-192.png",
        "icon-512.png",
        "icon-512-maskable.png",
        "apple-touch-icon.png",
    ):
        p = PWA_DIR / name
        if p.is_file():
            p.unlink(missing_ok=True)


def _meta_path() -> Path:
    return ensure_pwa_dir() / "meta.json"


def read_pwa_meta() -> dict[str, Any]:
    try:
        path = _meta_path()
        if path.is_file():
            import json

            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("pwa meta read failed", exc_info=True)
    return {}


def write_pwa_meta(payload: dict[str, Any]) -> None:
    try:
        import json

        path = _meta_path()
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("pwa meta write failed", exc_info=True)


_DISPLAY_NAME_CACHE: tuple[float, str] | None = None
_DISPLAY_NAME_TTL = 30.0


def panel_display_name() -> str:
    global _DISPLAY_NAME_CACHE
    import time

    now = time.monotonic()
    if _DISPLAY_NAME_CACHE and (now - _DISPLAY_NAME_CACHE[0]) < _DISPLAY_NAME_TTL:
        return _DISPLAY_NAME_CACHE[1]
    name = str(read_pwa_meta().get("name") or "").strip() or DEFAULT_NAME
    _DISPLAY_NAME_CACHE = (now, name)
    return name


async def load_pwa_settings(session: AsyncSession) -> dict[str, Any]:
    from app.services.users import get_setting

    name = (await get_setting(session, SETTING_NAME, "")).strip()
    short = (await get_setting(session, SETTING_SHORT, "")).strip()
    desc = (await get_setting(session, SETTING_DESC, "")).strip()
    icon_key = (await get_setting(session, SETTING_ICON, "")).strip()
    has_custom = bool(custom_icon_path())
    cfg = {
        "name": name or DEFAULT_NAME,
        "short_name": short or name or DEFAULT_SHORT_NAME,
        "description": desc or DEFAULT_DESCRIPTION,
        "name_raw": name,
        "short_name_raw": short,
        "description_raw": desc,
        "has_custom_icon": has_custom,
        "icon_preview": "/pwa/icon/192",
        "using_defaults": not name and not short and not has_custom,
        "icon_key": icon_key,
    }
    write_pwa_meta(
        {
            "name": cfg["name"],
            "short_name": cfg["short_name"],
            "description": cfg["description"],
            "has_custom_icon": has_custom,
        }
    )
    return cfg


async def save_pwa_from_form(session: AsyncSession, form: Any) -> tuple[bool, str]:
    from app.services.users import set_setting

    name = str(form.get("pwa_name") or "").strip()[:80]
    short = str(form.get("pwa_short_name") or "").strip()[:40]
    desc = str(form.get("pwa_description") or "").strip()[:160]

    if form.get("pwa_icon_clear"):
        clear_custom_icon()
        await set_setting(session, SETTING_ICON, "")
    else:
        upload = form.get("pwa_icon")
        if upload is not None and getattr(upload, "filename", None):
            raw = await upload.read()
            if raw:
                if len(raw) > 2_500_000:
                    return False, "حجم آیکن حداکثر ۲٫۵ مگابایت باشد"
                try:
                    save_uploaded_icon(raw)
                    await set_setting(session, SETTING_ICON, ICON_NAME)
                except Exception as exc:
                    logger.exception("pwa icon save failed")
                    return False, f"آیکن نامعتبر است: {exc}"

    await set_setting(session, SETTING_NAME, name)
    await set_setting(session, SETTING_SHORT, short)
    await set_setting(session, SETTING_DESC, desc)
    write_pwa_meta(
        {
            "name": name or DEFAULT_NAME,
            "short_name": short or name or DEFAULT_SHORT_NAME,
            "description": desc or DEFAULT_DESCRIPTION,
            "has_custom_icon": bool(custom_icon_path()),
        }
    )
    return True, "تنظیمات وب‌اپ ذخیره شد"


def build_manifest(cfg: dict[str, Any]) -> dict[str, Any]:
    name = (cfg.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME
    short = (cfg.get("short_name") or name).strip() or name
    desc = (cfg.get("description") or DEFAULT_DESCRIPTION).strip() or DEFAULT_DESCRIPTION
    return {
        "name": name,
        "short_name": short[:40],
        "description": desc,
        "start_url": "/home",
        "scope": "/",
        "display": "standalone",
        "id": "/home",
        "orientation": "any",
        "background_color": "#09090b",
        "theme_color": "#09090b",
        "lang": "fa",
        "dir": "rtl",
        "icons": [
            {
                "src": "/pwa/icon/192",
                "sizes": "192x192",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/pwa/icon/512",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "any",
            },
            {
                "src": "/pwa/icon/512?maskable=1",
                "sizes": "512x512",
                "type": "image/png",
                "purpose": "maskable",
            },
        ],
    }


def service_worker_js() -> str:
    return """/* PGClockBot panel service worker — static shell only */
const CACHE = 'pgclock-shell-v31';
const PRECACHE = [
  '/static/logo.png',
  '/static/logo-64.png',
  '/manifest.webmanifest'
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  /* Do NOT clients.claim() here — mid-load takeover aborts in-flight CSS/font
     requests on iOS Safari (blank/unstyled panel). */
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
});

function isVersionedPanelAsset(url) {
  return url.pathname === '/static/panel.css'
    || url.pathname === '/static/panel.js'
    || url.pathname === '/static/fonts.css'
    || url.pathname.indexOf('/static/fonts/') === 0;
}

/* When network fails for ?v=8.5.xx URLs, fall back to any cached same-pathname
   asset so the panel is never left unstyled/unscripted. */
function matchIgnoreSearch(req) {
  return caches.open(CACHE).then((cache) =>
    cache.match(req).then((hit) => {
      if (hit) return hit;
      return cache.keys().then((keys) => {
        const want = new URL(req.url).pathname;
        for (const k of keys) {
          try {
            if (new URL(k.url).pathname === want) return cache.match(k);
          } catch (e) {}
        }
        return undefined;
      });
    })
  );
}

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (!url.pathname.startsWith('/static/') && !url.pathname.startsWith('/pwa/')) return;

  if (isVersionedPanelAsset(url)) {
    event.respondWith(
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => matchIgnoreSearch(req))
    );
    return;
  }

  event.respondWith(
    caches.match(req).then((hit) =>
      hit ||
      fetch(req).then((res) => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }).catch(() => hit)
    )
  );
});
"""
