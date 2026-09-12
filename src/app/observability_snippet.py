"""Мінімальна спостережуваність для тих, хто не робив ЛР9.

Запасний вхід курсу. Дає рівно те, без чого не пишеться постмортем: один рядок
JSON на запит із часом, ідентифікатором, кодом відповіді і тривалістю. Формат
той самий, що вимагає ЛР9, тому далі нічого переробляти не доведеться.

Як підключити: покласти файл у `src/app/observability_snippet.py` і додати в
`src/app/main.py` два рядки після створення `app`:

    from app.observability_snippet import install
    install(app)

Це не звільняє від ЛР9: її правила лишаться червоними, бо там оцінюються ще
метрики і SLO. Але інцидент ви побачите, а саме це потрібно сьогодні.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from datetime import UTC, datetime


def log(event: str, level: str = "info", **fields) -> None:
    record = {"ts": datetime.now(UTC).isoformat(), "level": level, "event": event, **fields}
    print(json.dumps(record, ensure_ascii=False), file=sys.stdout, flush=True)


def install(app) -> None:
    """Вішає middleware, який пише рядок на кожен запит, включно з падіннями."""

    @app.middleware("http")
    async def observe(request, call_next):
        request_id = uuid.uuid4().hex[:8]
        started = time.perf_counter()

        def write(status: int, **extra) -> None:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            level = "error" if status >= 500 else ("warning" if status >= 400 else "info")
            log(
                "request",
                level=level,
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=duration_ms,
                **extra,
            )

        try:
            response = await call_next(request)
        except Exception as error:
            # Без цього блоку падіння проходить повз лог, і саме його ви шукаєте.
            write(500, error=type(error).__name__)
            raise

        write(response.status_code)
        response.headers["X-Request-ID"] = request_id
        return response
