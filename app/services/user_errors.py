"""Customer- and staff-facing error text: one place that turns an exception into a safe Persian message.

Services raise ``ValueError`` (or a domain error) with a Persian sentence for expected rule
violations. Anything else, including a stdlib ``ValueError`` such as ``invalid literal for int()``,
must never reach a person as raw text. ``domain_error_text`` passes through only text that is
already Persian and secret-free; everything else becomes a generic message with a short
reference that support can match against the server log.
"""

from __future__ import annotations

import logging
import re
import secrets

from app.services.redact import redact

logger = logging.getLogger(__name__)

ERROR_REF_PREFIX = "E-"
ERROR_REF_BYTES = 3
DOMAIN_ERROR_LIMIT = 300
GENERIC_ERROR_TEXT = "خطایی رخ داد. دوباره تلاش کنید."
PANEL_MISSING_TEXT = "این مورد در پاسارگارد وجود ندارد یا از پنل حذف شده است."

_PERSIAN_LETTER_RE = re.compile(r"[؀-ۿ]")
_ENGLISH_NOISE_RE = re.compile(
    r"(traceback|invalid literal|object has no attribute|\bNoneType\b|\bKeyError\b|"
    r"\bsqlalchemy\b|\bhttpx\b|\basyncpg\b|\baiosqlite\b|\bstatus[_ ]code\b)",
    re.IGNORECASE,
)


def new_error_ref() -> str:
    """Short opaque reference shown to the person and written next to the logged detail."""
    return ERROR_REF_PREFIX + secrets.token_hex(ERROR_REF_BYTES).upper()


def with_error_ref(text: str, ref: str) -> str:
    return f"{text} (کد پیگیری: {ref})"


def domain_error_text(
    exc: BaseException,
    *,
    fallback: str = GENERIC_ERROR_TEXT,
    limit: int = DOMAIN_ERROR_LIMIT,
) -> str:
    """The Persian message of an expected rule violation, or ``fallback`` plus a log reference.

    The raw detail is logged with the reference, never shown.
    """
    text = redact(str(exc), limit=limit).strip()
    if text and _PERSIAN_LETTER_RE.search(text) and not _ENGLISH_NOISE_RE.search(text):
        return text
    ref = new_error_ref()
    logger.warning("unexpected error text hidden ref=%s type=%s", ref, type(exc).__name__, exc_info=exc)
    return with_error_ref(fallback, ref)
