#!/usr/bin/env python3
"""Прогін мутантів курсу проти тестів репозиторію.

Кожен мутант це повна самодостатня реалізація `legacy/pricing.py` з однією
зміненою поведінкою. Скрипт по черзі підміняє модуль мутантом, ганяє тести і
повертає файл на місце. Мутант вважається вбитим, коли хоча б один тест упав.

Той самий скрипт ганяє workflow у репозиторії студента (чотири публічні
мутанти) і викладач при оцінюванні (усі вісім). Прихованої логіки тут немає.

Залежностей немає навмисно: тільки стандартна бібліотека.

Запуск:

    python run_mutants.py --mutants .course-mutants
    python run_mutants.py --repo ../student-repo --mutants ~/course/mutants_all
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

TARGET = "legacy/pricing.py"

KILLED = "ВБИТИЙ"
SURVIVED = "ВИЖИВ"
BROKEN = "НЕ РАХУЄТЬСЯ"


def clear_pycache(repo: Path) -> None:
    """Прибирає кеш байткоду перед кожним прогоном.

    Без цього мутанти рахуються неправильно, і помиляється воно тихо. Python
    вважає кеш свіжим, коли збігаються час зміни і розмір файла, а мутанти це
    той самий модуль з однією зміненою константою, тому розміри в них часто
    однакові. Два мутанти поспіль у межах секунди дають другому чужий байткод.
    """
    for item in repo.rglob("__pycache__"):
        if ".venv" in item.parts or "venv" in item.parts:
            continue
        shutil.rmtree(item, ignore_errors=True)


def run_tests(python: str, repo: Path) -> tuple[int, str]:
    clear_pycache(repo)
    done = subprocess.run(
        [python, "-B", "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=900,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    return done.returncode, done.stdout + done.stderr


def first_failure(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("FAILED "):
            return line[len("FAILED ") :].split(" - ")[0]
    return ""


def collection_error(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("ERROR "):
            return "тести не зібрались: " + line[len("ERROR ") :].strip()
    return "тести не зібрались"


def main() -> int:
    parser = argparse.ArgumentParser(description="Прогін мутантів курсу")
    parser.add_argument("--repo", default=".", help="каталог репозиторію")
    parser.add_argument("--mutants", required=True, help="каталог з файлами мутантів")
    parser.add_argument("--python", default=sys.executable, help="інтерпретатор для pytest")
    parser.add_argument("--summary", help="дописати звіт у файл, для GitHub Actions")
    args = parser.parse_args()

    repo = Path(args.repo).resolve()
    target = repo / TARGET
    if not target.is_file():
        print(f"У {repo} немає {TARGET}. Це не репозиторій курсу.")
        return 1

    mutants = sorted(Path(args.mutants).resolve().glob("m*.py"))
    if not mutants:
        print(f"У {args.mutants} немає жодного файла мутанта.")
        return 1

    code, output = run_tests(args.python, repo)
    if code != 0:
        print("Ваші тести червоні ще до підміни модуля, тому рахувати мутантів немає сенсу.")
        print("Спершу зробіть `make test` зеленим.")
        print()
        print(output.strip()[-1500:])
        return 1

    backup = target.read_bytes()
    rows: list[tuple[str, str, str]] = []
    try:
        for mutant in mutants:
            target.write_bytes(mutant.read_bytes())
            code, output = run_tests(args.python, repo)
            if code == 1:
                rows.append((mutant.stem, KILLED, first_failure(output)))
            elif code == 0:
                rows.append((mutant.stem, SURVIVED, "жоден тест не помітив підміни"))
            else:
                rows.append((mutant.stem, BROKEN, collection_error(output)))
    finally:
        target.write_bytes(backup)

    killed = sum(1 for _, verdict, _ in rows if verdict == KILLED)
    broken = [name for name, verdict, _ in rows if verdict == BROKEN]

    lines = ["# Мутанти курсу", ""]
    for name, verdict, note in rows:
        lines.append(f"- `{verdict:12}` **{name}** {note}")
    lines.append("")
    lines.append(f"**Спіймано {killed} з {len(rows)}.**")
    if broken:
        lines.append("")
        lines.append(
            "Мутанти " + ", ".join(broken) + " не порахувались: тести не змогли навіть "
            "зібратись. Найчастіша причина це тест до допоміжної функції, якої в мутанті "
            "немає. Тести пишіть до публічної функції `calculate_order_total`."
        )
    report = "\n".join(lines)

    print(report)
    if args.summary:
        with open(args.summary, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")
    if os.environ.get("GITHUB_STEP_SUMMARY") and not args.summary:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write(report + "\n")

    return 0 if killed == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
