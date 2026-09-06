from fastapi.testclient import TestClient

from app.main import app
from app.storage import storage

client = TestClient(app)


def test_created_item_is_readable_back():
    storage.clear()

    created = client.post("/items", json={"title": "Перший запис"})
    assert created.status_code == 201
    item_id = created.json()["id"]

    assert client.get("/items").json() != []
    assert client.get(f"/items/{item_id}").json()["title"] == "Перший запис"
