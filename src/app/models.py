"""Моделі даних сервісу."""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ItemStatus(str, Enum):
    new = "new"
    in_progress = "in_progress"
    done = "done"


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    status: ItemStatus = ItemStatus.new


class Item(ItemCreate):
    id: int
    created_at: datetime


class ItemPage(BaseModel):
    items: list[Item]
    total: int
