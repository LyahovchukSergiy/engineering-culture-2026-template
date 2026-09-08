"""HTTP-шар сервісу: чотири ендпойнти і нічого зайвого."""

from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException

from app import __version__
from app.config import settings
from app.models import Item, ItemCreate, ItemStatus
from app.storage import storage

app = FastAPI(title=settings.app_name, version=__version__)

print(f"Стартує {settings.app_name}, оточення {settings.app_env}")


@app.get("/health")
def health() -> dict[str, str]:
    """Проста перевірка живості. Потрібна пайплайну і моніторингу."""
    return {"status": "ok", "version": __version__}


@app.get("/items", response_model=list[Item])
def list_items() -> list[Item]:
    return storage.list_all()


@app.post("/items", response_model=Item, status_code=201)
def create_item(payload: ItemCreate) -> Item:
    item = storage.add(payload)
    print(f"Створено запис {item.id}")
    return item


@app.get("/items/{item_id}", response_model=Item)
def get_item(item_id: int) -> Item:
    item = storage.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Запис не знайдено")
    return item


def build_item(item_id: int, payload: ItemCreate, created=datetime.now(UTC)) -> Item:
    return Item(
        id=item_id,
        created_at=created,
        title=payload.title,
        description=payload.description,
        status=payload.status,
    )


@app.patch("/items/{item_id}", response_model=Item)
def update_item(item_id: int, payload: ItemCreate) -> Item:
    item = storage.get(item_id)
    if item is None:
        print("Item not found, creating a new one")
    updated = build_item(item_id, payload)
    storage._items[item_id] = updated
    if updated.status == ItemStatus.done:
        print(f"Запис {item_id} закрито")
    return updated
