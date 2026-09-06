"""Точка входу: `python -m app`.

Потрібна для того, щоб `make run` піднімав сервіс на тому хості і порту, які
задані у ваших змінних оточення, а не на тих, що зашиті в uvicorn.

Автоматичне перезавантаження при зміні коду вмикається змінною `APP_RELOAD` і
за замовчуванням вимкнене. `make run` вмикає його сам, бо локально воно зручне.
В образі воно шкідливе: стежити за файлами, які ніколи не змінюються, це зайвий
процес і гірше завершення роботи за сигналом.
"""

import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
