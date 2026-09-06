"""Конфігурація сервісу.

Значення приходять зі змінних оточення. Локально їх зручно тримати у файлі
`.env`, який у git не потрапляє. Перелік ключів дивіться в `.env.example`.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    """Повертає значення змінної оточення або запасне, якщо змінна порожня."""
    value = os.getenv(name, "")
    return value.strip() or default


@dataclass(frozen=True)
class Settings:
    app_name: str
    app_env: str
    host: str
    port: int
    log_level: str
    reload: bool


def load_settings() -> Settings:
    return Settings(
        app_name=_env("APP_NAME", "Starter Service"),
        app_env=_env("APP_ENV", "local"),
        host=_env("APP_HOST", "127.0.0.1"),
        port=int(_env("APP_PORT", "8000")),
        log_level=_env("APP_LOG_LEVEL", "info"),
        reload=_env("APP_RELOAD", "0").lower() in {"1", "true", "yes"},
    )


settings = load_settings()
