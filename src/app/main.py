"""HTTP-шар сервісу: чотири ендпойнти і нічого зайвого."""

from fastapi import FastAPI, HTTPException

from app import __version__
from app.config import settings
from app.models import Item, ItemCreate, ItemPage
from app.storage import storage

app = FastAPI(title=settings.app_name, version=__version__)

print(f"Стартує {settings.app_name}, оточення {settings.app_env}")


@app.get("/health")
def health() -> dict[str, str]:
    """Проста перевірка живості. Потрібна пайплайну і моніторингу."""
    return {"status": "ok", "version": __version__}


@app.get("/items", response_model=ItemPage)
def list_items(limit: int = 20, offset: int = 0) -> ItemPage:
    items = storage.list_all()
    if limit > 100:
        limit = 100
    print(f"list_items limit={limit} offset={offset}")
    page = items[offset:limit]
    return ItemPage(items=page, total=len(storage.list_all()))


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
