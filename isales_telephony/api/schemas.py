"""telephony-api-local DTOs not present in isales-common."""

from __future__ import annotations

from datetime import datetime
from typing import Generic, TypeVar

from isales_common.schemas._base import AppModel, ORMModel

T = TypeVar("T")


class Page(AppModel, Generic[T]):
    """Paginated list response."""

    items: list[T]
    total: int
    page: int
    page_size: int


class DeviceSimBindingRead(ORMModel):
    """Binding row read DTO. isales-common doesn't ship one — only telephony reads."""

    id: int
    device_id: int
    sim_card_id: int
    is_active: bool
    bind_at: datetime
    unbind_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
