from fastapi.testclient import TestClient

from app.main import app
from app.storage import storage

client = TestClient(app)


def test_update_changes_title():
    storage.clear()
    created = client.post("/items", json={"title": "Було"})
    item_id = created.json()["id"]

    response = client.patch(f"/items/{item_id}", json={"title": "Стало"})

    assert response.status_code == 200
    assert response.json()["title"] == "Стало"
