"""Gemini AI adapter — AI service integration (not a payment gateway).

This adapter provides access to Google's Gemini AI models for features like:
- Smart customer support responses
- Automated ticket categorization
- Content generation for marketing
- Payment fraud detection assistance

NOTE: This is NOT a payment service provider. It's an AI service integration
that can be used by other parts of the system for intelligent automation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from app.services.payment_providers.base import CheckoutRequest, CheckoutResult, VerifyResult

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_MODELS_ENDPOINT = f"{_API_BASE}/models"
_DEFAULT_MODEL = "gemini-1.5-flash"
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

# Shared HTTP client
_CLIENT: httpx.AsyncClient | None = None


async def _get_client() -> httpx.AsyncClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
    return _CLIENT


# ── Adapter ────────────────────────────────────────────────────────────────

class GeminiAdapter:
    """Google Gemini AI service adapter.

    This is NOT a payment gateway. It provides AI capabilities for:
    - Text generation
    - Classification
    - Summarization
    - Structured data extraction
    """

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = _DEFAULT_MODEL,
    ):
        self.api_key = (api_key or "").strip()
        self.model = model.strip()

    def _require_config(self) -> None:
        if not self.api_key:
            raise ValueError("Gemini API key is not configured")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }

    # ── AI Methods (not payment protocol) ──────────────────────────────────

    async def generate_text(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
    ) -> str:
        """Generate text using Gemini."""
        self._require_config()

        url = f"{_API_BASE}/models/{self.model}:generateContent"

        parts = [{"text": prompt}]
        if system_instruction:
            parts.insert(0, {"text": f"System: {system_instruction}\n\n"})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }

        client = await _get_client()
        try:
            resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            logger.warning("gemini generate network error: %s", type(exc).__name__)
            raise ValueError(f"AI service unavailable: {exc}") from exc

        try:
            data = resp.json() if resp.content else {}
        except ValueError:
            data = {}

        if resp.status_code >= 400:
            detail = data.get("error", {}).get("message", "Unknown error")
            logger.warning("gemini generate failed status=%s: %s", resp.status_code, detail)
            raise ValueError(f"AI service error: {detail}")

        candidates = data.get("candidates", [])
        if not candidates:
            return ""

        content = candidates[0].get("content", {})
        parts = content.get("parts", [])
        return "".join(p.get("text", "") for p in parts)

    async def classify_text(
        self,
        text: str,
        categories: list[str],
        *,
        system_instruction: str | None = None,
    ) -> str:
        """Classify text into one of the given categories."""
        categories_str = ", ".join(categories)
        prompt = f"""Classify the following text into exactly ONE of these categories: {categories_str}

Text: {text}

Return only the category name, nothing else."""

        if system_instruction:
            prompt = f"{system_instruction}\n\n{prompt}"

        result = await self.generate_text(prompt, temperature=0.1, max_tokens=50)
        result = result.strip()

        # Validate result is one of the categories
        if result in categories:
            return result
        # Fallback: find closest match
        for cat in categories:
            if cat.lower() in result.lower():
                return cat
        return categories[0] if categories else "unknown"

    async def extract_structured(
        self,
        text: str,
        schema: dict[str, Any],
        *,
        system_instruction: str | None = None,
    ) -> dict[str, Any]:
        """Extract structured data from text according to a JSON schema."""
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        prompt = f"""Extract structured data from the following text according to this JSON schema:
{schema_str}

Text: {text}

Return ONLY valid JSON matching the schema, nothing else."""

        if system_instruction:
            prompt = f"{system_instruction}\n\n{prompt}"

        result = await self.generate_text(prompt, temperature=0.1, max_tokens=1024)
        try:
            return json.loads(result.strip())
        except json.JSONDecodeError:
            logger.warning("gemini extract_structured: failed to parse JSON: %s", result[:200])
            return {}

    # ── Payment Protocol (NOT IMPLEMENTED - this is not a payment gateway) ──

    async def create_checkout(self, req: CheckoutRequest) -> CheckoutResult:
        """Gemini is not a payment gateway - this raises an error."""
        raise NotImplementedError(
            "GeminiAdapter is an AI service, not a payment gateway. "
            "Use it for text generation, classification, or data extraction."
        )

    async def verify(
        self,
        *,
        external_ref: str,
        amount: int,
        callback_params: dict[str, Any],
        expected_invoice_id: int | None = None,
    ) -> VerifyResult:
        """Gemini is not a payment gateway - this raises an error."""
        raise NotImplementedError(
            "GeminiAdapter is an AI service, not a payment gateway."
        )