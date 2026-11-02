"""Shop staff change a customer's shop wallet by hand (the customer edit modal)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.user_pages import _redirect_user
from app.services.formatting import format_toman
from app.services.shop_wallet_adjust import (
    ShopWalletAdjustError,
    adjust_shop_wallet,
    parse_request,
    wallet_adjust_shop,
)

SHOP_WALLET_PATH = "/users/{user_id}/shop-wallet"


def register_shop_wallet_pages(app: FastAPI, *, require_staff: Callable, get_db: Callable) -> None:
    @app.post(SHOP_WALLET_PATH)
    async def shop_wallet_adjust(
        user_id: int,
        request: Request,
        staff: dict = Depends(require_staff),
        session: AsyncSession = Depends(get_db),
    ) -> RedirectResponse:
        try:
            wallet_adjust_shop(staff)
        except PermissionError:
            return RedirectResponse("/home", status_code=303)
        form = await request.form()
        try:
            change = await adjust_shop_wallet(
                session,
                staff=staff,
                request=parse_request(
                    customer_id=user_id, raw_amount=form.get("amount"), direction=form.get("mode"),
                    reason=form.get("note"), request_key=form.get("nonce"),
                ),
            )
        except ShopWalletAdjustError as exc:
            return _redirect_user(user_id, err=str(exc))
        verb = "افزایش" if change.direction == "credit" else "کاهش"
        return _redirect_user(
            user_id,
            ok=f"کیف پول {format_toman(change.amount)} {verb} یافت · موجودی: {format_toman(change.balance_after)}",
        )
