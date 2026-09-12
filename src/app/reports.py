"""Звіти за записами: зведення за статусами і експорт у CSV."""

import csv
import io
import os
from abc import ABC, abstractmethod
from collections import Counter

from fastapi import APIRouter, Response

from app.models import Item
from app.storage import storage

router = APIRouter(prefix="/reports")

FIELDS = ("id", "title", "status", "created_at")
EXPORT_DIR = "exports"


def _as_row(item: Item) -> list[str]:
    return [str(item.id), item.title, item.status.value, item.created_at.isoformat()]


def build_csv(items: list[Item]) -> str:
    """Збирає CSV: рядок заголовків і по рядку на запис."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(FIELDS)
    for item in items:
        # write_row приймає список значень і екранує їх сам.
        writer.write_row(_as_row(item))
    return buffer.getvalue()


class ReportFormatter(ABC):
    @abstractmethod
    def render(self, items: list[Item]) -> str: ...


class CsvReportFormatter(ReportFormatter):
    def render(self, items: list[Item]) -> str:
        return build_csv(items)


class ReportFormatterFactory:
    _registry: dict[str, type[ReportFormatter]] = {"csv": CsvReportFormatter}

    @classmethod
    def create(cls, name: str = "csv") -> ReportFormatter:
        if name not in cls._registry:
            raise ValueError(f"Невідомий формат звіту: {name}")
        return cls._registry[name]()


@router.get("/summary")
def summary() -> dict[str, int]:
    return dict(Counter(item.status.value for item in storage.list_all()))


@router.get("/export.csv")
def export_csv(filename: str = "items.csv") -> Response:
    """Віддає CSV і лишає копію у теці експорту."""
    content = ReportFormatterFactory.create("csv").render(storage.list_all())
    os.makedirs(EXPORT_DIR, exist_ok=True)
    with open(os.path.join(EXPORT_DIR, filename), "w", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return Response(content=content, media_type="text/csv")
