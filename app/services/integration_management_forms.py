"""Bound management inputs and bind short-lived form tickets to exact server scope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import re
from uuid import uuid4

from itsdangerous import BadData, URLSafeTimedSerializer

from app.services.integration_owners import IntegrationOwner
from app.services.integration_policy import IntegrationError, validate_scopes

FORM_LIFETIME_SECONDS = 20 * 60
MAX_FORM_BYTES = 16 * 1024
ID_PATTERN = re.compile(r"intg_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.ASCII)
SELECTOR_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)


class ManagementAction(str, Enum):
    CREATE = "create"
    CONFIGURE = "configure"
    PLANS = "plans"
    ISSUE = "issue"
    ROTATE = "rotate"
    REVOKE = "revoke"


@dataclass(frozen=True, slots=True)
class ManagementTarget:
    action: ManagementAction
    integration_id: str | None = None
    selector: str = ""


@dataclass(frozen=True, slots=True)
class ManagementChange:
    status: str = "disabled"
    scopes: frozenset[str] = frozenset()
    plan_ids: frozenset[int] = frozenset()
    lifetime_days: int = 90
    overlap_minutes: int = 0
    confirmed: bool = False
    customer_entry_policy: str = "denied"


def management_signer(secret: bytes) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="pgclock-integration-management")


def _ticket_scope(owner: IntegrationOwner, target: ManagementTarget) -> list:
    return [owner.principal_id, owner.shop.kind.value, owner.shop.shop_id,
            target.action.value, target.integration_id, target.selector]


def new_management_ticket(signer: URLSafeTimedSerializer, owner: IntegrationOwner, target: ManagementTarget) -> str:
    validate_target(target)
    return signer.dumps([uuid4().hex, *_ticket_scope(owner, target)])


def validate_target(target: ManagementTarget) -> None:
    if not isinstance(target.action, ManagementAction) or not isinstance(target.selector, str):
        raise IntegrationError("invalid_request")
    if target.action == ManagementAction.CREATE:
        valid = target.integration_id is None and not target.selector
    else:
        valid = isinstance(target.integration_id, str) and ID_PATTERN.fullmatch(target.integration_id) is not None
    credential_action = target.action in {ManagementAction.ROTATE, ManagementAction.REVOKE}
    if not valid or (credential_action and not SELECTOR_PATTERN.fullmatch(target.selector)):
        raise IntegrationError("invalid_request")
    if not credential_action and target.selector:
        raise IntegrationError("invalid_request")


def read_management_ticket(
    signer: URLSafeTimedSerializer, token: str, owner: IntegrationOwner, target: ManagementTarget,
) -> str:
    validate_target(target)
    if not isinstance(token, str) or not 1 <= len(token) <= 1024:
        raise IntegrationError("invalid_request")
    try:
        data = signer.loads(token, max_age=FORM_LIFETIME_SECONDS)
    except BadData:
        raise IntegrationError("invalid_request") from None
    if (not isinstance(data, list) or len(data) != 7 or data[1:] != _ticket_scope(owner, target)
            or not isinstance(data[0], str) or not SELECTOR_PATTERN.fullmatch(data[0])):
        raise IntegrationError("invalid_request")
    return data[0]


def bounded_integer(raw: object, *, minimum: int, maximum: int) -> int:
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal() or len(raw) > 10:
        raise IntegrationError("invalid_request")
    result = int(raw)
    if not minimum <= result <= maximum:
        raise IntegrationError("invalid_request")
    return result


def _scopes(raw: object) -> frozenset[str]:
    if not isinstance(raw, str) or len(raw) > 512:
        raise IntegrationError("invalid_request")
    return validate_scopes(frozenset(raw.split()))


def parse_management_form(form: Mapping[str, object], action: ManagementAction) -> ManagementChange:
    if not isinstance(action, ManagementAction):
        raise IntegrationError("invalid_request")
    common = {"csrf_token", "ticket", "confirm"}
    fields = {
        ManagementAction.CREATE: {"scopes"}, ManagementAction.CONFIGURE: {"status", "scopes", "customer_entry_policy"},
        ManagementAction.PLANS: {"plan_ids"}, ManagementAction.ISSUE: {"scopes", "lifetime_days"},
        ManagementAction.ROTATE: {"lifetime_days", "overlap_minutes"}, ManagementAction.REVOKE: set(),
    }
    if set(form) - common - fields[action]:
        raise IntegrationError("invalid_request")
    raw_plans = form.get("plan_ids", "")
    if not isinstance(raw_plans, str) or len(raw_plans) > 1100 or len(raw_plans.split()) > 100:
        raise IntegrationError("invalid_request")
    return ManagementChange(
        status=str(form.get("status", "disabled")), scopes=_scopes(form.get("scopes", "")),
        plan_ids=frozenset(bounded_integer(value, minimum=1, maximum=2**31-1) for value in raw_plans.split()),
        lifetime_days=bounded_integer(form.get("lifetime_days", "90"), minimum=1, maximum=365),
        overlap_minutes=bounded_integer(form.get("overlap_minutes", "0"), minimum=0, maximum=1440),
        confirmed=form.get("confirm") == "yes",
        customer_entry_policy=str(form.get("customer_entry_policy", "denied")),
    )


def validate_change(target: ManagementTarget, change: ManagementChange) -> None:
    validate_scopes(change.scopes)
    if not isinstance(change.customer_entry_policy, str) or change.customer_entry_policy not in {"denied", "website"}:
        raise IntegrationError("invalid_request")
    if not isinstance(change.status, str) or change.status not in {"active", "disabled", "retired"} or type(change.confirmed) is not bool:
        raise IntegrationError("invalid_request")
    for value, lower, upper in ((change.lifetime_days, 1, 365), (change.overlap_minutes, 0, 1440)):
        if type(value) is not int or not lower <= value <= upper:
            raise IntegrationError("invalid_request")
    if (not isinstance(change.plan_ids, frozenset) or len(change.plan_ids) > 100
            or any(type(value) is not int or not 0 < value <= 2**31-1 for value in change.plan_ids)):
        raise IntegrationError("invalid_request")
    if target.action != ManagementAction.CREATE and not change.confirmed:
        raise IntegrationError("confirmation_required")
