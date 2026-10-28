"""A strict JSON boundary for versioned backend routes only."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

import httpx
from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError

from app.api.integration_auth import ERROR_STATUSES, IntegrationAuthenticationError, _http_error
from app.services.integration_policy import IntegrationError
from app.services.integration_pagination import page_limit
from app.services.integration_security import AuthenticationAttempt
from app.services.pasarguard import PasarGuardError

logger = logging.getLogger(__name__)
MAX_BODY_BYTES = 16 * 1024
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "Pragma": "no-cache", "Referrer-Policy": "no-referrer"}
ERROR_STATUSES.update({
    "customer_blocked": 403, "website_entry_not_enabled": 403, "customer_reference_conflict": 409,
    "invalid_cursor": 400, "idempotency_key_required": 400, "idempotency_conflict": 409,
    "link_conflict": 409, "challenge_expired": 410, "link_not_confirmed": 422,
    "external_reference_conflict": 409, "service_busy": 409, "terms_changed": 409, "access_not_ready": 409,
    "operation_archived": 410, "quota_exceeded": 422, "billing_blocked": 422, "temporarily_unavailable": 503,
    "queue_limit_reached": 429, "payload_too_large": 413, "unsupported_media_type": 415,
    "addon_ineligible": 422, "unsupported_terms": 422, "service_identity_mismatch": 409, "panel_unavailable": 503,
})


def idempotency_key(request: Request) -> str:
    values = request.headers.getlist("idempotency-key")
    if len(values) != 1:
        raise IntegrationError("idempotency_key_required")
    return values[0]


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


async def validate_transport_input(request: Request, query_names: frozenset[str]) -> None:
    if len(request.scope.get("query_string", b"")) > 2048:
        raise IntegrationError("invalid_request")
    if set(request.query_params) - query_names or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise IntegrationError("invalid_request")
    if "limit" in request.query_params:
        page_limit(request.query_params["limit"])
    if request.headers.get("content-encoding"):
        raise IntegrationError("unsupported_media_type")
    lengths = request.headers.getlist("content-length")
    if len(lengths) > 1:
        raise IntegrationError("invalid_request")
    if lengths:
        value = lengths[0]
        if not value.isascii() or not value.isdecimal() or len(value) > 8:
            raise IntegrationError("invalid_request")
        if int(value) > MAX_BODY_BYTES:
            raise IntegrationError("payload_too_large")
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise IntegrationError("payload_too_large")
        chunks.append(chunk)
    body = b"".join(chunks)
    request._body = body
    if body:
        if request.method in {"GET", "HEAD"}:
            raise IntegrationError("invalid_request")
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
            raise IntegrationError("unsupported_media_type")
        try:
            json.loads(body.decode("utf-8"), object_pairs_hook=_json_object, parse_constant=_reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise IntegrationError("invalid_request") from None


class IntegrationRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()
        query_names = frozenset(field.name for field in self.dependant.query_params)

        async def handle(request: Request) -> Response:
            attempt = request.scope.setdefault("pgclock.integration_attempt", AuthenticationAttempt())
            try:
                await validate_transport_input(request, query_names)
                response = await original(request)
            except IntegrationAuthenticationError as exc:
                return JSONResponse(exc.detail, status_code=exc.status_code, headers=exc.headers)
            except RequestValidationError:
                response = error_response(IntegrationError("invalid_request"), attempt)
            except IntegrationError as exc:
                response = error_response(exc, attempt)
            except (httpx.HTTPError, PasarGuardError, TimeoutError):
                response = error_response(IntegrationError("panel_unavailable"), attempt)
            except SQLAlchemyError as exc:
                logger.warning("Integration request storage unavailable type=%s", type(exc).__name__)
                response = error_response(IntegrationError("internal_error"), attempt)
            except Exception as exc:
                logger.warning("Integration boundary failure type=%s", type(exc).__name__)
                response = error_response(IntegrationError("internal_error"), attempt)
            response.headers.update(PRIVATE_HEADERS)
            response.headers["X-Request-ID"] = attempt.request_id
            return response

        return handle


def error_response(error: IntegrationError, attempt: AuthenticationAttempt) -> JSONResponse:
    converted = _http_error(error, attempt)
    headers = dict(converted.headers or {})
    if converted.status_code == 429:
        headers.setdefault("Retry-After", "60")
    return JSONResponse(converted.detail, status_code=converted.status_code, headers=headers)
