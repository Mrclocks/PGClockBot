"""Staff page for gift and discount codes (create, edit, enable/disable via the existing routes)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.gift_codes import (
    describe_code,
    gift_code_plan_options,
    gift_code_scope,
    list_gift_codes,
)

DONE_MESSAGE = "انجام شد"
GIFT_CODES_PATH = "/gift-codes"


def legacy_gift_modal_redirect(query: Mapping[str, str]) -> str | None:
    """Target for the old ``/plans?gifts=1`` modal link, keeping only the flash params."""
    if query.get("gifts") not in {"1", "true", "yes"}:
        return None
    flash = {key: query[key] for key in ("ok", "err") if query.get(key)}
    return GIFT_CODES_PATH + ("?" + urlencode(flash) if flash else "")


def register_gift_code_pages(
    app: FastAPI, *, render: Callable, require_staff: Callable, get_db: Callable,
) -> None:
    @app.get(GIFT_CODES_PATH)
    async def gift_codes_page(
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> Response:
        try:
            shop_id = gift_code_scope(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        codes = await list_gift_codes(session, shop_id=shop_id)
        plans = await gift_code_plan_options(session, shop_id=shop_id)
        plan_names = {plan_id: name for plan_id, name, _active in plans}
        flash_ok = request.query_params.get("ok")
        return render(
            request,
            "gift_codes.html",
            {
                "staff": staff,
                "gift_codes": codes,
                "gift_code_details": {code.id: describe_code(code, plan_names) for code in codes},
                "gift_code_plans": plans,
                "flash_ok": DONE_MESSAGE if flash_ok == "1" else flash_ok,
                "flash_err": request.query_params.get("err"),
            },
        )
