"""Unified credential / password policy (Phase D1).

Source of truth: PasarGuard ``PasswordValidator`` / ``UserValidator``
(``PasarGuard/panel`` ``app/models/validators.py``).

Do not invent stricter rules beyond PasarGuard + our placeholder ban.
On manual create/edit, reject early with clear Persian causes.
"""

from __future__ import annotations

import re

# PasarGuard special-character class (must match panel PasswordValidator).
_PG_SPECIAL_RE = re.compile(r"[!@#$%^&*()\-_=+\[\]{}|;:,.<>?/~`]")
_PG_SPECIAL_CHARS = "!@#$%^&*()-_=+[]{}|;:,.<>?/~`"

# PasarGuard UserValidator.validate_username
_PG_USERNAME_RE = re.compile(r"^[a-zA-Z0-9-_@.]+$")
_PG_USERNAME_CONSEC_SPECIAL_RE = re.compile(r"[-_@.]{2,}")

# Business / conflict API errors → standalone Persian (no validation wrapper).
_PG_BUSINESS_MSG_MAP: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(username|user).{0,40}(already\s*(exists|taken|in\s*use)|exists|taken|duplicate)"
            r"|(already\s*(exists|taken)|duplicate).{0,40}(username|user)"
            r"|user\s+already\s+exists|username\s+already\s+exists",
            re.I,
        ),
        "این نام کاربری قبلاً ثبت شده است. یک نام دیگر بفرستید.",
    ),
    (
        re.compile(
            r"unique\s*constraint|duplicate\s*key|(?:username|user).{0,20}conflict",
            re.I,
        ),
        "این نام کاربری قبلاً ثبت شده است. یک نام دیگر بفرستید.",
    ),
    (
        re.compile(r"permission\s*denied|not\s*allowed|forbidden|access\s*denied", re.I),
        "اجازه این عمل را ندارید.",
    ),
    (
        re.compile(r"template.{0,30}(not\s*found|invalid|required)|invalid\s*template", re.I),
        "تمپلیت نامعتبر است یا در دسترس نیست.",
    ),
    (
        re.compile(r"group.{0,30}(not\s*found|invalid|required)|invalid\s*group", re.I),
        "گروه انتخاب‌شده نامعتبر است یا در دسترس نیست.",
    ),
    (
        re.compile(
            r"\bgroup_ids?\b.{0,80}((field\s*)?required|must\s*not\s*be\s*empty|ensure\s*this\s*value)"
            r"|((field\s*)?required|missing).{0,40}\bgroup_ids?\b",
            re.I,
        ),
        "گروه پاسارگارد برای این پلن تنظیم نشده یا خالی است — در ویرایش پلن گروه را انتخاب کنید.",
    ),
    (
        re.compile(
            r"\b(expire|data_limit|status|proxy_settings)\b.{0,60}(invalid|not\s*valid|required)",
            re.I,
        ),
        "یکی از فیلدهای محدودیت سرویس (انقضا/حجم/وضعیت) برای پاسارگارد نامعتبر است.",
    ),
]

