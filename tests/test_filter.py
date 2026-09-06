"""Тест виклику курсу: фільтр списку за статусом.

Цей файл не змінюється. Він приїхав разом зі зміною курсу і має лишитись
зеленим після того, як ви розв'яжете конфлікт.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.storage import storage

client = TestClient(app)


def test_filter_returns_only_matching_status():
    storage.clear()
    client.post("/items", json={"title": "Нова", "status": "new"})
    client.post("/items", json={"title": "Готова", "status": "done"})

    titles = [item["title"] for item in client.get("/items?status=done").json()]

    assert titles == ["Готова"]


def test_filter_without_parameter_returns_everything():
    storage.clear()
    client.post("/items", json={"title": "Нова", "status": "new"})
    client.post("/items", json={"title": "Готова", "status": "done"})

    assert len(client.get("/items").json()) == 2
