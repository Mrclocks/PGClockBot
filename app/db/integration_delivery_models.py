"""Private API delivery material is encrypted and never stored on public service rows."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class IntegrationDelivery(Base):
    __tablename__ = "integration_deliveries"

    service_id: Mapped[int] = mapped_column(ForeignKey("user_services.id"), primary_key=True)
    url_enc: Mapped[str] = mapped_column(Text)
    url_digest: Mapped[str] = mapped_column(String(64))
    access_version: Mapped[int] = mapped_column(default=1, server_default="1")
    validated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
