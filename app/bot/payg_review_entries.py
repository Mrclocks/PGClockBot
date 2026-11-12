"""Entry of the PAYG charge review screen, shared by the ops hub and its handler."""

from __future__ import annotations

from app.bot.hub_entries import HubEntry

PAYG_REVIEW_CALLBACK = "pcr:l:0"
PAYG_REVIEW = HubEntry("btn_adm_payg_review", PAYG_REVIEW_CALLBACK, "🧾 بازبینی PAYG")
