"""«کاهش» / «افزایش» day and GB changes from admin forms (reseller capacity, a customer's service remaining).

The forms ask for positive amounts and a mode, like the wallet; the services below take a signed change.
"""

from __future__ import annotations

ADJUST_MODES = ("credit", "debit")


def signed_adjustment(mode: str | None, *, days: int, gb: float) -> tuple[int, float]:
    """Signed day and GB change for ``mode`` (``debit`` = «کاهش», ``credit`` = «افزایش»).

    Without a mode the amounts are already signed (older form).
    """
    if not mode:
        return days, gb
    if mode not in ADJUST_MODES:
        raise ValueError("نوع تغییر نامعتبر است")
    if days < 0 or gb < 0:
        raise ValueError("روز و گیگ را مثبت وارد کنید و کاهش یا افزایش را بزنید")
    if days == 0 and gb == 0:
        raise ValueError("روز یا گیگ را وارد کنید")
    sign = -1 if mode == "debit" else 1
    return sign * days, sign * gb
