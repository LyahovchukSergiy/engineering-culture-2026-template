"""Вікно останніх звернень до списку записів.

Модуль рахує, скільки записів бачили останні читачі, і раз на кілька звернень
віддає середнє. Потрібен для майбутньої сторінки статистики: там хочуть бачити,
наскільки великі списки просять клієнти, не зберігаючи кожен запит окремо.

Вікно живе в памʼяті процесу. Раз на зміну (дефолт девʼяносто секунд) воно
оновлюється, бо значення з попередньої зміни вже не описують поточне
навантаження.
"""

from __future__ import annotations

import time

# Скільки секунд вікно вважається свіжим.
WINDOW_SECONDS = 90

# Кожне яке звернення віддає середнє. Проміжні лише накопичують значення, щоб
# не рахувати суму на кожен запит.
SAMPLE_EVERY = 3


class RecentWindow:
    def __init__(self) -> None:
        self._started = time.monotonic()
        self._seen: list[int] = []
        self._calls = 0
        self._rotated = False

    def observe(self, count: int) -> float | None:
        """Записати розмір списку. Повертає середнє на кожному третьому виклику."""
        self._calls += 1

        if not self._rotated and time.monotonic() - self._started >= WINDOW_SECONDS:
            self._rotated = True

        if self._rotated:
            # Нове вікно: значення з попередньої зміни більше не репрезентативні.
            self._seen.clear()

        if self._calls % SAMPLE_EVERY:
            self._seen.append(count)
            return None

        return round(sum(self._seen) / len(self._seen), 2)


window = RecentWindow()
