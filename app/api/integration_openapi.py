"""Describe the shared strict API envelope and private response headers."""

from __future__ import annotations

from app.services.integration_policy import ERROR_MESSAGES

ERROR_CODES = {400: "Invalid request", 401: "Invalid credential", 403: "Access denied", 404: "Resource not found",
    409: "Conflict or resource not ready", 410: "Expired or archived", 413: "Request too large", 415: "Unsupported media type",
    422: "Ineligible technical terms", 429: "Shared request budget exhausted", 500: "Internal error", 503: "Temporarily unavailable"}


def enrich_integration_openapi(document: dict) -> None:
    schemas = document.setdefault("components", {}).setdefault("schemas", {})
    schemas["IntegrationError"] = {"type": "object", "additionalProperties": False, "required": ["error"], "properties": {
        "error": {"type": "object", "additionalProperties": False,
            "required": ["code", "message", "request_id", "retryable", "details"], "properties": {
                "code": {"type": "string", "enum": sorted(ERROR_MESSAGES)}, "message": {"type": "string"},
                "request_id": {"type": "string"}, "retryable": {"type": "boolean"},
                "details": {"anyOf": [{"type": "null"}, {"type": "object", "additionalProperties": False,
                    "properties": {"operation_id": {"type": "string"}, "field": {"type": "string"}}}]}}}}}
    for path, methods in document.get("paths", {}).items():
        if not path.startswith("/api/v1/"):
            continue
        for method, operation in methods.items():
            if method not in {"get", "post", "delete", "put", "patch"}:
                continue
            responses = operation.setdefault("responses", {})
            for status, description in ERROR_CODES.items():
                responses[str(status)] = {"description": description, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/IntegrationError"}}}}
            for response in responses.values():
                response.setdefault("headers", {}).update({"Cache-Control": {"schema": {"type": "string", "const": "private, no-store"}},
                    "X-Request-ID": {"schema": {"type": "string"}}})
            if "202" in responses:
                responses["202"]["headers"].update({"Location": {"schema": {"type": "string"}}, "Retry-After": {"schema": {"type": "integer"}}})
            if path.endswith("/qr"):
                responses["200"]["content"] = {"image/png": {"schema": {"type": "string", "format": "binary"}}}
                responses["200"]["headers"]["X-Access-Version"] = {"schema": {"type": "integer", "minimum": 1}}
            for parameter in operation.get("parameters", []):
                if parameter.get("name") == "limit":
                    parameter["schema"].update(minimum=1, maximum=100)
                elif parameter.get("name") == "cursor":
                    parameter["schema"]["maxLength"] = 1024