# English fragments from PasarGuard / pydantic validation → Persian cause
_PG_API_MSG_MAP: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"Username only can be 3 to 128 characters\.?", re.I),
        "طول نام کاربری باید ۳ تا ۱۲۸ کاراکتر باشد",
    ),
    (
        re.compile(
            r"Username can only contain alphanumeric characters, -, _, @, and \.?",
            re.I,
        ),
        "نام کاربری فقط حروف انگلیسی، عدد و نمادهای - _ @ . را می‌پذیرد",
    ),
    (
        re.compile(r"Username cannot have consecutive special characters\.?", re.I),
        "نمادهای ویژه (- _ @ .) نباید پشت‌سرهم در نام کاربری بیایند",
    ),
    (
        re.compile(r"Password too long: maximum 72 bytes when UTF-8 encoded\.?", re.I),
        "رمز عبور حداکثر ۷۲ بایت (UTF-8) باشد",
    ),
    (
        re.compile(r"Password must be at least 12 characters long\.?", re.I),
        "رمز عبور باید حداقل ۱۲ کاراکتر باشد",
    ),
    (
        re.compile(r"Password must contain at least 2 digits\.?", re.I),
        "رمز عبور باید حداقل دو رقم داشته باشد",
    ),
    (
        re.compile(r"Password must contain at least 2 uppercase letters\.?", re.I),
        "رمز عبور باید حداقل دو حرف بزرگ انگلیسی داشته باشد",
    ),
    (
        re.compile(r"Password must contain at least 2 lowercase letters\.?", re.I),
        "رمز عبور باید حداقل دو حرف کوچک انگلیسی داشته باشد",
    ),
    (
        re.compile(r"Password must contain at least one special character\.?", re.I),
        "رمز عبور باید حداقل یک کاراکتر خاص مجاز داشته باشد",
    ),
    (
        re.compile(r"Password cannot contain the username\.?", re.I),
        "رمز عبور نباید شامل نام کاربری باشد",
    ),
    (
        re.compile(
            r'Password cannot contain the double quote \(?"?\)? character\.?',
            re.I,
        ),
        'رمز عبور نباید شامل کاراکتر " باشد',
    ),
    (
        re.compile(r"\bfield\s*required\b", re.I),
        "یک فیلد الزامی خالی است",
    ),
    (
        re.compile(r"\binput\s*should\s*be\s*a\s*valid\b", re.I),
        "مقدار یکی از فیلدها معتبر نیست",
    ),
]


def _format_issues(header: str, errors: list[str]) -> str:
    if len(errors) == 1:
        return f"{header} {errors[0]}"
    numbered = " ".join(f"({i}) {e}" for i, e in enumerate(errors, 1))
    return f"{header} {numbered}"


def validate_pg_username(
    username: str,
    *,
    lowercase: bool = False,
) -> tuple[str, str | None]:
    """Return ``(cleaned_username, persian_error_or_None)``.

    Mirrors PasarGuard ``UserValidator.validate_username`` (len 3–128,
    charset ``[a-zA-Z0-9-_@.]``, no consecutive specials).
    """
    u = (username or "").replace("\r", "").strip()
    if lowercase:
        u = u.lower()

    errors: list[str] = []
    if not u:
        errors.append("نام کاربری الزامی است.")
    else:
        n = len(u)
        if not (3 <= n <= 128):
            errors.append(
                f"طول باید بین ۳ تا ۱۲۸ کاراکتر باشد (الان {n} کاراکتر است)."
            )
        if not _PG_USERNAME_RE.fullmatch(u):
            errors.append(
                "فقط حروف انگلیسی، عدد و نمادهای - _ @ . مجاز است "
                "(فاصله، فارسی، یا سایر نمادها قبول نیست)."
            )
        elif _PG_USERNAME_CONSEC_SPECIAL_RE.search(u):
            errors.append(
                "نمادهای ویژه (- _ @ .) نباید پشت‌سرهم بیایند "
                "(مثلاً «a__b» یا «user..1» غیرمجاز است)."
            )

    if errors:
        return u, _format_issues(
            "نام کاربری با محدودیت‌های پاسارگارد هماهنگ نیست:",
            errors,
        )
    return u, None


