#!/usr/bin/env python3
"""Чотири ключові метрики DORA за історією цього репозиторію.

Скрипт курсу «Інженерна культура та інструменти командної розробки». Рахує
частоту поставки, час проходження зміни, частку невдалих змін і час
відновлення, друкує готовий звіт у Markdown і таблицю по тижнях.

Запуск:

    python3 tools/dora.py
    python3 tools/dora.py --since 2026-09-08 --until 2026-12-01
    python3 tools/dora.py --since 2026-09-08 --until 2026-12-01 --split 2026-11-17
    python3 tools/dora.py --json
    python3 tools/dora.py --repo ../inshyi-repozytorii

Звіт друкується в термінал, копіюйте його в `docs/metrics.md`. Рядок «Команда
для повторення» друкується з проставленими межами вікна навмисно: число без
меж вікна нічого не означає, а з ними той самий прогін завтра дає той самий
результат.

Час усюди UTC. Інакше ваш звіт і звіт перевірки розійшлись би на кордоні доби
просто через різні часові пояси машин.

Залежностей немає: тільки стандартна бібліотека і git. Дані pull request
беруться з GitHub API анонімно, а якщо в оточенні є GH_TOKEN або GITHUB_TOKEN,
то з ним.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

VERSION_TAG = re.compile(r"^v?\d+\.\d+\.\d+")
FIX_SUBJECT = re.compile(r"^(fix|revert)(\([^)]*\))?!?:", re.IGNORECASE)
REVERT_SUBJECT = re.compile(r'^revert\s+"(.+)"\s*$', re.IGNORECASE)
REVERTS_COMMIT = re.compile(r"[Tt]his reverts commit ([0-9a-f]{7,40})")
PR_SUFFIX = re.compile(r"\s+\(#\d+\)$")
HOUR = 3600.0
UTC_ZONE = timezone.utc

RECORD = "\x1e"
FIELD = "\x1f"


class Fail(Exception):
    """Помилка, яку видно користувачу одним рядком, без трасування."""


# --------------------------------------------------------------------------- #
# Дані
# --------------------------------------------------------------------------- #


def git(repo: Path, args: list[str]) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise Fail(f"git {' '.join(args)}: {done.stderr.strip() or 'не вдалося'}")
    return done.stdout


def main_ref(repo: Path) -> str:
    """Головна гілка. Спершу віддалена: у прогоні на pull_request локальної main
    у клоні немає, і git log по ній падає з bad revision."""
    for candidate in ("refs/remotes/origin/main", "refs/heads/main"):
        done = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        if done.returncode == 0:
            return candidate
    raise Fail("у репозиторії немає гілки main ні локально, ні в origin")


def slug(repo: Path) -> str | None:
    done = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo, capture_output=True, text=True
    )
    if done.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+)/([^/.\s]+)", done.stdout.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def moment(value: str) -> datetime:
    """Дата з командного рядка або з git. YYYY-MM-DD це початок доби UTC."""
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=UTC_ZONE)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC_ZONE)
    except ValueError as error:
        raise Fail(f"не розумію дату {value!r}, треба YYYY-MM-DD") from error


def stamp(when: datetime) -> str:
    return when.astimezone(UTC_ZONE).strftime("%Y-%m-%dT%H:%M:%SZ")


def commits(repo: Path, ref: str) -> list[dict]:
    """Усі коміти головної гілки з файлами, які вони змінили, від старих до нових."""
    raw = git(
        repo,
        [
            "log",
            ref,
            "--no-merges",
            "--name-only",
            f"--format={RECORD}%H{FIELD}%cI{FIELD}%aI{FIELD}%s{FIELD}%b{FIELD}",
        ],
    )
    out: list[dict] = []
    for chunk in raw.split(RECORD):
        if not chunk.strip():
            continue
        parts = chunk.split(FIELD)
        if len(parts) < 6:
            continue
        sha, landed, written, subject, body, tail = parts[:6]
        files = [line.strip() for line in tail.splitlines() if line.strip()]
        out.append(
            {
                "sha": sha.strip(),
                "landed": moment(landed),
                "written": moment(written),
                "subject": subject.strip(),
                "body": body,
                "files": files,
            }
        )
    out.reverse()
    return out


def releases(repo: Path) -> list[dict]:
    """Теги вигляду vX.Y.Z. Для анотованого тега дата це дата тега, для
    легкого дата коміту: саме це і дає %(creatordate)."""
    raw = git(
        repo,
        [
            "for-each-ref",
            "--format=%(refname:short)" + FIELD + "%(creatordate:iso-strict)",
            "refs/tags",
        ],
    )
    out = []
    for line in raw.splitlines():
        if FIELD not in line:
            continue
        name, when = line.split(FIELD, 1)
        if not VERSION_TAG.match(name.strip()):
            continue
        out.append({"name": name.strip(), "at": moment(when)})
    out.sort(key=lambda item: item["at"])
    return out


def api(path: str) -> list | dict | None:
    url = f"https://api.github.com/{path}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dora.py"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or gh_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers=headers), timeout=30
        ) as response:
            return json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return None


def gh_token() -> str | None:
    try:
        done = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None


def pulls(name: str | None) -> tuple[list[dict], str]:
    """Змержені pull request. Другим значенням повертає джерело: 'api' або причину."""
    if name is None:
        return [], "немає remote origin на GitHub"
    found: list[dict] = []
    for page in (1, 2, 3):
        batch = api(f"repos/{name}/pulls?state=closed&per_page=100&page={page}")
        if batch is None:
            return [], "GitHub API не відповів"
        if not isinstance(batch, list):
            return [], "GitHub API віддав не те, що очікувалось"
        for item in batch:
            if not item.get("merged_at"):
                continue
            found.append(
                {
                    "number": item.get("number"),
                    "opened": moment(item["created_at"]),
                    "merged": moment(item["merged_at"]),
                }
            )
        if len(batch) < 100:
            break
    found.sort(key=lambda item: item["merged"])
    return found, "api"


# --------------------------------------------------------------------------- #
# Метрики
# --------------------------------------------------------------------------- #


def is_fix(commit: dict) -> bool:
    subject = PR_SUFFIX.sub("", commit["subject"])
    return bool(FIX_SUBJECT.match(subject) or REVERT_SUBJECT.match(subject))


def broke_it(commit: dict, history: list[dict]) -> dict | None:
    """Зміна, яку виправляє цей коміт.

    Для відкату це точна пара: git пише в тіло коміту «This reverts commit», а
    кнопка Revert на GitHub лишає назву в лапках. Для `fix:` точної пари немає,
    тому береться найсвіжіша попередня зміна, яка чіпала хоч один спільний файл
    і сама не була виправленням.
    """
    earlier = [item for item in history if item["landed"] < commit["landed"]]
    subject = PR_SUFFIX.sub("", commit["subject"])

    match = REVERTS_COMMIT.search(commit["body"] or "")
    if match:
        prefix = match.group(1)
        for item in reversed(earlier):
            if item["sha"].startswith(prefix):
                return item

    quoted = REVERT_SUBJECT.match(subject)
    if quoted:
        wanted = quoted.group(1).strip()
        for item in reversed(earlier):
            if PR_SUFFIX.sub("", item["subject"]) == wanted:
                return item

    touched = set(commit["files"])
    if not touched:
        return None
    for item in reversed(earlier):
        if is_fix(item):
            continue
        if touched & set(item["files"]):
            return item
    return None


def window_slice(items: list[dict], key: str, since: datetime, until: datetime) -> list[dict]:
    return [item for item in items if since <= item[key] < until]


def measure(data: dict, since: datetime, until: datetime) -> dict:
    """Чотири метрики на заданому вікні."""
    changes = window_slice(data["commits"], "landed", since, until)
    fixes = [item for item in changes if is_fix(item)]
    tags = window_slice(data["releases"], "at", since, until)
    merged = window_slice(data["pulls"], "merged", since, until)

    weeks = (until - since).total_seconds() / (7 * 24 * HOUR)
    lead = [(item["merged"] - item["opened"]).total_seconds() / HOUR for item in merged]

    recovery = []
    for item in fixes:
        broken = broke_it(item, data["commits"])
        if broken is not None:
            recovery.append((item["landed"] - broken["landed"]).total_seconds() / HOUR)

    return {
        "weeks": weeks,
        "changes": len(changes),
        "fixes": len(fixes),
        "releases": len(tags),
        "release_names": [item["name"] for item in tags],
        "pulls": len(merged),
        "lead_hours": statistics.median(lead) if lead else None,
        "fail_rate": (100.0 * len(fixes) / len(changes)) if changes else None,
        "recovery_hours": statistics.median(recovery) if recovery else None,
        "recovery_cases": len(recovery),
        "frequency": (len(tags) / weeks) if weeks > 0 else None,
    }


def weeks_between(since: datetime, until: datetime) -> list[datetime]:
    start = since - timedelta(days=since.weekday())
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    out = []
    cursor = start
    while cursor < until:
        out.append(cursor)
        cursor += timedelta(days=7)
    return out


# --------------------------------------------------------------------------- #
# Звіт
# --------------------------------------------------------------------------- #


def plural(count: int, one: str, few: str, many: str) -> str:
    tail = abs(count) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def hours(value: float | None) -> str:
    if value is None:
        return "немає даних"
    if value < 1:
        return f"{round(value * 60)} хв"
    if value < 48:
        return f"{value:.1f} год"
    return f"{value / 24:.1f} доби"


def row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def summary_rows(result: dict) -> list[str]:
    frequency = (
        f"{result['frequency']:.2f} на тиждень ({result['releases']} "
        f"{plural(result['releases'], 'реліз', 'релізи', 'релізів')} "
        f"за {result['weeks']:.1f} тижня)"
        if result["frequency"] is not None
        else "немає даних"
    )
    lead = (
        f"{hours(result['lead_hours'])} (медіана за {result['pulls']} pull request)"
        if result["lead_hours"] is not None
        else "немає даних"
    )
    fail = (
        f"{result['fail_rate']:.1f}% ({result['fixes']} "
        f"{plural(result['fixes'], 'виправлення', 'виправлення', 'виправлень')} "
        f"з {result['changes']} {plural(result['changes'], 'зміни', 'змін', 'змін')})"
        if result["fail_rate"] is not None
        else "немає даних"
    )
    recovery = (
        f"{hours(result['recovery_hours'])} (медіана за {result['recovery_cases']} "
        f"{plural(result['recovery_cases'], 'випадком', 'випадками', 'випадками')})"
        if result["recovery_hours"] is not None
        else "немає даних"
    )
    return [
        row(["Частота поставки", frequency]),
        row(["Час проходження зміни", lead]),
        row(["Частка невдалих змін", fail]),
        row(["Час відновлення", recovery]),
    ]


def weekly_rows(data: dict, since: datetime, until: datetime) -> list[str]:
    out = []
    for start in weeks_between(since, until):
        finish = start + timedelta(days=7)
        part = measure(data, max(start, since), min(finish, until))
        out.append(
            row(
                [
                    start.strftime("%Y-%m-%d"),
                    str(part["changes"]),
                    str(part["fixes"]),
                    str(part["releases"]),
                    str(part["pulls"]),
                    hours(part["lead_hours"]),
                ]
            )
        )
    return out


def split_rows(data: dict, since: datetime, until: datetime, split: datetime) -> list[str]:
    out = []
    for label, left, right in (
        (f"до {split.strftime('%Y-%m-%d')}", since, split),
        (f"від {split.strftime('%Y-%m-%d')}", split, until),
    ):
        part = measure(data, left, right)
        out.append(
            row(
                [
                    label,
                    str(part["changes"]),
                    str(part["fixes"]),
                    str(part["releases"]),
                    hours(part["lead_hours"]),
                ]
            )
        )
    return out


def command_line(since: datetime, until: datetime, split: datetime | None) -> str:
    parts = ["python3 tools/dora.py", f"--since {stamp(since)}", f"--until {stamp(until)}"]
    if split is not None:
        parts.append(f"--split {stamp(split)}")
    return " ".join(parts)


def report(data: dict, since: datetime, until: datetime, split: datetime | None) -> str:
    result = measure(data, since, until)
    lines = [
        f"# Метрики репозиторію {data['slug'] or data['path'].name}",
        "",
        f"Вікно: {stamp(since)} ... {stamp(until)}, це {result['weeks']:.1f} тижня. Час усюди UTC.",
        "",
        f"Команда для повторення: `{command_line(since, until, split)}`",
        "",
        "## Зведення",
        "",
        row(["Метрика", "Значення"]),
        row(["---", "---"]),
        *summary_rows(result),
        "",
        "## По тижнях",
        "",
        row(["Тиждень", "Змін", "Виправлень", "Релізів", "PR злито", "Медіана lead time"]),
        row(["---", "--:", "--:", "--:", "--:", "--:"]),
        *weekly_rows(data, since, until),
    ]
    if split is not None:
        lines += [
            "",
            f"## До і після {split.strftime('%Y-%m-%d')}",
            "",
            row(["Вікно", "Змін", "Виправлень", "Релізів", "Медіана lead time"]),
            row(["---", "--:", "--:", "--:", "--:"]),
            *split_rows(data, since, until, split),
        ]
    lines += [
        "",
        "## Звідки числа",
        "",
        "Частота поставки: теги вигляду `vX.Y.Z` у вікні, поділені на кількість "
        f"тижнів вікна. У цьому вікні: {', '.join(result['release_names']) or 'тегів немає'}.",
        "",
        "Час проходження зміни: медіана часу від відкриття pull request до його "
        "злиття. Часу до відкриття pull request тут немає: після squash merge "
        "авторська дата комітів гілки дорівнює даті злиття, тому з історії його "
        "не дістати.",
        "",
        "Частка невдалих змін: коміти головної гілки, назва яких починається з "
        '`fix:` або `revert:`, плюс відкати виду `Revert "..."`, поділені на всі '
        "коміти головної гілки у вікні.",
        "",
        "Час відновлення: час від зміни до її виправлення. Для відкату пара точна, "
        "бо git пише в коміт `This reverts commit`. Для `fix:` пара це найсвіжіша "
        "попередня зміна, яка чіпала хоч один спільний файл.",
    ]
    if data["pulls_source"] != "api":
        lines += [
            "",
            f"Дані pull request не прочитані: {data['pulls_source']}. Тому час "
            "проходження зміни в цьому звіті порожній.",
        ]
    return "\n".join(lines) + "\n"


def payload(data: dict, since: datetime, until: datetime, split: datetime | None) -> dict:
    result = measure(data, since, until)
    out = {
        "repo": data["slug"] or data["path"].name,
        "since": stamp(since),
        "until": stamp(until),
        "split": stamp(split) if split else None,
        "command": command_line(since, until, split),
        "pulls_source": data["pulls_source"],
        "weeks": round(result["weeks"], 2),
        "changes": result["changes"],
        "fixes": result["fixes"],
        "releases": result["releases"],
        "pulls": result["pulls"],
        "summary_rows": summary_rows(result),
        "weekly_rows": weekly_rows(data, since, until),
        "split_rows": split_rows(data, since, until, split) if split else [],
    }
    return out


# --------------------------------------------------------------------------- #
# Запуск
# --------------------------------------------------------------------------- #


def collect(path: Path) -> dict:
    repo = path.resolve()
    if not (repo / ".git").exists():
        raise Fail(f"у {repo} немає репозиторію git")
    ref = main_ref(repo)
    name = slug(repo)
    merged, source = pulls(name)
    return {
        "path": repo,
        "slug": name,
        "commits": commits(repo, ref),
        "releases": releases(repo),
        "pulls": merged,
        "pulls_source": source,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Чотири ключові метрики DORA за історією репозиторію"
    )
    parser.add_argument("--repo", default=".", help="каталог репозиторію")
    parser.add_argument("--since", help="початок вікна, YYYY-MM-DD (за замовчуванням перший коміт)")
    parser.add_argument("--until", help="кінець вікна, YYYY-MM-DD (за замовчуванням зараз)")
    parser.add_argument("--split", help="дата, на якій розділити вікно для порівняння")
    parser.add_argument("--json", action="store_true", help="машинний формат замість звіту")
    args = parser.parse_args()

    try:
        data = collect(Path(args.repo))
        if not data["commits"]:
            raise Fail("у головній гілці немає жодного коміту")
        since = moment(args.since) if args.since else data["commits"][0]["landed"]
        until = moment(args.until) if args.until else datetime.now(UTC_ZONE)
        split = moment(args.split) if args.split else None
        if until <= since:
            raise Fail("кінець вікна не пізніший за початок")
        if split is not None and not (since < split < until):
            raise Fail("дата --split має лежати всередині вікна")
    except Fail as error:
        print(f"dora.py: {error}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload(data, since, until, split), ensure_ascii=False, indent=2))
    else:
        print(report(data, since, until, split), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
