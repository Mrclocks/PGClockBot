"""Normalize and parse numeric text across web + bot (EN / Persian / Arabic digits)."""
from __future__ import annotations

_FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_TO_FA_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def to_fa_digits(value: int | str | None) -> str:
    """Map ASCII digits to Persian digits for operator-facing labels."""
    return str(value if value is not None else "").translate(_TO_FA_DIGITS)


def normalize_digits(text: str | None) -> str:
    """Map Persian/Arabic digits to ASCII and strip common thousand separators/spaces.

    Decimal separators (ASCII ``.``, Persian ``٫``, Arabic ``،``) are left for
    callers that need float parsing — use :func:`normalize_number_text` instead
    when both digit and decimal normalization are required.
    """
    raw = (text or "").strip().translate(_FA_DIGITS).translate(_AR_DIGITS)
    return raw.replace(",", "").replace("٬", "").replace(" ", "").replace("\u00a0", "")


def normalize_number_text(text: str | None) -> str:
    """Normalize digits + separators so ``int``/``float`` can parse the value."""
    raw = normalize_digits(text).replace("٫", ".").replace("،", ".")
    # Collapse accidental double dots from mixed separators
    if raw.count(".") > 1:
        parts = raw.split(".")
        raw = "".join(parts[:-1]) + "." + parts[-1]
    return raw


def parse_int(text: str | None, *, default: int | None = None) -> int:
    raw = normalize_number_text(text)
    if not raw:
        if default is not None:
            return default
        raise ValueError("empty")
    # Allow "12.0" style from number inputs
    if "." in raw:
        return int(float(raw))
    return int(raw)


def parse_float(text: str | None, *, default: float | None = None) -> float:
    raw = normalize_number_text(text)
    if not raw:
        if default is not None:
            return default
        raise ValueError("empty")
    return float(raw)


def parse_optional_int(text: str | None) -> int | None:
    raw = normalize_number_text(text)
    if not raw:
        return None
    return parse_int(raw)


def parse_optional_float(text: str | None) -> float | None:
    raw = normalize_number_text(text)
    if not raw:
        return None
    return parse_float(raw)
