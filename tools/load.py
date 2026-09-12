#!/usr/bin/env python3
"""Навантаження на сервіс: кілька сотень запитів за хвилину, частина з них падає.

Скрипт курсу. Він нічого не лагодить і нічого не ламає в коді: просто стукає в
сервіс так, як стукав би справжній клієнт, і рахує, що з цього вийшло, зі свого
боку.

Частина запитів навмисно некоректна, і це не помилка скрипта. Реальний трафік
теж містить биті запити: старий мобільний клієнт, чужий скрипт, переплутаний
ідентифікатор. Питання роботи саме в цьому: сервіс бачить те саме, що бачить
скрипт, і ви маєте вміти дістати з нього ті самі числа.

Залежностей немає, тільки стандартна бібліотека.

Запуск:

    python3 tools/load.py                       60 секунд на 127.0.0.1:8000
    python3 tools/load.py --seconds 20          коротший прогін
    python3 tools/load.py --url http://127.0.0.1:8080
    python3 tools/load.py --seed 7              інший набір запитів
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

TIMEOUT = 5.0

# Частки запитів. Сума не має значення, вони беруться як ваги.
MIX = (
    ("list", 45),  # GET /items, коректний
    ("create", 25),  # POST /items, коректний
    ("get_missing", 15),  # GET /items/<великий id>, дає 404
    ("create_invalid", 10),  # POST /items з порожнім title, дає 422
    ("health", 5),  # GET /health
)

TITLES = (
    "запис із навантаження",
    "перевірка черги",
    "тестовий запис",
    "заявка з форми",
)


def pick(rng: random.Random) -> str:
    names = [name for name, _ in MIX]
    weights = [weight for _, weight in MIX]
    return rng.choices(names, weights=weights, k=1)[0]


def build(kind: str, base: str, rng: random.Random) -> tuple[str, str, bytes | None]:
    """(метод, адреса, тіло) для одного запиту."""
    if kind == "list":
        return "GET", f"{base}/items", None
    if kind == "health":
        return "GET", f"{base}/health", None
    if kind == "get_missing":
        return "GET", f"{base}/items/{rng.randint(90000, 99999)}", None
    if kind == "create":
        body = {"title": rng.choice(TITLES), "description": "згенеровано tools/load.py"}
        return "POST", f"{base}/items", json.dumps(body).encode("utf-8")
    body = {"title": "", "description": "порожня назва, сервіс має відповісти 422"}
    return "POST", f"{base}/items", json.dumps(body).encode("utf-8")


def send(kind: str, base: str, rng: random.Random) -> tuple[str, int | str, float]:
    """(вид запиту, код відповіді або назва помилки, тривалість у мілісекундах)."""
    method, url, body = build(kind, base, rng)
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            response.read()
            code: int | str = response.status
    except urllib.error.HTTPError as error:
        error.read()
        code = error.code
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as error:
        code = type(error).__name__
    return kind, code, (time.perf_counter() - started) * 1000


def percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


def report(results: list[tuple[str, int | str, float]], seconds: float) -> None:
    total = len(results)
    durations = [item[2] for item in results]
    by_code: dict[int | str, int] = {}
    for _, code, _ in results:
        by_code[code] = by_code.get(code, 0) + 1

    ok = sum(count for code, count in by_code.items() if isinstance(code, int) and code < 400)
    bad = total - ok

    print()
    print("=" * 62)
    print(f"Надіслано запитів: {total} за {seconds:.1f} с")
    print(f"Успішних (код менший за 400): {ok}")
    print(f"Невдалих: {bad}")
    print()
    print("За кодами відповіді:")
    for code in sorted(by_code, key=str):
        share = by_code[code] / total * 100
        print(f"  {str(code):>24}  {by_code[code]:>5}  {share:5.1f}%")
    print()
    print("Тривалість відповіді, мілісекунди:")
    print(f"  медіана {statistics.median(durations):.1f}")
    print(f"  p95     {percentile(durations, 0.95):.1f}")
    print(f"  максимум {max(durations):.1f}")
    print("=" * 62)
    print()
    print("Це числа з боку клієнта. Тепер дістаньте ті самі з боку сервісу:")
    print("  скільки запитів упало, які саме і чому;")
    print("  скільки з них були повільнішими за вашу межу.")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Навантаження на сервіс курсу")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="адреса сервісу")
    parser.add_argument("--seconds", type=float, default=60.0, help="скільки часу слати")
    parser.add_argument("--workers", type=int, default=4, help="скільки паралельних клієнтів")
    parser.add_argument("--pause", type=float, default=0.4, help="пауза між запитами клієнта")
    parser.add_argument("--seed", type=int, default=1, help="зерно генератора запитів")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    probe = send("health", base, random.Random(0))
    if not isinstance(probe[1], int):
        print(f"Сервіс на {base} не відповідає: {probe[1]}.", file=sys.stderr)
        print("Підніміть його командою make run і повторіть.", file=sys.stderr)
        return 1

    print(f"Стукаю в {base} протягом {args.seconds:.0f} с, клієнтів {args.workers}.")
    print("Дивіться у вивід сервісу, поки це йде.")
    print("Один запит на /health уже пішов на перевірку зв'язку: сервіс побачить")
    print("на один запит більше, ніж порахує цей звіт.")

    results: list[tuple[str, int | str, float]] = []
    deadline = time.monotonic() + args.seconds
    started = time.perf_counter()

    def client(number: int) -> list[tuple[str, int | str, float]]:
        rng = random.Random(args.seed * 1000 + number)
        mine = []
        while time.monotonic() < deadline:
            mine.append(send(pick(rng), base, rng))
            time.sleep(args.pause)
        return mine

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for chunk in pool.map(client, range(args.workers)):
            results.extend(chunk)

    report(results, time.perf_counter() - started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
