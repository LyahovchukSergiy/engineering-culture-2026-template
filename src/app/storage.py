"""Сховище записів.

Дані живуть у пам'яті процесу і зникають після перезапуску. Для стартового
сервісу цього достатньо. Якщо ви візьмете з беклогу фічу, якій потрібне
справжнє сховище, замініть цей клас, а не розтягуйте роботу з даними по
всьому коду.
"""

from datetime import UTC, datetime

from app.models import Item, ItemCreate


class ItemStorage:
    def __init__(self) -> None:
        self._items: dict[int, Item] = {}
        self._next_id = 1

    def add(self, payload: ItemCreate) -> Item:
        item = Item(
            id=self._next_id,
            created_at=datetime.now(UTC),
            **payload.model_dump(),
        )
        self._items[item.id] = item
        self._next_id += 1
        return item

    def list_all(self) -> list[Item]:
        return list(self._items.values())

    def get(self, item_id: int) -> Item | None:
        return self._items.get(item_id)

    def clear(self) -> None:
        self._items.clear()
        self._next_id = 1


storage = ItemStorage()
