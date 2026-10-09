#!/usr/bin/env python3
"""Write .env safely from JSON on stdin (no shell escaping issues)."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    data = json.load(sys.stdin)
    root = Path(__file__).resolve().parent.parent
    path = root / ".env"
    order = [
        "BOT_TOKEN",
        "BOT_USERNAME",
        "ADMIN_IDS",
        "PG_BASE_URL",
        "PG_USERNAME",
        "PG_PASSWORD",
        "PG_API_KEY",
        "WEB_HOST",
        "WEB_PORT",
        "WEB_SECRET",
        "WEB_ADMIN_USER",
        "WEB_ADMIN_PASSWORD",
        "DATABASE_URL",
        "WEBHOOK_URL",
        "WEBHOOK_PATH",
        "PUBLIC_BASE_URL",
        "CURRENCY",
        "DEFAULT_LOCALE",
    ]
    lines: list[str] = []
    for key in order:
        val = "" if data.get(key) is None else str(data.get(key))
        # Never allow accidental newlines to break .env / logins
        val = val.replace("\r", "").strip()
        # Always use double quotes; escape backslash and quotes
        escaped = val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        lines.append(f'{key}="{escaped}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    print(str(path))


if __name__ == "__main__":
    main()
