"""Precompute browser cache keys for panel assets when the process starts.

Static files are served as immutable for a week, so every stylesheet and script URL carries a key
derived from the file's content: an edited file gets a new URL even when the version number is the same.
"""

from __future__ import annotations

from pathlib import Path

from app.py_compat import file_digest

_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
_SCRIPT_NAMES = (
    "panel.js",
    "panel_widget_request.js",
    "panel_ok_flash.js",
    "money-inputs.js",
    "list-inputs.js",
    "sales-chart.js",
)


def build_asset_cache_key(path: Path) -> str:
    with path.open("rb") as source:
        return file_digest(source, "sha256").hexdigest()[:16]


# Startup computation avoids filesystem work on menu/refresh requests.
_CACHE_KEYS = {name: build_asset_cache_key(_STATIC_DIR / name) for name in _SCRIPT_NAMES}


_ASSET_SUFFIXES = (".css", ".js")
# Every stylesheet and script under /static (relative path -> content key), read once at startup.
_ASSET_KEYS = {
    path.relative_to(_STATIC_DIR).as_posix(): build_asset_cache_key(path)
    for path in sorted(_STATIC_DIR.rglob("*"))
    if path.is_file() and path.suffix in _ASSET_SUFFIXES and "guide" not in path.relative_to(_STATIC_DIR).parts[:1]
}


def asset_version(name: str, fallback: str = "") -> str:
    """Cache key for ``/static/<name>``; ``fallback`` (the app version) for files not known at startup."""
    return _ASSET_KEYS.get(name, fallback)


def get_panel_asset_cache_key(name: str) -> str:
    try:
        return _CACHE_KEYS[name]
    except KeyError:
        raise ValueError("Unsupported panel script") from None
