#!/usr/bin/env python3
"""Reset web panel login (writes data/web_admin.json; clears plaintext .env password)."""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.web_auth import save_web_admin, validate_password_strength  # noqa: E402


PASSWORD_RULES = (
    "Password rejected. It needs at least 12 characters, 2 digits, 2 uppercase and 2 lowercase "
    "English letters and one special character; no double quote (\"), not the username, not a "
    "sample password, at most 72 bytes."
)

def _sync_env(user: str) -> None:
    """Keep WEB_ADMIN_USER in sync; never persist plaintext WEB_ADMIN_PASSWORD.

    Canonical credentials live in data/web_admin.json (bcrypt). Leaving a
    plaintext password in .env is a backup/leak risk.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    text = env_path.read_text(encoding="utf-8")

    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "").replace("\r", "")

    def upsert(src: str, key: str, value: str) -> str:
        line = f'{key}="{esc(value)}"'
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
        if pattern.search(src):
            return pattern.sub(line, src)
        return src.rstrip() + "\n" + line + "\n"

    def clear_password(src: str) -> str:
        pattern = re.compile(r"^WEB_ADMIN_PASSWORD=.*$", re.M)
        if pattern.search(src):
            return pattern.sub('WEB_ADMIN_PASSWORD=""', src)
        return src

    text = upsert(text, "WEB_ADMIN_USER", user)
    text = clear_password(text)
    env_path.write_text(text, encoding="utf-8")


def main() -> None:
    print("Reset web panel login")
    print("(saved to data/web_admin.json; .env password cleared)")
    user = input("Web username [admin]: ").strip() or "admin"
    while True:
        p1 = getpass.getpass("New password: ").replace("\r", "").strip()
        if not p1:
            print("Password cannot be empty.")
            continue
        p2 = getpass.getpass("Confirm password: ").replace("\r", "").strip()
        if p1 != p2:
            print("Passwords do not match.")
            continue
        ok, _persian_reason = validate_password_strength(p1, username=user)
        if not ok:
            # The rule text is Persian (web panel); the terminal gets the same rules in English.
            print(PASSWORD_RULES)
            continue
        break
    path = save_web_admin(user, p1)
    _sync_env(user)
    print(f"Saved: {path}")
    print("Restart recommended:")
    print("  sudo systemctl restart pgclockbot")


if __name__ == "__main__":
    main()
