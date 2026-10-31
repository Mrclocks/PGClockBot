"""Trial contact gate — HMAC phone hashes, Iran MSISDN check, never log raw phones."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re

log = logging.getLogger(__name__)

_DIGITS_RE = re.compile(r"\D+")


def normalize_phone_digits(raw: str | None) -> str:
    """Strip to digits only. Does not log or return formatted MSISDN with +."""
    s = _DIGITS_RE.sub("", (raw or "").strip())
    # Drop leading 00 international prefix
    if s.startswith("00"):
        s = s[2:]
    return s


def is_iran_mobile(digits: str) -> bool:
    """Accept 98XXXXXXXXXX (12) or 09XXXXXXXXX (11) Iranian mobiles."""
    d = normalize_phone_digits(digits)
    if not d:
        return False
    if d.startswith("98") and len(d) == 12 and d[2] == "9":
        return True
    if d.startswith("9") and len(d) == 10:
        return True
    if d.startswith("09") and len(d) == 11:
        return True
    return False


def canonical_iran_phone(digits: str) -> str:
    """Normalize to 98XXXXXXXXXX for hashing. Raises ValueError if not Iranian mobile."""
    d = normalize_phone_digits(digits)
    if d.startswith("09") and len(d) == 11:
        d = "98" + d[1:]
    elif d.startswith("9") and len(d) == 10:
        d = "98" + d
    if not (d.startswith("98") and len(d) == 12 and d[2] == "9"):
        raise ValueError("شماره موبایل ایران معتبر نیست")
    return d


def phone_hmac_sha256(digits: str, *, secret: str) -> str:
    """HMAC-SHA256 hex digest of canonical digits. Never log ``digits`` or digest with phone."""
    key = (secret or "").encode("utf-8")
    if not key:
        raise ValueError("secret missing")
    # Hash whatever canonical form we have; callers should canonicalize for Iran gate.
    payload = normalize_phone_digits(digits).encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def hash_trial_phone(raw_phone: str, *, secret: str, require_iran: bool) -> str:
    """Return phone_hash for trial claim storage."""
    digits = normalize_phone_digits(raw_phone)
    if not digits:
        raise ValueError("شماره تماس دریافت نشد")
    if require_iran:
        digits = canonical_iran_phone(digits)
    elif len(digits) < 8:
        raise ValueError("شماره تماس نامعتبر است")
    elif is_iran_mobile(digits):
        # One person, one hash: 0912…, 98912… and 0098912… must not look like three phones.
        digits = canonical_iran_phone(digits)
    return phone_hmac_sha256(digits, secret=secret)
