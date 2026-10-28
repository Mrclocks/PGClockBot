"""Fence all application panel writes against durable API service ownership."""

from __future__ import annotations

import json
import re
import secrets
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.db.integration_models import Integration
from app.db.integration_operation_models import IntegrationNativeMutation, IntegrationOperation, ServiceMutationGuard
from app.db.integration_read_models import IntegrationServiceSnapshot
from app.db.models import OrgPrincipal, UserService
from app.services.integration_operation_claims import OperationLease
from app.services.integration_operation_targets import PanelTarget, lease_condition
from app.services.integration_policy import IntegrationError
from app.services.pg_user_scope import pg_user_owner_username

USER_PATH = re.compile(r"/api/user/(?:from_template/)?by-(id|username)/([^/]+)(?:/.*)?$", re.ASCII)


@dataclass(slots=True)
class DispatchPermit:
    lease: OperationLease
    target: PanelTarget
    client_identity: str
    used: bool = False


_PERMIT: ContextVar[DispatchPermit | None] = ContextVar("integration_dispatch", default=None)


@contextmanager
def dispatch_permit(permit: DispatchPermit) -> Iterator[None]:
    token = _PERMIT.set(permit)
    try:
        yield
    finally:
        _PERMIT.reset(token)


def _sessions(client: Any) -> Any:
    if getattr(client, "_integration_sessions", None) is not None:
        return client._integration_sessions
    # Production session configuration imports models; defer to avoid a transport cycle.
    from app.db.session import SessionLocal
    return SessionLocal


async def _permitted(client: Any, method: str, path: str, kwargs: dict, permit: DispatchPermit) -> Any:
    if (permit.used or permit.client_identity != client._read_cache_ident() or method != permit.target.method
            or path != permit.target.path or kwargs.get("json") != permit.target.payload):
        raise IntegrationError("service_busy")
    async with _sessions(client)() as session:
        target = await session.scalar(select(IntegrationOperation.target_json).where(
            *lease_condition(permit.lease), IntegrationOperation.dispatch_sent_at.is_not(None),
        ))
        if target != permit.target.serialized():
            raise IntegrationError("service_busy")
    permit.used = True
    return await client._request_unfenced(method, path, retry_auth=False, **kwargs)


async def _tracked_service(client: Any, path: str) -> int | None:
    match = USER_PATH.fullmatch(path)
    if match is None:
        return None
    kind, value = match.groups()
    async with _sessions(client)() as session:
        condition = UserService.pg_user_id == int(value) if kind == "id" and value.isascii() and value.isdecimal() else UserService.pg_username == value
        service_id = await session.scalar(select(UserService.id).where(UserService.integration_id.is_not(None), condition))
        pending = await session.scalar(select(UserService.id).where(UserService.integration_id.is_not(None),
            UserService.pg_user_id.is_(None)).limit(1))
    if service_id is not None or pending is None or kind != "id":
        return service_id
    # A create may have reached PG before its local ID was learned. Resolve only
    # this visible ID using the caller's own credentials, outside the transaction.
    info = await client._request_unfenced("GET", f"/api/user/by-id/{value}", use_cache=False)
    if not isinstance(info, dict) or not isinstance(info.get("username"), str):
        raise IntegrationError("service_busy")
    async with _sessions(client)() as session:
        return await session.scalar(select(UserService.id).where(UserService.integration_id.is_not(None),
            UserService.pg_username == info["username"]))


def _native_status_value(method: str, path: str, payload: object) -> str | None:
    if method != "PUT" or not isinstance(payload, dict) or "/from_template/" in path:
        return None
    if path.count("/") == 4 and set(payload) == {"status"} and payload["status"] in {"active", "disabled"}:
        return payload["status"]
    if path.count("/") == 5 and path.endswith("/disabled") and set(payload) == {"disabled"} and type(payload["disabled"]) is bool:
        return "disabled" if payload["disabled"] else "active"
    return None


async def _native_status(client: Any, service_id: int, method: str, path: str, kwargs: dict) -> Any:
    payload = kwargs.get("json")
    status = _native_status_value(method, path, payload)
    if status is None:
        raise IntegrationError("service_busy")
    sessions = _sessions(client)
    async with sessions() as session:
        # Locking the owning integration serializes native claims with API accepts.
        await session.execute(update(Integration).where(Integration.id.in_(select(UserService.integration_id).where(
            UserService.id == service_id))).values(revision=Integration.revision+1))
        service = await session.get(UserService, service_id)
        if await session.get(ServiceMutationGuard, service_id) is not None:
            raise IntegrationError("service_busy")
        mutation = IntegrationNativeMutation(integration_id=service.integration_id, service_id=service_id,
            token=secrets.token_hex(32), target_json=json.dumps({"method": method, "path": path, "payload": payload}, sort_keys=True))
        session.add(mutation)
        await session.flush()
        session.add(ServiceMutationGuard(service_id=service_id, native_mutation_id=mutation.id))
        mutation_id = mutation.id
        username, panel_id = service.pg_username, service.pg_user_id
        owner = await session.scalar(select(OrgPrincipal.pg_username).where(OrgPrincipal.id == service.owner_principal_id))
        if not owner or panel_id is None:
            raise IntegrationError("service_busy")
        await session.commit()
    try:
        result = await client._request_unfenced(method, path, retry_auth=False, **kwargs)
        info = await client._request_unfenced("GET", f"/api/user/by-id/{panel_id}", use_cache=False)
        if not isinstance(info, dict) or info.get("id") != panel_id or info.get("username") != username or info.get("status") != status or pg_user_owner_username(info) != owner.lower():
            raise IntegrationError("service_identity_mismatch")
    except BaseException:
        # Even cancellation may happen after PG applied the write. Keep the fence.
        async with sessions() as session:
            await session.execute(update(IntegrationNativeMutation).where(IntegrationNativeMutation.id == mutation_id).values(state="unknown"))
            await session.execute(update(IntegrationServiceSnapshot).where(IntegrationServiceSnapshot.service_id == service_id).values(
                state="review", resource_version=IntegrationServiceSnapshot.resource_version+1))
            await session.commit()
        raise
    async with sessions() as session:
        await session.execute(update(IntegrationNativeMutation).where(IntegrationNativeMutation.id == mutation_id,
            IntegrationNativeMutation.state == "intent").values(state="completed", updated_at=datetime.now(timezone.utc)))
        await session.execute(update(IntegrationServiceSnapshot).where(IntegrationServiceSnapshot.service_id == service_id).values(
            panel_status=status, resource_version=IntegrationServiceSnapshot.resource_version+1))
        await session.execute(delete(ServiceMutationGuard).where(ServiceMutationGuard.native_mutation_id == mutation_id))
        await session.commit()
    return result


async def panel_mutation(client: Any, method: str, path: str, kwargs: dict) -> Any:
    method = method.upper()
    permit = _PERMIT.get()
    if permit is not None:
        return await _permitted(client, method, path, kwargs, permit)
    service_id = await _tracked_service(client, path)
    if service_id is None:
        return await client._request_unfenced(method, path, **kwargs)
    try:
        return await _native_status(client, service_id, method, path, kwargs)
    except IntegrityError:
        raise IntegrationError("service_busy") from None
