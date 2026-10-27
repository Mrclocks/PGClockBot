"""Several API payment gateways (Zarinpal / SunPay) per shop, stored as a JSON list in settings.

Each gateway keeps its own credential. A shop that never saved the list keeps using the
single gateway configured by the older ``psp_*`` settings, exposed here as id ``default``.
A checkout records the gateway id, so its verify uses the same merchant even after the
shop adds, reorders or disables gateways.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

KEY_PSP_GATEWAYS = "psp_gateways"
LEGACY_GATEWAY_ID = "default"
MAX_PSP_GATEWAYS = 8
PROVIDERS = ("zarinpal", "sunpay", "mock")
REQUEST_METHODS = ("GET", "POST")
DEFAULT_GATEWAY_NAME = "درگاه آنلاین"

_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
_TRUE = {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class PspGateway:
    id: str
    name: str
    provider: str
    merchant_id: str
    pay_url: str
    request_method: str
    sandbox: bool
    enabled: bool

    def adapter_settings(self) -> dict[str, str]:
        """The ``psp_*`` keys the provider adapters read, for this gateway only."""
        return {
            "psp_merchant_id": self.merchant_id,
            "psp_pay_url": self.pay_url,
            "psp_sunpay_request_method": self.request_method,
            "psp_sandbox": "1" if self.sandbox else "0",
        }


def is_valid_gateway_id(raw: object) -> bool:
    return isinstance(raw, str) and bool(_ID_RE.match(raw))


def _on(raw: object, default: bool) -> bool:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in _TRUE


def _provider(raw: object) -> str:
    name = str(raw or "").strip().lower()
    return name if name in PROVIDERS else "zarinpal"


def _request_method(raw: object) -> str:
    method = str(raw or "").strip().upper()
    return method if method in REQUEST_METHODS else "GET"


def _from_entry(entry: dict[str, Any]) -> PspGateway:
    gateway_id = str(entry.get("id") or "")
    if not is_valid_gateway_id(gateway_id):
        gateway_id = uuid.uuid4().hex[:12]
    return PspGateway(
        id=gateway_id,
        name=str(entry.get("name") or "").strip()[:64] or DEFAULT_GATEWAY_NAME,
        provider=_provider(entry.get("provider")),
        merchant_id=str(entry.get("merchant_id") or "").strip()[:256],
        pay_url=str(entry.get("pay_url") or "").strip()[:512],
        request_method=_request_method(entry.get("request_method")),
        sandbox=_on(entry.get("sandbox"), False),
        enabled=_on(entry.get("enabled"), True),
    )


def parse_psp_gateways(raw: str | None) -> list[PspGateway]:
    """Saved gateways in order, unique ids, at most ``MAX_PSP_GATEWAYS``."""
    try:
        data = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    out: list[PspGateway] = []
    seen: set[str] = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        gateway = _from_entry(entry)
        if gateway.id in seen:
            continue
        seen.add(gateway.id)
        out.append(gateway)
        if len(out) >= MAX_PSP_GATEWAYS:
            break
    return out


def _legacy_gateway(ui: dict) -> PspGateway:
    return PspGateway(
        id=LEGACY_GATEWAY_ID,
        name=DEFAULT_GATEWAY_NAME,
        provider=_provider(ui.get("psp_provider")),
        merchant_id=str(ui.get("psp_merchant_id") or "").strip(),
        pay_url=str(ui.get("psp_pay_url") or "").strip(),
        request_method=_request_method(ui.get("psp_sunpay_request_method")),
        sandbox=_on(ui.get("psp_sandbox"), True),
        enabled=True,
    )


def all_psp_gateways(ui: dict) -> list[PspGateway]:
    """The shop's gateways; the older single-gateway settings when no list was saved."""
    saved = parse_psp_gateways(ui.get(KEY_PSP_GATEWAYS))
    return saved or [_legacy_gateway(ui)]


def enabled_psp_gateways(ui: dict) -> list[PspGateway]:
    return [g for g in all_psp_gateways(ui) if g.enabled]


def find_psp_gateway(ui: dict, gateway_id: str | None) -> PspGateway | None:
    """A gateway by id, enabled or not (an open checkout is verified after a disable)."""
    if not gateway_id:
        # Created before gateway ids existed: the older settings still hold that credential.
        return _legacy_gateway(ui)
    return next((g for g in all_psp_gateways(ui) if g.id == gateway_id), None)


def choose_psp_gateway(ui: dict, gateway_id: str | None) -> PspGateway:
    """The enabled gateway a new checkout uses; ``ValueError`` with a customer-safe text."""
    enabled = enabled_psp_gateways(ui)
    if not enabled:
        raise ValueError("درگاه آنلاینی فعال نیست")
    if gateway_id is None:
        return enabled[0]
    chosen = next((g for g in enabled if g.id == gateway_id), None)
    if chosen is None:
        raise ValueError("این درگاه دیگر فعال نیست")
    return chosen


def gateway_choices(ui: dict) -> list[PspGateway]:
    """Gateways the customer picks from; empty when there is only one to use."""
    enabled = enabled_psp_gateways(ui)
    return enabled if len(enabled) > 1 else []


def _form_entry(gateway: PspGateway) -> dict[str, Any]:
    return {
        "id": gateway.id,
        "name": gateway.name,
        "provider": gateway.provider,
        "has_secret": bool(gateway.merchant_id),
        "pay_url": gateway.pay_url,
        "request_method": gateway.request_method,
        "sandbox": gateway.sandbox,
        "enabled": gateway.enabled,
    }


def psp_gateways_form_json(ui: dict) -> str:
    """The list for the panel editor, without credentials (``has_secret`` says one is saved)."""
    return json.dumps([_form_entry(g) for g in all_psp_gateways(ui)], ensure_ascii=False)


def _stored_entry(gateway: PspGateway) -> dict[str, Any]:
    entry = _form_entry(gateway)
    del entry["has_secret"]
    entry["merchant_id"] = gateway.merchant_id
    return entry


def merge_submitted_psp_gateways(submitted: str, ui: dict) -> str:
    """The list to store from the editor's JSON.

    A blank credential keeps the one already saved for that gateway id, the same rule as
    every other secret field: the panel never sends saved credentials back to the browser.
    """
    previous = {g.id: g.merchant_id for g in all_psp_gateways(ui)}
    rows: list[dict[str, Any]] = []
    for gateway in parse_psp_gateways(submitted):
        entry = _stored_entry(gateway)
        if not gateway.merchant_id:
            entry["merchant_id"] = previous.get(gateway.id, "")
        rows.append(entry)
    return json.dumps(rows, ensure_ascii=False)
