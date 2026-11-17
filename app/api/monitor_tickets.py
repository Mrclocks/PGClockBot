"""Automated ticket monitoring API endpoint for cron jobs."""

from __future__ import annotations

import os
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import SessionLocal
from app.services.panel_tickets import (
    PanelTicketStatus,
    actor_from_staff,
    get_ticket,
    list_tickets,
    reply_ticket,
)

router = APIRouter(prefix="/api/monitor", tags=["monitor"])

# Secret token for cron job authentication (set in .env)
MONITOR_TOKEN = os.getenv("TICKET_MONITOR_TOKEN", "").strip()


async def verify_token(token: str = Query(...)) -> None:
    """Verify the monitor token."""
    if not MONITOR_TOKEN:
        raise HTTPException(status_code=503, detail="Monitor token not configured")
    if token != MONITOR_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")


@router.post("/tickets/check", dependencies=[Depends(verify_token)])
async def monitor_tickets() -> dict:
    """
    Check for unanswered panel tickets and auto-respond.
    Called by external cron job (Hermes).
    """
    async with SessionLocal() as session:
        staff = {"role": "admin", "username": "Platform Admin (Auto)"}
        actor = actor_from_staff(staff)
    
    # Get all tickets for admin (owner)
    tickets = await list_tickets(session, staff, limit=100)
    
    results = []
    for ticket in tickets:
        # Only respond to tickets where admin hasn't responded yet
        if ticket.owner_unread and ticket.status in {
            PanelTicketStatus.OPEN.value,
            PanelTicketStatus.IN_PROGRESS.value
        }:
            # Load full ticket with messages
            full_ticket = await get_ticket(session, staff, ticket.id, load_messages=True)
            if not full_ticket:
                continue
            
            # Analyze the last user message to determine response
            user_messages = [m for m in full_ticket.messages if m.sender_role != 'admin']
            last_user_msg = user_messages[-1].body if user_messages else ""
            
            # Generate appropriate response based on content
            response = generate_response(last_user_msg, full_ticket)
            
            if response:
                # Reply as admin
                await reply_ticket(session, staff, ticket.id, body=response)
                results.append({
                    "ticket_id": ticket.id,
                    "subject": ticket.subject,
                    "opener": ticket.opener_label,
                    "opener_role": ticket.opener_role,
                    "response_preview": response[:200] + ("..." if len(response) > 200 else "")
                })
    
    return {
        "checked": len(tickets),
        "responded": len(results),
        "results": results
    }


def generate_response(last_user_msg: str, ticket) -> str:
    """Generate an appropriate response based on the user's message."""
    msg_lower = last_user_msg.lower()
    
    # Payment gateway related
    if any(kw in msg_lower for kw in ['مرچنت', 'zarinpal', 'tonpays', 'درگاه', 'پرداخت', 'payment', 'merchant', 'api key', 'کلید']):
        return (
            "سلام! در مورد تنظیم درگاه پرداخت:\n\n"
            "برای استفاده از درگاه تون پی (TonPays) در ربات خود:\n"
            "۱. به تنظیمات ربات > پرداخت > درگاه API بروید\n"
            "۲. ارائه‌دهنده را روی `tonpays` بگذارید\n"
            "۳. مرچنت/کلید API: کلید API تون پیز خود را وارد کنید (از پنل tonpays.online)\n"
            "۴. سندباکس: غیرفعال (0)\n\n"
            "اگر از زارین‌پال استفاده می‌کنید، ارائه‌دهنده را روی `zarinpal` بگذارید و مرچنت کد زارین‌پال خود را وارد کنید.\n\n"
            "هر نماینده می‌تواند درگاه پرداخت مخصوص خود را در تنظیمات رباتش پیکربندی کند.\n\n"
            "آیا نیاز به راهنمای بیشتری دارید؟"
        )
    
    # Reseller application related
    if any(kw in msg_lower for kw in ['نماینده', 'reseller', 'نمایندگی', 'اعمال', 'apply', 'نمایندگی']):
        return (
            "سلام! برای درخواست نمایندگی:\n\n"
            "۱. در ربات اصلی دکمه «درخواست نمایندگی» را بزنید\n"
            "۲. فرم را تکمیل کنید\n"
            "۳. پس از تایید ادمین، دسترسی پنل نماینده به شما داده می‌شود\n\n"
            "در پنل نماینده می‌توانید:\n"
            "- پلن‌های فروش بسازید\n"
            "- تنظیمات پرداخت (درگاه تون پیز، کارت به کارت، و...) را برای ربات خود پیکربندی کنید\n"
            "- مشتری‌های خود را مدیریت کنید\n\n"
            "مشکل خاصی در فرآیند دارید؟"
        )
    
    # Error/bug reports
    if any(kw in msg_lower for kw in ['خطا', 'error', 'باغ', 'کار نمی‌کنه', 'نمیاد', 'مشکل', 'bug', ' خراب', 'نه', 'نشد']):
        return (
            "سلام! برای بررسی خطا لطفاً اطلاعات زیر را بفرستید:\n\n"
            "۱. متن کامل خطا (اسکرین‌شات یا کپی متن)\n"
            "۲. مرحله‌ای که خطا رخ داده (خرید، پرداخت، اتصال، و...)\n"
            "۳. آیدی عددی تلگرام شما\n"
            "۴. اگر مربوط به درگاه است: ارائه‌دهنده (tonpays/zarinpal/...)\n\n"
            "با این اطلاعات می‌توانم سریع‌تر کمکتان کنم."
        )
    
    # Default helpful response
    return (
        f"سلام {ticket.opener_label}!\n\n"
        "تیکت شما دریافت شد. من ادمین پلتفرم هستم و به زودی بررسی می‌کنم.\n\n"
        "اگر سوال فنی دارید، لطفاً جزئیات بیشتری بدهید تا دقیق‌تر راهنمایی کنم."
    )