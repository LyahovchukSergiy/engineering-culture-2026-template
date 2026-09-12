from fastapi.testclient import TestClient

from app.main import app
from app.reports import build_csv
from app.storage import storage

client = TestClient(app)


def test_summary_counts_statuses():
    storage.clear()
    client.post("/items", json={"title": "Перший"})
    client.post("/items", json={"title": "Другий"})

    assert client.get("/reports/summary").json()["new"] == 2


def test_export_contains_every_item():
    storage.clear()
    output = build_csv(storage.list_all())

    for item in storage.list_all():
        assert item.title in output
