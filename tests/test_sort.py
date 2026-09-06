"""Тест виклику курсу: сортування списку за датою.

Цей файл не змінюється. Він приїхав разом зі зміною курсу і має лишитись
зеленим після того, як ви розв'яжете конфлікт.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.storage import storage

client = TestClient(app)


def test_default_order_is_oldest_first():
    storage.clear()
    client.post("/items", json={"title": "Перша"})
    client.post("/items", json={"title": "Друга"})

    titles = [item["title"] for item in client.get("/items").json()]

    assert titles == ["Перша", "Друга"]


def test_minus_prefix_gives_newest_first():
    storage.clear()
    client.post("/items", json={"title": "Перша"})
    client.post("/items", json={"title": "Друга"})

    titles = [item["title"] for item in client.get("/items?sort=-created_at").json()]

    assert titles == ["Друга", "Перша"]


def test_unknown_sort_field_is_rejected():
    storage.clear()

    assert client.get("/items?sort=title").status_code == 422
