"""Cookie-free backend Bearer dependency, deliberately unmounted in production."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
import logging

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integration_transport import TRANSPORT_KEY
from app.config import get_settings
from app.services.integration_client_ip import proxy_networks, resolve_client
from app.services.integration_rate_limits import IntegrationRateLimited, RequestBudget, WRITE_SCOPES, charge_integration
from app.services.integration_security import AuthenticationAttempt
from app.services.integration_security_audit import verify_audit_identity
from app.services.db_safe import rollback_quiet
from app.services.integration_authority import (
    IntegrationAuthority, IntegrationPGEvidence, require_local_scopes, resolve_integration_authority,
)
from app.services.integration_policy import ERROR_MESSAGES, IntegrationError, validate_scopes
from app.services.pasarguard import PasarGuardError

ERROR_STATUSES = {
    "invalid_request": 400, "unsupported_authentication": 400, "invalid_credential": 401,
    "api_disabled": 503, "integration_disabled": 403, "principal_inactive": 403,
    "permission_denied": 403, "not_found": 404, "plan_ineligible": 422,
    "authorization_unavailable": 503, "internal_error": 500, "rate_limited": 429, "security_unavailable": 503,
}
logger = logging.getLogger(__name__)
_ATTEMPT_KEY = "pgclock.integration_attempt"
_BEARER = re.compile(r"Bearer ([A-Za-z0-9_.-]+)", re.IGNORECASE | re.ASCII)
_QUERY_CREDENTIALS = frozenset({"authorization", "access_token", "token", "api_key", "apikey", "credential", "bearer", "key"})
_OTHER_AUTH_HEADERS = frozenset({b"x-api-key", b"proxy-authorization", b"x-auth-token"})
PGVerifier = Callable[[IntegrationAuthority], Awaitable[IntegrationPGEvidence]]


class IntegrationAuthenticationError(HTTPException):
    """Dedicated exception so the integration envelope cannot alter panel errors."""


async def integration_auth_error_response(request: Request, error: IntegrationAuthenticationError) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content=error.detail, headers=error.headers)


def register_integration_auth_errors(app: FastAPI) -> None:
    app.add_exception_handler(IntegrationAuthenticationError, integration_auth_error_response)


def _request_bearer(request: Request, scheme: str) -> str:
    headers = request.scope.get("headers", [])
    if scheme != "https":
        raise IntegrationError("unsupported_authentication")
    if any(name.lower() == b"cookie" and value for name, value in headers):
        raise IntegrationError("unsupported_authentication")
    if any(name.lower() in _OTHER_AUTH_HEADERS for name, _ in headers):
        raise IntegrationError("unsupported_authentication")
    if any(key.lower() in _QUERY_CREDENTIALS for key in request.query_params):
        raise IntegrationError("unsupported_authentication")
    if request.headers.get("origin"):
        raise IntegrationError("unsupported_authentication")
    authorization = [value for name, value in headers if name.lower() == b"authorization"]
    if len(authorization) > 1:
        raise IntegrationError("unsupported_authentication")
    if not authorization:
        raise IntegrationError("invalid_credential")
    if b"," in authorization[0]:
        raise IntegrationError("unsupported_authentication")
    if len(authorization[0]) > 256:
        raise IntegrationError("invalid_credential")
    match = _BEARER.fullmatch(authorization[0].decode("latin-1"))
    if match is None:
        raise IntegrationError("invalid_credential")
    return match.group(1)


def _http_error(error: IntegrationError, attempt: AuthenticationAttempt) -> IntegrationAuthenticationError:
    status = ERROR_STATUSES[error.code]
    headers = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer",
               "X-Request-ID": attempt.request_id}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if isinstance(error, IntegrationRateLimited):
        headers["Retry-After"] = str(error.retry_after)
    return IntegrationAuthenticationError(status_code=status, headers=headers, detail={"error": {
        "code": error.code, "message": ERROR_MESSAGES[error.code], "request_id": attempt.request_id,
        "retryable": status in {429, 503}, "details": (
            {"operation_id": error.operation_id} if error.code == "operation_archived"
            and re.fullmatch(r"op_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", getattr(error, "operation_id", "")) else None
        ),
    }})


async def _authorize(
    session: AsyncSession, bearer: str, required_scopes: frozenset[str], pg_verifier: PGVerifier | None,
) -> IntegrationAuthority:
    if pg_verifier is None:
        return await resolve_integration_authority(session, bearer, required_scopes=required_scopes)
    authority = await resolve_integration_authority(session, bearer)
    require_local_scopes(authority, required_scopes)
    await session.rollback()
    evidence = await pg_verifier(authority)
    return await resolve_integration_authority(
        session, bearer, required_scopes=required_scopes, pg_evidence=evidence,
    )


async def _complete_attempt(
    session: AsyncSession, attempt: AuthenticationAttempt, error: IntegrationError | None,
) -> None:
    try:
        await session.rollback()
        await attempt.finish(session, error.code if error else "succeeded")
    except SQLAlchemyError as exc:
        await rollback_quiet(session)
        logger.warning("Integration authentication audit unavailable type=%s", type(exc).__name__)
        raise _http_error(IntegrationError("security_unavailable"), attempt) from None
    if error is not None:
        raise _http_error(error, attempt) from None


def integration_bearer_dependency(
    get_db: Callable, *, required_scopes: frozenset[str] = frozenset(), pg_verifier: PGVerifier | None = None,
    budget: RequestBudget = RequestBudget.READ, read_only: bool = False,
) -> Callable:
    """Trusted route configuration selects budgets; operational routers stay unmounted.

    Run behind IntegrationPeerCapture at the server boundary. No caller header or
    generic panel trust flag can establish transport provenance or budget class.
    """
    validate_scopes(required_scopes)
    if not isinstance(budget, RequestBudget):
        raise ValueError("A trusted RequestBudget is required")
    if required_scopes & WRITE_SCOPES and budget != RequestBudget.WRITE and not read_only:
        raise ValueError("Mutation scopes require the write budget")
    if "services:access" in required_scopes and budget != RequestBudget.PRIVATE:
        raise ValueError("Private access requires both read and private budgets")

    async def authenticate(request: Request, response: Response, session: AsyncSession = Depends(get_db)) -> IntegrationAuthority:
        attempt = request.scope.setdefault(_ATTEMPT_KEY, AuthenticationAttempt())
        response.headers["X-Request-ID"] = attempt.request_id
        error = None
        try:
            if read_only and request.method != "GET":
                raise IntegrationError("invalid_request")
            settings = get_settings()
            try:
                networks = proxy_networks(settings.integration_trusted_proxies)
            except ValueError:
                raise IntegrationError("security_unavailable") from None
            client = resolve_client(request.scope.get(TRANSPORT_KEY), request.scope.get("headers", []), networks)
            await attempt.admit(session, client, settings.web_secret)
            bearer = _request_bearer(request, client.scheme)
            attempt.identity = await verify_audit_identity(session, bearer)
            await session.rollback()
            await charge_integration(session, attempt.identity.integration_id, budget, charged=attempt.charged)
            authority = await _authorize(session, bearer, required_scopes, pg_verifier)
        except IntegrationError as exc:
            error = exc
        except (httpx.HTTPError, PasarGuardError):
            error = IntegrationError("authorization_unavailable")
        except SQLAlchemyError as exc:
            await rollback_quiet(session)
            logger.warning("Integration authentication storage unavailable type=%s", type(exc).__name__)
            error = IntegrationError("security_unavailable")
        await _complete_attempt(session, attempt, error)
        request.scope["pgclock.integration_bearer"] = bearer
        return authority

    return authenticate
