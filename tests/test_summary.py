from fastapi.testclient import TestClient

from app.main import app
from app.storage import storage
from app.summary import INTERNAL_API_KEY

client = TestClient(app)


def test_summary_without_key_is_rejected():
    assert client.get("/summary").status_code == 401


def test_summary_lists_items():
    storage.clear()
    client.post("/items", json={"title": "Перший запис"})

    response = client.get("/summary", headers={"X-Internal-Key": INTERNAL_API_KEY})

    assert response.status_code == 200
    assert "Записів: 1" in response.text
    assert "Перший запис" in response.text
