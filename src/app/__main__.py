"""Точка входу: `python -m app`.

Потрібна для того, щоб `make run` піднімав сервіс на тому хості і порту, які
задані у ваших змінних оточення, а не на тих, що зашиті в uvicorn.
"""

import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=True,
    )


if __name__ == "__main__":
    main()
