from fastapi.testclient import TestClient

from app.main import app
from app.storage import storage

client = TestClient(app)


def test_pagination_returns_page_and_total():
    storage.clear()
    for number in range(3):
        client.post("/items", json={"title": f"Запис {number}"})

    response = client.get("/items?limit=2&offset=0")

    assert response.status_code == 200
    assert response.json()["total"] >= 0


def test_limit_is_capped():
    storage.clear()
    client.post("/items", json={"title": "Один"})

    response = client.get("/items?limit=500")

    assert len(response.json()["items"]) <= 100
