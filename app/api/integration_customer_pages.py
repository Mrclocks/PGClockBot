"""Read-only website customer page, separate from Telegram customer administration."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.api.integration_management_pages import _error_response
from app.services.integration_customer_views import website_customer_page
from app.services.integration_management_forms import bounded_integer
from app.services.integration_policy import IntegrationError


def register_integration_customer_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable,
) -> None:
    @app.get("/integrations/{integration_id}/customers")
    async def customers(request: Request, integration_id: str, staff: dict = Depends(require_staff),
                        session: AsyncSession = Depends(get_db)) -> Response:
        try:
            after = bounded_integer(request.query_params.get("after", "0"), minimum=0, maximum=2**31-1)
            rows, next_after = await website_customer_page(session, staff, integration_id, after=after)
        except IntegrationError as exc:
            return _error_response(render, request, staff, exc)
        return render(request, "integration_customers.html", {
            "staff": staff, "integration_id": integration_id, "customers": rows, "next_after": next_after,
        })
