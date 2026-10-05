"""Redact secrets from logs and user-facing error strings."""

from __future__ import annotations

import re

# Standalone tokens and Telegram API ``/bot<token>/`` URL forms.
_BOT_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{20,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~\-+/=]{8,}")
_PASSWORD_QS_RE = re.compile(
    r"(?i)(password|passwd|pwd|token|secret|api[_-]?key|access_token|refresh_token)=([^&\s\"']+)"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("?(?:password|passwd|pwd|token|secret|api[_-]?key|access_token|refresh_token|authorization)"?\s*:\s*)("(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[^\s,}\]]+)'
)
_COOKIE_RE = re.compile(r"(?i)(cookie\s*[:=]\s*)([^\n]+)")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)([^\n]+)")
_BASIC_AUTH_RE = re.compile(r"(://)([^:/@\s]+):([^@/\s]+)(@)")
_GATE_QS_RE = re.compile(r"(?i)([?&]gate=)([^&\s]+)")


def redact(text: object, *, limit: int = 400) -> str:
    s = "" if text is None else str(text)
    s = _BOT_TOKEN_RE.sub("<bot-token>", s)
    s = _BEARER_RE.sub(r"\1<redacted>", s)
    s = _PASSWORD_QS_RE.sub(r"\1=<redacted>", s)
    s = _JSON_SECRET_RE.sub(r"\1\"<redacted>\"", s)
    s = _COOKIE_RE.sub(r"\1<redacted>", s)
    s = _AUTH_HEADER_RE.sub(r"\1<redacted>", s)
    s = _BASIC_AUTH_RE.sub(r"\1\2:<redacted>\4", s)
    s = _GATE_QS_RE.sub(r"\1<redacted>", s)
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def user_safe_error(
    exc: object,
    *,
    fallback: str = "خطای داخلی. دوباره تلاش کنید.",
    limit: int = 180,
) -> str:
    """Persian, secret-safe message for Telegram user-facing replies.

    Prefer PasarGuard ``user_message`` / ``friendly_pg_error`` so operators see
    clear causes (duplicate username, timeout, 403) instead of raw English.
    Still redacts tokens and never echoes tracebacks.
    """
    from app.services.credential_policy import friendly_pg_error

    status_code = getattr(exc, "status_code", None)
    status_code = status_code if isinstance(status_code, int) else None

    msg = ""
    if callable(getattr(exc, "user_message", None)):
        try:
            msg = str(exc.user_message(fallback="") or "").strip()
        except Exception:
            msg = ""
    if not msg:
        msg = "" if exc is None else str(exc)

    msg = friendly_pg_error(msg, status_code=status_code)
    msg = redact(msg, limit=limit).strip()
    if not msg:
        return fallback
    low = msg.lower()
    if "traceback" in low or "/app/" in msg or 'file "' in low:
        return fallback
    # Unmapped English HTTP/API noise → generic Persian, never raw status lines.
    if re.search(r"\b(failed\s*\(|traceback|exception)\b", low) and not re.search(
        r"[\u0600-\u06FF]", msg
    ):
        return fallback
    return msg