def validate_password_strength(
    password: str,
    *,
    username: str | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, persian_error)``. Empty error when ok.

    Mirrors PasarGuard admin password rules; optional username ban matches PG
    ``check_username`` behavior. On failure, lists every violated rule so the
    operator knows exactly what to fix.
    """
    from app.services.security_policy import is_placeholder_password

    p = password or ""
    if not p:
        return False, "رمز عبور الزامی است."

    errors: list[str] = []
    encoded_len = len(p.encode("utf-8"))
    if encoded_len > 72:
        errors.append(
            f"حداکثر ۷۲ بایت UTF-8 مجاز است (الان {encoded_len} بایت)."
        )
    if len(p) < 12:
        errors.append(f"حداقل ۱۲ کاراکتر لازم است (الان {len(p)} کاراکتر).")
    digit_n = len(re.findall(r"\d", p))
    if digit_n < 2:
        errors.append(f"حداقل دو رقم لازم است (الان {digit_n} رقم).")
    upper_n = len(re.findall(r"[A-Z]", p))
    if upper_n < 2:
        errors.append(
            f"حداقل دو حرف بزرگ انگلیسی لازم است (الان {upper_n} حرف)."
        )
    lower_n = len(re.findall(r"[a-z]", p))
    if lower_n < 2:
        errors.append(
            f"حداقل دو حرف کوچک انگلیسی لازم است (الان {lower_n} حرف)."
        )
    if not _PG_SPECIAL_RE.search(p):
        errors.append(
            "حداقل یک کاراکتر خاص از مجموعه "
            f"{_PG_SPECIAL_CHARS} لازم است."
        )
    if '"' in p:
        errors.append('کاراکتر " در رمز مجاز نیست.')
    if username and username.strip() and username.strip().lower() in p.lower():
        errors.append("رمز نباید خودِ نام کاربری را داخل خود داشته باشد.")
    if is_placeholder_password(p):
        errors.append("این رمز نمونه/ضعیف است؛ رمز قوی‌تری انتخاب کنید.")

    if errors:
        return False, _format_issues(
            "رمز عبور با محدودیت‌های پاسارگارد هماهنگ نیست:",
            errors,
        )
    return True, ""


def validate_credentials(
    username: str,
    password: str,
    *,
    lowercase_username: bool = False,
) -> tuple[str, str | None]:
    """Validate username then password. Return ``(cleaned_username, error_or_None)``."""
    cleaned, uerr = validate_pg_username(username, lowercase=lowercase_username)
    if uerr:
        return cleaned, uerr
    ok, perr = validate_password_strength(password, username=cleaned)
    if not ok:
        return cleaned, perr
    return cleaned, None


def password_policy_hint_fa() -> str:
    """Short Persian hint for forms (matches PasarGuard rules)."""
    return (
        "حداقل ۱۲ کاراکتر، شامل حداقل دو رقم، دو حرف بزرگ، دو حرف کوچک "
        "و یک کاراکتر خاص (طبق قوانین پاسارگارد)"
    )


def username_policy_hint_fa() -> str:
    """Short Persian hint for username fields (matches PasarGuard rules)."""
    return (
        "۳ تا ۱۲۸ کاراکتر؛ فقط حروف انگلیسی، عدد و - _ @ . "
        "— بدون فاصله و بدون دو نماد پشت‌سرهم (قوانین پاسارگارد)"
    )


def humanize_pg_validation_error(text: str) -> str:
    """Translate known PasarGuard English validation fragments to Persian causes."""
    raw = (text or "").strip()
    if not raw:
        return raw
    out = raw
    changed = False
    for pat, fa in _PG_API_MSG_MAP:
        if pat.search(out):
            out = pat.sub(fa, out)
            changed = True
    if not changed:
        return raw
    # Drop noisy pydantic prefixes when present
    out = re.sub(r"(?i)\bvalue error,?\s*", "", out)
    out = re.sub(r"\s+", " ", out).strip(" ;.")
    return f"پاسارگارد درخواست را رد کرد. علت: {out}"


# Transport / generic HTTP noise → short Persian (bot + panel).
_PG_TRANSPORT_MSG_MAP: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"timed?\s*out|timeout|deadline exceeded", re.I),
        "اتصال به پاسارگارد طول کشید. کمی بعد دوباره تلاش کنید.",
    ),
    (
        re.compile(
            r"connection\s*(refused|reset|aborted)|network\s*unreachable|"
            r"name\s*or\s*service\s*not\s*known|temporary\s*failure|"
            r"failed\s*to\s*establish|connect\s*error",
            re.I,
        ),
        "ارتباط با پاسارگارد برقرار نشد. آدرس/شبکه را بررسی کنید.",
    ),
    (
        re.compile(r"\b401\b|unauthorized", re.I),
        "ورود به پاسارگارد ناموفق بود. نام کاربری یا رمز را بررسی کنید.",
    ),
    (
        re.compile(r"\b404\b|not\s*found", re.I),
        "مورد درخواستی در پاسارگارد پیدا نشد.",
    ),
    (
        re.compile(r"\b429\b|too\s*many\s*requests|rate\s*limit", re.I),
        "درخواست‌ها زیاد شده است. کمی صبر کنید و دوباره تلاش کنید.",
    ),
    (
        re.compile(r"\b50[0-4]\b|bad\s*gateway|service\s*unavailable|internal\s*server", re.I),
        "پاسارگارد موقتاً در دسترس نیست. کمی بعد دوباره تلاش کنید.",
    ),
    (
        re.compile(r"failed\s*\(\s*409\s*\)", re.I),
        "این نام کاربری قبلاً ثبت شده است. یک نام دیگر بفرستید.",
    ),
    (
        re.compile(r"failed\s*\(\s*403\s*\)", re.I),
        "اجازه این عمل را ندارید.",
    ),
    (
        re.compile(r"failed\s*\(\s*405\s*\)|method\s*not\s*allowed", re.I),
        "این عملیات در پاسارگارد پشتیبانی نشد (متد نامعتبر). آدرس PG_BASE_URL و نسخه پنل را بررسی کنید.",
    ),
    (
        re.compile(r"failed\s*\(\s*422\s*\)", re.I),
        "پاسارگارد دادهٔ ارسالی را نامعتبر دانست. گروه پلن، نام کاربری و محدودیت حجم/زمان را بررسی کنید.",
    ),
]


def friendly_pg_error(text: str, *, status_code: int | None = None) -> str:
    """Map PG API / HTTP errors to a short Persian operator message.

    Business conflicts (duplicate username, 409, 403) return a standalone line.
    Credential validation fragments keep the existing humanize wrapper.
    Unknown text is returned unchanged.
    """
    raw = (text or "").strip()
    if status_code == 409:
        return "این نام کاربری قبلاً ثبت شده است. یک نام دیگر بفرستید."
    if status_code == 403:
        return "اجازه این عمل را ندارید."
    if status_code == 401:
        return "ورود به پاسارگارد ناموفق بود. نام کاربری یا رمز را بررسی کنید."
    if status_code == 404:
        return "مورد درخواستی در پاسارگارد پیدا نشد."
    if status_code in {500, 502, 503, 504}:
        return "پاسارگارد موقتاً در دسترس نیست. کمی بعد دوباره تلاش کنید."
    if status_code == 429:
        return "درخواست‌ها زیاد شده است. کمی صبر کنید و دوباره تلاش کنید."
    if status_code == 405:
        return (
            "این عملیات در پاسارگارد پشتیبانی نشد (متد نامعتبر). "
            "آدرس PG_BASE_URL و نسخه پنل را بررسی کنید."
        )
    if not raw:
        if status_code == 422:
            return (
                "پاسارگارد دادهٔ ارسالی را نامعتبر دانست. "
                "گروه پلن، نام کاربری و محدودیت حجم/زمان را بررسی کنید."
            )
        return raw
    # Already Persian / operator-facing — keep as-is.
    if re.search(r"[\u0600-\u06FF]", raw) and not re.search(
        r"\b(failed|error|exception|traceback)\b", raw, re.I
    ):
        return raw
    for pat, fa in _PG_BUSINESS_MSG_MAP:
        if pat.search(raw):
            return fa
    for pat, fa in _PG_TRANSPORT_MSG_MAP:
        if pat.search(raw):
            return fa
    humanized = humanize_pg_validation_error(raw)
    if humanized != raw:
        return humanized
    if status_code == 422:
        return f"پاسارگارد داده را نامعتبر دانست: {raw[:300]}"
    return raw


def generate_compliant_password(length: int = 14) -> str:
    """Generate a password that always satisfies ``validate_password_strength``."""
    import secrets
    import string

    length = max(14, int(length))
    specials = _PG_SPECIAL_CHARS
    alphabet = string.ascii_letters + string.digits + specials
    for _ in range(32):
        required = (
            [secrets.choice(string.ascii_lowercase) for _ in range(2)]
            + [secrets.choice(string.ascii_uppercase) for _ in range(2)]
            + [secrets.choice(specials)]
            + [secrets.choice(string.digits) for _ in range(2)]
        )
        required += [secrets.choice(alphabet) for _ in range(length - len(required))]
        secrets.SystemRandom().shuffle(required)
        pwd = "".join(required)
        ok, _ = validate_password_strength(pwd)
        if ok:
            return pwd
    return "AaBb12!@#$xY" + secrets.token_hex(2)
