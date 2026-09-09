#!/usr/bin/env python3
"""Валідатор репозиторію курсу «Інженерна культура та інструменти командної розробки».

Читає репозиторій і каже, що в ньому вже відповідає вимогам поточної роботи, а
що ні. Той самий скрипт ганяє пайплайн у репозиторії студента і викладач при
перевірці, тому прихованих правил тут немає: усе, що бачить викладач, бачить і
студент.

Рівні:

    OK      вимога виконана
    FAIL    вимога не виконана, блок рубрики під загрозою
    WARN    зауваження, бал за нього не знімається
    LATER   артефакт майбутньої роботи, на цьому етапі його відсутність нормальна
    SKIP    перевірити не вдалося, причина в рядку

Запуск:

    python tools/check.py                 поточна робота з tools/course.json
    python tools/check.py --lr 1          конкретна робота
    python tools/check.py --repo ../inshe  інший каталог
    python tools/check.py --summary out.md звіт у файл, для GitHub Actions

Залежностей немає навмисно: тільки стандартна бібліотека, щоб скрипт запускався
там, де більше нічого не поставлено. Перевірки, які потребують GitHub API,
використовують `gh`, і якщо його немає або він не авторизований, вони дають
SKIP, а не падають.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

OK = "OK"
FAIL = "FAIL"
WARN = "WARN"
LATER = "LATER"
SKIP = "SKIP"

TEMPLATE_MARKER = "Шаблон репозиторію курсу «Інженерна культура"

FUTURE_ARTIFACTS = {
    "CHANGELOG.md": "ЛР6",
    "SECURITY.md": "ЛР7",
    "AGENTS.md": "ЛР11",
    "docs/adr": "ЛР3",
    "docs/runbook.md": "ЛР8",
    "docs/slo.md": "ЛР9",
    "docs/postmortems": "ЛР10",
}

REAL_DATA_PATTERNS = [
    (r"\boa\.edu\.ua\b", "домен академії"),
    (r"\boa\.ukr\.education\b", "домен академії"),
    (r"@oa\.", "пошта академії"),
    (r"\+380\d{9}", "український номер телефону"),
    (r"\b0(50|63|66|67|68|73|93|95|96|97|98|99)\d{7}\b", "український номер телефону"),
    (r"(наказ|розказ|розклад)\D{0,40}\d{2}\.\d{2}\.\d{4}", "наказ або розклад з реальною датою"),
]

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
TEXT_SUFFIXES = {
    ".py",
    ".md",
    ".txt",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".cfg",
    ".ini",
    ".env",
    ".html",
    ".csv",
}


@dataclass
class Result:
    block: str
    code: str
    level: str
    message: str


class Repo:
    """Доступ до репозиторію: файли, git і GitHub. Усе, що може не спрацювати,
    повертає None замість того, щоб валити перевірку."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._gh_cache: dict[str, object] = {}

    def run(self, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
        return subprocess.run(
            args,
            cwd=self.path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def read(self, relative: str) -> str | None:
        target = self.path / relative
        if not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")

    def exists(self, relative: str) -> bool:
        return (self.path / relative).exists()

    def tracked_files(self) -> list[str]:
        done = self.run(["git", "ls-files"])
        if done.returncode != 0:
            return []
        return [line for line in done.stdout.splitlines() if line]

    def slug(self) -> str | None:
        """owner/repo з remote origin."""
        done = self.run(["git", "remote", "get-url", "origin"])
        if done.returncode != 0:
            return None
        match = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", done.stdout.strip())
        if not match:
            return None
        return f"{match.group(1)}/{match.group(2)}"

    def gh_available(self) -> bool:
        try:
            done = self.run(["gh", "auth", "status"], timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        if done.returncode == 0:
            return True
        return bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))

    def gh_json(self, endpoint: str):
        if endpoint in self._gh_cache:
            return self._gh_cache[endpoint]
        try:
            done = self.run(["gh", "api", endpoint], timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0:
            return None
        try:
            value = json.loads(done.stdout)
        except json.JSONDecodeError:
            return None
        self._gh_cache[endpoint] = value
        return value

    def text_files(self):
        for item in self.path.rglob("*"):
            if not item.is_file():
                continue
            if SKIP_DIRS & set(item.relative_to(self.path).parts):
                continue
            if item.suffix.lower() not in TEXT_SUFFIXES and item.name not in {
                "Makefile",
                "Dockerfile",
            }:
                continue
            yield item


def section_items(text: str, heading_pattern: str) -> list[str] | None:
    """Елементи списку в розділі, знайденому за заголовком. None, якщо розділу немає."""
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") and re.search(heading_pattern, line, re.IGNORECASE):
            start = index + 1
            break
    if start is None:
        return None
    items = []
    for line in lines[start:]:
        if line.lstrip().startswith("#"):
            break
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "+ ")) or re.match(r"^\d+\.\s", stripped):
            items.append(stripped)
    return items


# --------------------------------------------------------------------------- #
# Блок A. Артефакти
# --------------------------------------------------------------------------- #


def check_a(repo: Repo) -> list[Result]:
    out: list[Result] = []
    slug = repo.slug()

    if not repo.gh_available() or slug is None:
        out.append(Result("A", "A1", SKIP, "публічність не перевірена: немає gh або remote origin"))
        out.append(Result("A", "A2", SKIP, "походження зі шаблону не перевірене"))
    else:
        info = repo.gh_json(f"repos/{slug}")
        if info is None:
            out.append(Result("A", "A1", SKIP, f"GitHub API не відповів по {slug}"))
            out.append(Result("A", "A2", SKIP, "походження зі шаблону не перевірене"))
        else:
            if info.get("visibility") == "public":
                out.append(Result("A", "A1", OK, "репозиторій публічний"))
            else:
                out.append(
                    Result(
                        "A",
                        "A1",
                        FAIL,
                        "репозиторій не публічний, Actions і Pages не працюватимуть",
                    )
                )

            template = (info.get("template_repository") or {}).get("full_name")
            expected = course_config().get("template_repo")
            if template and expected and template.lower() == expected.lower():
                out.append(Result("A", "A2", OK, "створений зі шаблону курсу"))
            else:
                out.append(
                    Result("A", "A2", WARN, "не видно, що репозиторій створений зі шаблону курсу")
                )

    readme = repo.read("README.md")
    if readme is None:
        out.append(Result("A", "A3", FAIL, "README.md не знайдено"))
        out.append(Result("A", "A4", FAIL, "README.md не знайдено"))
    else:
        if TEMPLATE_MARKER in readme:
            out.append(
                Result(
                    "A", "A3", FAIL, "README.md досі шаблонний, його треба переписати під свою тему"
                )
            )
        elif len(readme.encode("utf-8")) < 800:
            out.append(
                Result(
                    "A",
                    "A3",
                    FAIL,
                    f"README.md закороткий: {len(readme.encode('utf-8'))} байтів із 800",
                )
            )
        else:
            out.append(Result("A", "A3", OK, "README.md переписаний під свій продукт"))

        missing = [need for need in ("make run", "make test", ".env") if need not in readme]
        if missing:
            out.append(Result("A", "A4", FAIL, f"у README.md немає згадки: {', '.join(missing)}"))
        else:
            out.append(Result("A", "A4", OK, "README.md описує запуск, тести і змінні оточення"))

    contributing = repo.read("CONTRIBUTING.md")
    if contributing is None:
        out.append(Result("A", "A5", FAIL, "CONTRIBUTING.md не знайдено"))
        out.append(Result("A", "A6", FAIL, "CONTRIBUTING.md не знайдено"))
    else:
        todos = len(re.findall(r"\bTODO\b", contributing, re.IGNORECASE))
        if todos:
            out.append(
                Result("A", "A5", FAIL, f"у CONTRIBUTING.md лишилось позначок TODO: {todos}")
            )
        else:
            out.append(Result("A", "A5", OK, "CONTRIBUTING.md заповнений, TODO немає"))

        items = section_items(contributing, r"definition of done")
        if items is None:
            out.append(
                Result("A", "A6", FAIL, "у CONTRIBUTING.md немає розділу Definition of Done")
            )
        elif len(items) < 4:
            out.append(
                Result(
                    "A",
                    "A6",
                    FAIL,
                    f"у Definition of Done {len(items)} пунктів, потрібно щонайменше 4",
                )
            )
        else:
            out.append(Result("A", "A6", OK, f"у Definition of Done пунктів: {len(items)}"))

    env_example = repo.read(".env.example")
    if env_example is None:
        out.append(Result("A", "A7", FAIL, ".env.example не знайдено"))
    else:
        keys, filled = [], []
        for line in env_example.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            keys.append(name.strip())
            if value.strip():
                filled.append(name.strip())
        if filled:
            out.append(
                Result(
                    "A",
                    "A7",
                    FAIL,
                    f"у .env.example є значення, а має бути тільки ключі: {', '.join(filled)}",
                )
            )
        elif len(keys) < 3:
            out.append(
                Result("A", "A7", FAIL, f"у .env.example {len(keys)} ключів, потрібно щонайменше 3")
            )
        else:
            out.append(Result("A", "A7", OK, f"у .env.example ключів без значень: {len(keys)}"))

    tracked = repo.tracked_files()
    if not tracked:
        out.append(Result("A", "A8", SKIP, "git ls-files нічого не повернув"))
    elif ".env" in tracked:
        out.append(Result("A", "A8", FAIL, ".env потрапив у git, це витік конфігурації"))
    else:
        out.append(Result("A", "A8", OK, ".env у git не відстежується"))

    gitignore = repo.read(".gitignore") or ""
    if re.search(r"^\s*\.env\s*$", gitignore, re.MULTILINE):
        out.append(Result("A", "A9", OK, ".gitignore містить .env"))
    else:
        out.append(Result("A", "A9", FAIL, "у .gitignore немає рядка .env"))

    findings = []
    for item in repo.text_files():
        content = item.read_text(encoding="utf-8", errors="replace")
        for pattern, label in REAL_DATA_PATTERNS:
            if re.search(pattern, content, re.IGNORECASE):
                findings.append(f"{item.relative_to(repo.path)}: {label}")
                break
    if findings:
        out.append(Result("A", "A10", FAIL, "схоже на реальні дані: " + "; ".join(findings[:5])))
    else:
        out.append(Result("A", "A10", OK, "ознак реальних даних не знайдено"))

    for name, work in FUTURE_ARTIFACTS.items():
        if not repo.exists(name):
            out.append(Result("A", "A11", LATER, f"{name} з'явиться на {work}"))

    return out


# --------------------------------------------------------------------------- #
# Блок B. Воно працює
# --------------------------------------------------------------------------- #


def check_b(repo: Repo, run_slow: bool) -> list[Result]:
    out: list[Result] = []

    if not run_slow:
        out.append(Result("B", "B1", SKIP, "make test не запускався, додайте --slow"))
        out.append(Result("B", "B2", SKIP, "make lint не запускався, додайте --slow"))
        out.append(Result("B", "B3", SKIP, "сервіс не піднімався, додайте --slow"))
        out.append(Result("B", "B4", LATER, "make build запрацює після ЛР6"))
        return out

    for code, target in (("B1", "test"), ("B2", "lint")):
        try:
            done = repo.run(["make", target], timeout=900)
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            out.append(Result("B", code, FAIL, f"make {target} не вдалося запустити: {error}"))
            continue
        if done.returncode == 0:
            out.append(Result("B", code, OK, f"make {target} зелений"))
        else:
            tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
            out.append(Result("B", code, FAIL, f"make {target} червоний: " + " / ".join(tail)))

    out.append(health_check(repo))
    out.append(Result("B", "B4", LATER, "make build запрацює після ЛР6"))
    return out


def health_check(repo: Repo) -> Result:
    env = dict(os.environ, APP_HOST="127.0.0.1", APP_PORT="8099")
    python = repo.path / ".venv" / "bin" / "python"
    interpreter = str(python) if python.exists() else sys.executable
    process = subprocess.Popen(
        [interpreter, "-m", "app"],
        cwd=repo.path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for _ in range(30):
            time.sleep(1)
            try:
                with urllib.request.urlopen("http://127.0.0.1:8099/health", timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if payload.get("status") == "ok":
                    return Result("B", "B3", OK, "сервіс піднявся, /health віддає ok")
                return Result("B", "B3", FAIL, f"/health відповів, але не ok: {payload}")
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
                continue
        return Result("B", "B3", FAIL, "сервіс не відповів на /health за 30 секунд")
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


# --------------------------------------------------------------------------- #
# Блок C. Слід процесу
# --------------------------------------------------------------------------- #


def check_c(repo: Repo) -> list[Result]:
    slug = repo.slug()
    if not repo.gh_available() or slug is None:
        return [
            Result("C", code, SKIP, "потрібен gh і remote origin")
            for code in ("C1", "C2", "C3", "C4", "C5")
        ]

    owner = slug.split("/")[0]
    out: list[Result] = []

    raw_issues = repo.gh_json(f"repos/{slug}/issues?state=all&per_page=100")
    if raw_issues is None:
        return [
            Result("C", code, SKIP, "GitHub API не віддав issue")
            for code in ("C1", "C2", "C3", "C4", "C5")
        ]

    # Рахуються заведені студентом issue незалежно від стану: на ЛР1 одна з
    # шести закривається через PR, а на ЛР2 закриваються ще дві. Якби правило
    # дивилось лише на відкриті, воно карало б саме тих, хто зробив роботу.
    issues = [
        item
        for item in raw_issues
        if "pull_request" not in item and (item.get("user") or {}).get("login") == owner
    ]

    if len(issues) >= 6:
        out.append(Result("C", "C1", OK, f"заведених власних issue: {len(issues)}"))
    else:
        out.append(Result("C", "C1", FAIL, f"заведених власних issue {len(issues)}, потрібно 6"))

    with_criteria = sum(
        1
        for item in issues
        if len(re.findall(r"^\s*- \[[ xX]\]", item.get("body") or "", re.MULTILINE)) >= 3
    )
    if with_criteria >= 6:
        out.append(Result("C", "C2", OK, f"issue з критеріями приймання: {with_criteria}"))
    elif with_criteria >= 4:
        out.append(Result("C", "C2", WARN, f"issue з критеріями приймання {with_criteria} із 6"))
    else:
        out.append(
            Result(
                "C",
                "C2",
                FAIL,
                f"issue з критеріями приймання {with_criteria}, потрібно щонайменше 4 із 6",
            )
        )

    pulls = repo.gh_json(f"repos/{slug}/pulls?state=closed&per_page=100") or []
    merged = [item for item in pulls if item.get("merged_at")]

    if merged:
        out.append(Result("C", "C3", OK, f"змержених pull request: {len(merged)}"))
    else:
        out.append(Result("C", "C3", FAIL, "змержених pull request немає"))
        out.append(Result("C", "C4", FAIL, "немає PR, який можна перевірити"))
        out.append(Result("C", "C5", FAIL, "немає PR, через який закривалась би issue"))
        return out

    good = []
    for item in merged:
        branch = (item.get("head") or {}).get("ref", "")
        body = item.get("body") or ""
        if branch and branch != "main" and re.search(r"#\d+|/issues/\d+", body):
            good.append(item)
    if good:
        out.append(Result("C", "C4", OK, "є PR з окремої гілки з посиланням на issue"))
    else:
        out.append(
            Result(
                "C", "C4", FAIL, "жоден змержений PR не йде з окремої гілки з посиланням на issue"
            )
        )

    closed_own = [item for item in issues if item.get("state") == "closed"]
    if closed_own:
        out.append(Result("C", "C5", OK, f"закритих власних issue: {len(closed_own)}"))
    else:
        out.append(Result("C", "C5", FAIL, "жодна власна issue не закрита"))

    if any(re.search(r"\bAI\b", item.get("body") or "", re.IGNORECASE) for item in merged):
        out.append(Result("C", "C6", OK, "в описі PR є рядок про AI"))
    else:
        out.append(Result("C", "C6", WARN, "в описі PR немає рядка про AI"))

    return out


# --------------------------------------------------------------------------- #
# Складання
# --------------------------------------------------------------------------- #

RAW_BASE = (
    "https://raw.githubusercontent.com/"
    "LyahovchukSergiy/engineering-culture-2026-template/main/tools"
)

_data_cache: dict[str, dict | None] = {}


def course_file(name: str) -> dict | None:
    """Дані курсу: спершу файл поруч зі скриптом, потім свіжа копія з репозиторію
    курсу. Другий шлях потрібен тому, що workflow у репозиторії студента створений
    на ЛР1 і тягне лише два файли. Усе, що курс додасть пізніше, доїжджає сюди, а
    не через правку чужих репозиторіїв. Мережі може не бути, тоді None."""
    if name in _data_cache:
        return _data_cache[name]
    value = None
    local = Path(__file__).with_name(name)
    try:
        value = json.loads(local.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            with urllib.request.urlopen(f"{RAW_BASE}/{name}", timeout=15) as response:
                value = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError, OSError):
            value = None
    _data_cache[name] = value
    return value


def course_config() -> dict:
    return course_file("course.json") or {"current_lr": 1}


def rotation_entry(login: str) -> tuple[dict | None, dict | None]:
    """Рядок таблиці ротації для власника репозиторію: кого він рев'ює на ЛР3,
    чий onboarding проходить на ЛР8, чий портфель дивиться на ЛР13."""
    table = course_file("rotation.json")
    if not table:
        return None, None
    for item in table.get("students", []):
        if item.get("github", "").lower() == login.lower():
            return item, table
    return None, table


def prefixed(results: list[Result], tag: str) -> list[Result]:
    """Позначає, з якої роботи правило, коли перевірка йде накопичувально."""
    return [Result(item.block, f"{tag}.{item.code}", item.level, item.message) for item in results]


def run_lr1(repo: Repo, run_slow: bool) -> list[Result]:
    return check_a(repo) + check_b(repo, run_slow) + check_c(repo)


# --------------------------------------------------------------------------- #
# ЛР2. Шлях задачі
# --------------------------------------------------------------------------- #

CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]+\))?!?: .+"
)

COURSE_TESTS = ("tests/test_filter.py", "tests/test_sort.py")


def branch_rule_types(repo: Repo, slug: str) -> tuple[set[str], dict] | None:
    """Активні правила захисту main і параметри правила pull_request.

    Читає і рулсети, і класичний захист. Ендпойнт rules/branches доступний з
    правами на читання, тому працює і в пайплайні студента, де токен раннера
    прав адміністратора не має.
    """
    types: set[str] = set()
    pr_params: dict = {}
    seen = False

    rules = repo.gh_json(f"repos/{slug}/rules/branches/main")
    if isinstance(rules, list):
        seen = True
        for rule in rules:
            if isinstance(rule, dict) and rule.get("type"):
                types.add(rule["type"])
                if rule["type"] == "pull_request":
                    pr_params = rule.get("parameters") or {}

    classic = repo.gh_json(f"repos/{slug}/branches/main/protection")
    if isinstance(classic, dict):
        seen = True
        if classic.get("required_pull_request_reviews") is not None:
            types.add("pull_request")
        if (classic.get("required_linear_history") or {}).get("enabled"):
            types.add("required_linear_history")
        if not (classic.get("allow_force_pushes") or {}).get("enabled", False):
            types.add("non_fast_forward")

    return (types, pr_params) if seen else None


def check_lr2_files(repo: Repo) -> list[Result]:
    out: list[Result] = []

    template = repo.read(".github/PULL_REQUEST_TEMPLATE.md")
    if template is None:
        out.append(Result("A", "A1", FAIL, ".github/PULL_REQUEST_TEMPLATE.md не знайдено"))
        out.append(Result("A", "A2", FAIL, "шаблон PR відсутній, поля про AI немає"))
    else:
        out.append(Result("A", "A1", OK, "шаблон pull request на місці"))
        if re.search(r"\bAI\b", template, re.IGNORECASE):
            out.append(Result("A", "A2", OK, "у шаблоні PR є рядок про AI"))
        else:
            out.append(Result("A", "A2", FAIL, "у шаблоні PR немає рядка про AI"))

    missing = [name for name in COURSE_TESTS if not repo.exists(name)]
    if missing:
        out.append(Result("B", "B1", FAIL, "немає тестів виклику: " + ", ".join(missing)))
    else:
        out.append(Result("B", "B1", OK, "обидва тести виклику на місці"))

    markers = []
    for item in repo.text_files():
        content = item.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^<{7} |^={7}$|^>{7} ", content, re.MULTILINE):
            markers.append(str(item.relative_to(repo.path)))
    if markers:
        out.append(
            Result("B", "B2", FAIL, "маркери конфлікту лишились у: " + ", ".join(markers[:5]))
        )
    else:
        out.append(Result("B", "B2", OK, "маркерів конфлікту в коді немає"))

    return out


def main_ref(repo: Repo) -> str | None:
    """Посилання на головну гілку.

    У пайплайні на подію pull_request робоче дерево це штучний merge-коміт, і
    рахувати історію по ньому не можна: там завжди буде merge. Тому спершу
    беремо віддалений main, і лише якщо його немає, локальний.
    """
    for candidate in ("refs/remotes/origin/main", "refs/heads/main"):
        done = repo.run(["git", "rev-parse", "--verify", "--quiet", candidate])
        if done.returncode == 0:
            return candidate
    return None


def check_lr2_process(repo: Repo) -> list[Result]:
    slug = repo.slug()
    codes = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")
    if not repo.gh_available() or slug is None:
        return [Result("C", code, SKIP, "потрібен gh і remote origin") for code in codes]

    out: list[Result] = []

    found = branch_rule_types(repo, slug)
    if found is None:
        note = "не вдалося прочитати захист main"
        out.extend(Result("C", code, SKIP, note) for code in ("C1", "C2", "C3", "C7"))
    else:
        types, pr_params = found
        checks = (
            ("C1", "pull_request", "злиття в main тільки через pull request"),
            ("C2", "required_linear_history", "лінійна історія увімкнена"),
            ("C3", "non_fast_forward", "force push у main заборонений"),
        )
        for code, needed, message in checks:
            if needed in types:
                out.append(Result("C", code, OK, message))
            else:
                out.append(Result("C", code, FAIL, "не увімкнено: " + message))

        # Підпункт, який GitHub вмикає за замовчуванням. Він вимагає окремого
        # апрува, коли в pull request є коміти чужого авторства, а виклики курсу
        # приходять саме такими. У соло-репозиторії апрувати нікому.
        if pr_params.get("require_extra_approval_for_unattributed_changes"):
            out.append(
                Result(
                    "C",
                    "C7",
                    WARN,
                    "у правилі pull request увімкнено додатковий апрув для комітів "
                    "чужого авторства: зміни курсу через cherry-pick можуть не злитись",
                )
            )
        else:
            out.append(Result("C", "C7", OK, "додаткового апрува для чужих комітів не вимагається"))

    ref = main_ref(repo)
    if ref is None:
        out.extend(
            Result("C", code, SKIP, "гілка main недоступна локально") for code in ("C4", "C5", "C6")
        )
        return out

    merges = repo.run(["git", "rev-list", "--merges", "--count", ref])
    if merges.returncode == 0:
        count = int(merges.stdout.strip() or 0)
        if count == 0:
            out.append(Result("C", "C4", OK, "merge-комітів у main немає, історія лінійна"))
        else:
            out.append(
                Result("C", "C4", FAIL, f"у main merge-комітів: {count}, історія не лінійна")
            )
    else:
        out.append(Result("C", "C4", SKIP, "не вдалося прочитати історію main"))

    log = repo.run(["git", "log", "--format=%s", "-30", ref])
    subjects = [line for line in log.stdout.splitlines() if line.strip()]
    body = subjects[:-1] if len(subjects) > 1 else subjects
    bad = [line for line in body if not CONVENTIONAL.match(line)]
    if not body:
        out.append(Result("C", "C5", SKIP, "історія main порожня"))
    elif len(bad) <= 2:
        out.append(
            Result("C", "C5", OK, f"Conventional Commits: {len(body) - len(bad)} з {len(body)}")
        )
    else:
        out.append(Result("C", "C5", FAIL, "не за Conventional Commits: " + "; ".join(bad[:3])))

    if any(re.search(r"revert", line, re.IGNORECASE) for line in subjects):
        out.append(Result("C", "C6", OK, "у main є коміт відкату"))
    else:
        out.append(Result("C", "C6", FAIL, "у main немає коміта, у назві якого є слово revert"))

    return out


# --------------------------------------------------------------------------- #
# ЛР3. Стандарти коду, рефакторинг і рев'ю
# --------------------------------------------------------------------------- #

ADR_SECTIONS = ("Статус", "Контекст", "Рішення", "Наслідки")
PRICING_TESTS = "tests/legacy/test_pricing.py"
PRICING_EXPECTED = ("200.0", "1020.0", "535.0", "450.0")
REVIEW_TAG = re.compile(r"^\s*(blocker|suggestion|nit)\s*:", re.IGNORECASE)
PR_LINK = re.compile(r"https://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")


def codeowners(repo: Repo) -> str | None:
    for place in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        text = repo.read(place)
        if text is not None:
            return text
    return None


def adr_files(repo: Repo) -> list[str]:
    return sorted(
        name
        for name in repo.tracked_files()
        if re.fullmatch(r"docs/adr/\d{4}-[^/]+\.md", name)
    )


def check_lr3_files(repo: Repo) -> list[Result]:
    out: list[Result] = []

    hooks = repo.read(".pre-commit-config.yaml")
    if hooks is None:
        out.append(Result("A", "A1", FAIL, ".pre-commit-config.yaml не знайдено"))
        out.append(Result("A", "A2", FAIL, "без конфігурації pre-commit хука форматера немає"))
    else:
        out.append(Result("A", "A1", OK, "конфігурація pre-commit на місці"))
        if re.search(r"ruff-format|black|format", hooks, re.IGNORECASE):
            out.append(Result("A", "A2", OK, "у pre-commit є хук форматера"))
        else:
            out.append(Result("A", "A2", FAIL, "у pre-commit немає хука форматера"))

    owners = codeowners(repo)
    if owners is None:
        out.append(Result("A", "A3", FAIL, "CODEOWNERS не знайдено ні в .github/, ні в корені"))
    elif "@" in owners:
        out.append(Result("A", "A3", OK, "CODEOWNERS на місці і називає власника"))
    else:
        out.append(Result("A", "A3", FAIL, "у CODEOWNERS немає жодного @власника"))

    records = adr_files(repo)
    if len(records) < 2:
        out.append(
            Result("A", "A4", FAIL, f"ADR у docs/adr знайдено {len(records)}, потрібно два")
        )
        out.append(Result("A", "A5", FAIL, "немає двох ADR, розділи перевіряти нема в чому"))
        out.append(
            Result("A", "A6", FAIL, "немає двох ADR, посилання на PR не перевірити")
        )
    else:
        out.append(Result("A", "A4", OK, f"ADR знайдено: {len(records)}"))
        broken = []
        without_pr = []
        for name in records:
            text = repo.read(name) or ""
            missing_sections = [
                section
                for section in ADR_SECTIONS
                if not re.search(rf"^#+\s*{section}", text, re.MULTILINE)
            ]
            if missing_sections:
                broken.append(name)
            if not PR_LINK.search(text):
                without_pr.append(name)
        if broken:
            out.append(
                Result("A", "A5", FAIL, "немає всіх чотирьох розділів у: " + ", ".join(broken))
            )
        else:
            out.append(Result("A", "A5", OK, "у кожному ADR чотири розділи"))
        if without_pr:
            out.append(
                Result(
                    "A",
                    "A6",
                    FAIL,
                    "немає посилання на pull request у: " + ", ".join(without_pr),
                )
            )
        else:
            out.append(Result("A", "A6", OK, "кожен ADR посилається на pull request"))

    log = repo.read("docs/review-log.md")
    if log is None:
        out.append(Result("A", "A7", FAIL, "docs/review-log.md не знайдено"))
    else:
        slug = repo.slug() or "/"
        owner = slug.split("/")[0].lower()
        foreign = {
            (found[0], found[1], found[2])
            for found in PR_LINK.findall(log)
            if found[0].lower() != owner
        }
        if len(foreign) >= 2:
            out.append(Result("A", "A7", OK, f"у журналі рев'ю чужих pull request: {len(foreign)}"))
        else:
            out.append(
                Result(
                    "A",
                    "A7",
                    FAIL,
                    f"у журналі рев'ю посилань на чужі pull request {len(foreign)}, потрібно два",
                )
            )

    tests = repo.read(PRICING_TESTS)
    if tests is None:
        out.append(Result("B", "B1", FAIL, f"{PRICING_TESTS} зник, а його правити не можна"))
    else:
        lost = [value for value in PRICING_EXPECTED if value not in tests]
        if lost:
            out.append(
                Result("B", "B1", FAIL, "у тестах курсу змінені очікування: " + ", ".join(lost))
            )
        else:
            out.append(Result("B", "B1", OK, "тести курсу до pricing.py не змінені"))

    out.append(pricing_refactored(repo))

    makefile = repo.read("Makefile") or ""
    lint_target = re.search(r"^lint:.*?(?=^\w|\Z)", makefile, re.MULTILINE | re.DOTALL)
    if lint_target and "format" in lint_target.group(0):
        out.append(Result("B", "B3", OK, "make lint перевіряє і форматування"))
    else:
        out.append(Result("B", "B3", FAIL, "у цілі lint немає перевірки форматування"))

    return out


def pricing_refactored(repo: Repo) -> Result:
    """Чи це справді рефакторинг, а не прогін форматера.

    Одного коміта мало: на кроці 1 студент проганяє форматер по всьому
    репозиторію, і файл змінюється сам собою. Тому додатково дивимось на сліди
    роботи, яких вимагає умова: названі числа або розбиття на функції.
    """
    touched = repo.run(["git", "log", "--oneline", "--", "legacy/pricing.py"])
    commits = len([line for line in touched.stdout.splitlines() if line.strip()])
    if commits < 2:
        return Result("B", "B2", FAIL, "legacy/pricing.py лишився таким, як у шаблоні")

    source = repo.read("legacy/pricing.py") or ""
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return Result("B", "B2", FAIL, f"legacy/pricing.py не парситься: {error}")

    constants = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name) and target.id.isupper()
    ]
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if constants or len(functions) > 1:
        return Result(
            "B",
            "B2",
            OK,
            f"рефакторинг видно: названих констант {len(constants)}, функцій {len(functions)}",
        )
    return Result(
        "B",
        "B2",
        FAIL,
        "у legacy/pricing.py немає ні названих чисел, ні поділу на функції: "
        "схоже, файл лише переформатований",
    )


def refactor_pull_request(repo: Repo, slug: str) -> dict | None:
    """Pull request, яким рефакторинг приїхав у main."""
    log = repo.run(["git", "log", "-1", "--format=%H", "--", "legacy/pricing.py"])
    sha = log.stdout.strip()
    if not sha:
        return None
    found = repo.gh_json(f"repos/{slug}/commits/{sha}/pulls")
    if not isinstance(found, list) or not found:
        return None
    return found[0]


def check_lr3_process(repo: Repo) -> list[Result]:
    slug = repo.slug()
    codes = ("C1", "C2", "C3", "C4")
    if not repo.gh_available() or slug is None:
        return [Result("C", code, SKIP, "потрібен gh і remote origin") for code in codes]

    out: list[Result] = []
    login = slug.split("/")[0]

    pull = refactor_pull_request(repo, slug)
    if pull is None:
        out.append(Result("C", "C1", FAIL, "рефакторинг pricing.py не прийшов через pull request"))
        out.append(Result("C", "C2", SKIP, "немає pull request рефакторингу"))
    else:
        number = pull.get("number")
        out.append(Result("C", "C1", OK, f"рефакторинг прийшов через pull request #{number}"))
        opened = pull.get("created_at") or ""
        merged = pull.get("merged_at") or ""
        comments = repo.gh_json(f"repos/{slug}/pulls/{number}/comments") or []
        outside = [
            item
            for item in comments
            if isinstance(item, dict)
            and (item.get("user") or {}).get("login", "").lower() != login.lower()
        ]
        hours = 0.0
        if opened and merged:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            hours = (
                time.mktime(time.strptime(merged, fmt)) - time.mktime(time.strptime(opened, fmt))
            ) / 3600
        if outside:
            out.append(
                Result(
                    "C",
                    "C2",
                    OK,
                    f"pull request #{number} прочитали інші: коментарів {len(outside)}",
                )
            )
        elif not merged or hours >= 24:
            out.append(
                Result("C", "C2", OK, f"pull request #{number} чекав {hours:.0f} год")
            )
        else:
            out.append(
                Result(
                    "C",
                    "C2",
                    WARN,
                    f"pull request #{number} злитий через {hours:.0f} год і без чужих коментарів: "
                    "рев'ювати його не було коли",
                )
            )

    log = repo.read("docs/review-log.md") or ""
    targets = {found for found in PR_LINK.findall(log) if found[0].lower() != login.lower()}
    if not targets:
        out.append(
            Result("C", "C3", FAIL, "у docs/review-log.md немає чужих pull request")
        )
        out.append(
            Result("C", "C4", SKIP, "немає чужих pull request, теги не перевірити")
        )
        return out

    good = []
    thin = []
    untagged = []
    unreadable = []
    for owner, name, number in sorted(targets):
        comments = repo.gh_json(f"repos/{owner}/{name}/pulls/{number}/comments")
        if comments is None:
            unreadable.append(f"{owner}/{name}#{number}")
            continue
        mine = [
            item
            for item in comments
            if isinstance(item, dict)
            and (item.get("user") or {}).get("login", "").lower() == login.lower()
        ]
        if len(mine) < 3:
            thin.append(f"{owner}/{name}#{number}: коментарів {len(mine)}")
            continue
        good.append(f"{owner}/{name}#{number}")
        if not all(REVIEW_TAG.match(item.get("body") or "") for item in mine):
            untagged.append(f"{owner}/{name}#{number}")

    if unreadable and not good:
        out.append(Result("C", "C3", SKIP, "не вдалося прочитати: " + ", ".join(unreadable)))
        out.append(Result("C", "C4", SKIP, "коментарі недоступні"))
        return out

    if len(good) >= 2:
        out.append(Result("C", "C3", OK, "рев'ю з трьома коментарями і більше: " + ", ".join(good)))
    else:
        note = "; ".join(thin) or "рев'ю не знайдено"
        out.append(
            Result(
                "C",
                "C3",
                FAIL,
                f"повних рев'ю {len(good)} з двох, треба по три коментарі: {note}",
            )
        )

    if untagged:
        out.append(
            Result(
                "C",
                "C4",
                FAIL,
                "коментарі без позначки blocker, suggestion або nit у: " + ", ".join(untagged),
            )
        )
    elif good:
        out.append(Result("C", "C4", OK, "кожен коментар позначений типом"))
    else:
        out.append(Result("C", "C4", SKIP, "немає повних рев'ю, теги перевіряти нема на чому"))

    return out


def run_lr3(repo: Repo, run_slow: bool) -> list[Result]:
    return (
        prefixed(run_lr1(repo, run_slow), "ЛР1")
        + prefixed(check_lr2_files(repo) + check_lr2_process(repo), "ЛР2")
        + prefixed(check_lr3_files(repo) + check_lr3_process(repo), "ЛР3")
    )


def run_lr2(repo: Repo, run_slow: bool) -> list[Result]:
    return prefixed(run_lr1(repo, run_slow), "ЛР1") + prefixed(
        check_lr2_files(repo) + check_lr2_process(repo), "ЛР2"
    )


# --------------------------------------------------------------------------- #
# ЛР4. Тести і CI
# --------------------------------------------------------------------------- #

CI_WORKFLOW = ".github/workflows/ci.yml"
MUTANTS_WORKFLOW = ".github/workflows/mutants.yml"
COVERAGE_ARTIFACT = "coverage-html"
REQUIRED_CONTEXTS = ("lint", "test")

# Тести, які прийшли з курсом. Власними вони не рахуються: ЛР4 просить дописати
# свої, а не залишити те, що вже лежало в шаблоні і у виклику ЛР2.
COURSE_OWNED_TESTS = {
    "tests/legacy/test_pricing.py",
    "tests/test_health.py",
    "tests/test_items.py",
    "tests/test_filter.py",
    "tests/test_sort.py",
}

BADGE = re.compile(r"actions/workflows/[\w.-]+/badge\.svg")
PRIVATE_PRICING = re.compile(r"\bpricing\._\w+")


def own_test_files(repo: Repo) -> list[str]:
    return [
        name
        for name in repo.tracked_files()
        if re.fullmatch(r"tests/(?:[^/]+/)*test_[^/]+\.py", name) and name not in COURSE_OWNED_TESTS
    ]


def test_functions(tree: ast.AST) -> list[ast.FunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def uses_http_client(node: ast.AST) -> bool:
    """Чи б'є тест у ендпойнт. Ознака це виклик методу на об'єкті з ім'ям client."""
    for item in ast.walk(node):
        if isinstance(item, ast.Attribute) and isinstance(item.value, ast.Name):
            if "client" in item.value.id.lower() and item.attr in {
                "get",
                "post",
                "put",
                "patch",
                "delete",
            }:
                return True
    return False


def parsed_tests(repo: Repo) -> list[tuple[str, str, ast.AST]]:
    out = []
    for name in own_test_files(repo):
        source = repo.read(name)
        if source is None:
            continue
        try:
            out.append((name, source, ast.parse(source)))
        except SyntaxError:
            continue
    return out


def check_lr4_tests(repo: Repo) -> list[Result]:
    out: list[Result] = []
    files = parsed_tests(repo)

    pricing_tests = 0
    for _, source, tree in files:
        if "calculate_order_total" not in source:
            continue
        pricing_tests += len(test_functions(tree))
    if pricing_tests >= 6:
        out.append(Result("A", "A1", OK, f"власних тестів до pricing.py: {pricing_tests}"))
    else:
        out.append(
            Result(
                "A",
                "A1",
                FAIL,
                f"власних тестів до pricing.py {pricing_tests}, потрібно щонайменше 6",
            )
        )

    private = []
    for name, source, tree in files:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("pricing"):
                private += [
                    f"{name}: {alias.name}" for alias in node.names if alias.name.startswith("_")
                ]
        if PRIVATE_PRICING.search(source):
            private.append(f"{name}: звернення до приватного імені модуля")
    if private:
        out.append(
            Result(
                "A",
                "A2",
                FAIL,
                "тест звертається не до публічної функції: "
                + "; ".join(private[:3])
                + ". Після підміни мутантом такий тест дасть ImportError",
            )
        )
    else:
        out.append(Result("A", "A2", OK, "тести звертаються до публічної функції модуля"))

    integration = 0
    for _, _, tree in files:
        integration += sum(1 for node in test_functions(tree) if uses_http_client(node))
    if integration >= 2:
        out.append(Result("A", "A3", OK, f"власних інтеграційних тестів: {integration}"))
    else:
        out.append(
            Result("A", "A3", FAIL, f"власних інтеграційних тестів {integration}, потрібно 2")
        )

    return out


def check_lr4_files(repo: Repo) -> list[Result]:
    out: list[Result] = []

    if repo.exists(MUTANTS_WORKFLOW):
        out.append(Result("A", "A4", OK, "mutants.yml на місці"))
    else:
        out.append(Result("A", "A4", FAIL, f"{MUTANTS_WORKFLOW} не знайдено"))

    notes = repo.read("docs/ci.md")
    if notes is None:
        out.append(Result("A", "A5", FAIL, "docs/ci.md не знайдено"))
    else:
        blocks = len(re.findall(r"^#{2,}\s+\S", notes, re.MULTILINE)) or len(
            re.findall(r"^\s*(?:[-*+]|\d+\.)\s+\S", notes, re.MULTILINE)
        )
        if blocks >= 3:
            out.append(Result("A", "A5", OK, f"у docs/ci.md розібрано пунктів: {blocks}"))
        else:
            out.append(
                Result("A", "A5", FAIL, f"у docs/ci.md {blocks} пунктів, а дефектів було три")
            )

    readme = repo.read("README.md") or ""
    if BADGE.search(readme):
        out.append(Result("A", "A6", OK, "у README є бейдж статусу пайплайна"))
    else:
        out.append(Result("A", "A6", FAIL, "у README немає бейджа статусу пайплайна"))

    out.extend(check_lr4_pipeline(repo))
    return out


def yaml_top_blocks(text: str) -> dict[str, str]:
    """Розбиває YAML на блоки верхнього рівня. Повного розбору тут не треба, і
    залежностей у скрипті немає навмисно."""
    blocks: dict[str, str] = {}
    current = None
    lines: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^([A-Za-z_][\w-]*):", line)
        if match:
            if current:
                blocks[current] = "\n".join(lines)
            current = match.group(1)
            lines = [line]
        elif current:
            lines.append(line)
    if current:
        blocks[current] = "\n".join(lines)
    return blocks


def yaml_jobs(text: str) -> dict[str, str]:
    jobs_block = yaml_top_blocks(text).get("jobs", "")
    out: dict[str, str] = {}
    current = None
    lines: list[str] = []
    for line in jobs_block.splitlines()[1:]:
        match = re.match(r"^  ([A-Za-z_][\w-]*):", line)
        if match:
            if current:
                out[current] = "\n".join(lines)
            current = match.group(1)
            lines = [line]
        elif current:
            lines.append(line)
    if current:
        out[current] = "\n".join(lines)
    return out


def check_lr4_pipeline(repo: Repo) -> list[Result]:
    """Три дефекти виклику, кожен окремим рядком: тригер, крок checkout,
    замаскована помилка. Читається текст файла, бо стан у GitHub говорить лише
    про останній прогін, а не про те, чому він такий."""
    text = repo.read(CI_WORKFLOW)
    if text is None:
        return [
            Result("B", code, FAIL, f"{CI_WORKFLOW} не знайдено") for code in ("B1", "B2", "B3")
        ]

    triggers = yaml_top_blocks(text).get("on", "")
    if "pull_request" in triggers:
        out = [Result("B", "B1", OK, "пайплайн запускається на pull request")]
    else:
        out = [
            Result(
                "B",
                "B1",
                FAIL,
                "у ci.yml немає тригера pull_request: на pull request не запускається нічого, "
                "тому і в required checks вибирати нема чого",
            )
        ]

    jobs = yaml_jobs(text)
    without = [name for name, block in jobs.items() if "actions/checkout" not in block]
    if not jobs:
        out.append(Result("B", "B2", FAIL, "у ci.yml не видно жодного job"))
    elif without:
        out.append(
            Result(
                "B",
                "B2",
                FAIL,
                "немає кроку actions/checkout у job: " + ", ".join(sorted(without)),
            )
        )
    else:
        out.append(Result("B", "B2", OK, f"код забирається в кожному job: {len(jobs)}"))

    if "continue-on-error" in text:
        out.append(
            Result(
                "B",
                "B3",
                FAIL,
                "у ci.yml лишився continue-on-error: провалений крок не робить перевірку "
                "червоною, тому зелена галочка нічого не означає",
            )
        )
    else:
        out.append(Result("B", "B3", OK, "жоден крок не ховає власну помилку"))

    return out


def latest_run(repo: Repo, slug: str, workflow: str) -> dict | None:
    data = repo.gh_json(f"repos/{slug}/actions/workflows/{workflow}/runs?per_page=1")
    runs = (data or {}).get("workflow_runs") if isinstance(data, dict) else None
    if not runs:
        return None
    return runs[0]


def required_contexts(repo: Repo, slug: str) -> set[str] | None:
    found = None
    rules = repo.gh_json(f"repos/{slug}/rules/branches/main")
    if isinstance(rules, list):
        found = set()
        for rule in rules:
            if isinstance(rule, dict) and rule.get("type") == "required_status_checks":
                checks = (rule.get("parameters") or {}).get("required_status_checks") or []
                found |= {item.get("context", "") for item in checks if isinstance(item, dict)}
    classic = repo.gh_json(f"repos/{slug}/branches/main/protection")
    if isinstance(classic, dict):
        contexts = (classic.get("required_status_checks") or {}).get("contexts") or []
        found = (found or set()) | set(contexts)
    return found


def check_lr4_process(repo: Repo) -> list[Result]:
    slug = repo.slug()
    codes = ("B4", "B5", "B6", "C1")
    if not repo.gh_available() or slug is None:
        return [
            Result("B" if code[0] == "B" else "C", code, SKIP, "потрібен gh і remote origin")
            for code in codes
        ]

    out: list[Result] = []

    run = latest_run(repo, slug, "ci.yml")
    if run is None:
        out.append(Result("B", "B4", FAIL, "жодного прогону ci.yml ще не було"))
        out.append(Result("B", "B5", SKIP, "немає прогону, артефакти перевіряти нема де"))
    elif run.get("conclusion") == "success":
        number = run.get("run_number")
        out.append(Result("B", "B4", OK, f"останній прогін ci.yml зелений (#{number})"))
        out.append(coverage_artifact(repo, slug, run))
    else:
        state = run.get("conclusion") or run.get("status") or "невідомо"
        out.append(Result("B", "B4", FAIL, f"останній прогін ci.yml: {state}"))
        out.append(coverage_artifact(repo, slug, run))

    mutants = latest_run(repo, slug, "mutants.yml")
    if mutants is None:
        out.append(Result("B", "B6", FAIL, "жодного прогону mutants.yml ще не було"))
    elif mutants.get("conclusion") == "success":
        out.append(Result("B", "B6", OK, "усі чотири публічні мутанти вбиті"))
    else:
        out.append(
            Result(
                "B",
                "B6",
                FAIL,
                "останній прогін mutants.yml червоний: серед чотирьох публічних мутантів "
                "хтось вижив, звіт у кроці «Прогін мутантів»",
            )
        )

    contexts = required_contexts(repo, slug)
    if contexts is None:
        out.append(Result("C", "C1", SKIP, "не вдалося прочитати захист main"))
    else:
        missing = [name for name in REQUIRED_CONTEXTS if name not in contexts]
        if not missing:
            out.append(Result("C", "C1", OK, "required checks увімкнені: lint і test"))
        elif contexts:
            out.append(
                Result(
                    "C",
                    "C1",
                    FAIL,
                    "серед required checks немає: "
                    + ", ".join(missing)
                    + f" (є: {', '.join(sorted(contexts))})",
                )
            )
        else:
            out.append(Result("C", "C1", FAIL, "required status checks на main не увімкнені"))

    return out


def coverage_artifact(repo: Repo, slug: str, run: dict) -> Result:
    data = repo.gh_json(f"repos/{slug}/actions/runs/{run.get('id')}/artifacts")
    items = (data or {}).get("artifacts") if isinstance(data, dict) else None
    if items is None:
        return Result("B", "B5", SKIP, "GitHub API не віддав список артефактів")
    names = {item.get("name") for item in items if isinstance(item, dict)}
    if COVERAGE_ARTIFACT in names:
        return Result("B", "B5", OK, f"у прогоні є артефакт {COVERAGE_ARTIFACT}")
    return Result(
        "B",
        "B5",
        FAIL,
        f"у прогоні немає артефакта {COVERAGE_ARTIFACT}: звіт покриття нікуди не зберігається",
    )


def run_lr4(repo: Repo, run_slow: bool) -> list[Result]:
    return (
        prefixed(run_lr1(repo, run_slow), "ЛР1")
        + prefixed(check_lr2_files(repo) + check_lr2_process(repo), "ЛР2")
        + prefixed(check_lr3_files(repo) + check_lr3_process(repo), "ЛР3")
        + prefixed(check_lr4_tests(repo) + check_lr4_files(repo) + check_lr4_process(repo), "ЛР4")
    )


# --------------------------------------------------------------------------- #
# ЛР5. Проміжна контрольна точка
# --------------------------------------------------------------------------- #
#
# Нових артефактів ЛР5 не додає, тому правил тут мало і кожне відповідає рядку
# рубрики. Перший рядок рубрики, «валідатор зелений», окремого правила не має
# навмисно: це весь звіт вище, тобто відсутність FAIL за ЛР1-ЛР4.

SCREENCAST_MARK = re.compile(r"\bЛР\s?0?5\b|\bLR\s?0?5\b|скринкаст", re.IGNORECASE)
LINK = re.compile(r"https?://\S+")
BRANCH_PREFIX = re.compile(r"\b([a-z][a-z0-9._-]{1,20})/")
COMMIT_TYPES = (
    "feat",
    "fix",
    "docs",
    "style",
    "refactor",
    "perf",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
)
COMMIT_TYPE_IN_TEXT = re.compile(r"\b(" + "|".join(COMMIT_TYPES) + r")\b", re.IGNORECASE)
COMMIT_TYPE_IN_SUBJECT = re.compile(r"^([a-z]+)(?:\([^)]+\))?!?: ")


def section_text(text: str, heading_pattern: str) -> str | None:
    """Тіло розділу, знайденого за заголовком. None, якщо розділу немає.

    Відрізняється від section_items тим, що віддає весь текст, а не тільки
    елементи списку: угода про гілки і коміти пишеться прозою.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.lstrip().startswith("#") and re.search(heading_pattern, line, re.IGNORECASE):
            start = index + 1
            break
    if start is None:
        return None
    body = []
    for line in lines[start:]:
        if line.lstrip().startswith("#"):
            break
        body.append(line)
    return "\n".join(body)


def check_lr5_screencast(repo: Repo) -> list[Result]:
    """Рядок рубрики «показ проведено». Валідатор не дивиться відео, він знаходить
    посилання і друкує його разом із датою, щоб пакетний прогін по підгрупі одразу
    давав список того, що треба подивитись."""
    slug = repo.slug()
    if not repo.gh_available() or slug is None:
        return [Result("A", "A1", SKIP, "потрібен gh і remote origin")]

    raw = repo.gh_json(f"repos/{slug}/issues?state=all&per_page=100")
    if raw is None:
        return [Result("A", "A1", SKIP, "GitHub API не віддав issue")]

    owner = slug.split("/")[0]
    found = []
    for item in raw:
        if "pull_request" in item or (item.get("user") or {}).get("login") != owner:
            continue
        title = item.get("title") or ""
        body = item.get("body") or ""
        if not SCREENCAST_MARK.search(title) and not SCREENCAST_MARK.search(body):
            continue
        link = LINK.search(body) or LINK.search(title)
        if link:
            found.append((item, link.group(0)))

    if not found:
        return [
            Result(
                "A",
                "A1",
                FAIL,
                "немає issue зі скринкастом: потрібна власна issue, у назві якої є ЛР5, "
                "а в тілі посилання на п'ятихвилинний запис",
            )
        ]

    item, link = max(found, key=lambda pair: pair[0].get("created_at") or "")
    when = (item.get("created_at") or "")[:10]
    return [Result("A", "A1", OK, f"скринкаст: {link} (issue #{item.get('number')}, {when})")]


def check_lr5_pipeline(repo: Repo) -> list[Result]:
    """Рядок рубрики «пайплайн зелений». ЛР4 дивиться на останній прогін де завгодно,
    тому зеленою може бути гілка, а main лишатись червоним. Контрольна точка питає
    саме про main."""
    slug = repo.slug()
    if not repo.gh_available() or slug is None:
        return [Result("B", code, SKIP, "потрібен gh і remote origin") for code in ("B1", "B2")]

    out: list[Result] = []
    for code, workflow, title in (
        ("B1", "ci.yml", "пайплайн"),
        ("B2", "mutants.yml", "прогін мутантів"),
    ):
        data = repo.gh_json(
            f"repos/{slug}/actions/workflows/{workflow}/runs?branch=main&per_page=1"
        )
        runs = (data or {}).get("workflow_runs") if isinstance(data, dict) else None
        if runs is None:
            out.append(Result("B", code, SKIP, f"GitHub API не віддав прогони {workflow}"))
        elif not runs:
            out.append(
                Result("B", code, FAIL, f"{workflow} жодного разу не відпрацював на main")
            )
        elif runs[0].get("conclusion") == "success":
            number = runs[0].get("run_number")
            out.append(Result("B", code, OK, f"на main {title} зелений (#{number})"))
        else:
            state = runs[0].get("conclusion") or runs[0].get("status") or "невідомо"
            out.append(Result("B", code, FAIL, f"на main {workflow}: {state}"))
    return out


def check_lr5_agreement(repo: Repo) -> list[Result]:
    """Рядок рубрики «історія відповідає власному CONTRIBUTING.md».

    Правило звіряє історію не із загальним ідеалом, а з тим, що студент сам
    написав на ЛР1. Тому воно однаково закривається двома способами: привести
    історію до угоди або чесно переписати угоду під те, як робота йшла насправді.
    """
    contributing = repo.read("CONTRIBUTING.md")
    if contributing is None:
        return [
            Result("C", code, FAIL, "CONTRIBUTING.md не знайдено, звіряти нема з чим")
            for code in ("C1", "C2")
        ]

    slug = repo.slug()
    out: list[Result] = []

    branches = section_text(contributing, r"гілк")
    declared = sorted({match.lower() for match in BRANCH_PREFIX.findall(branches or "")})
    if branches is None:
        out.append(Result("C", "C1", FAIL, "у CONTRIBUTING.md немає розділу про гілки"))
    elif not declared:
        out.append(
            Result(
                "C",
                "C1",
                FAIL,
                "у розділі про гілки немає прикладу реального імені виду feat/щось, "
                "тому звіряти історію нема з чим",
            )
        )
    elif not repo.gh_available() or slug is None:
        out.append(Result("C", "C1", SKIP, "потрібен gh і remote origin"))
    else:
        pulls = repo.gh_json(f"repos/{slug}/pulls?state=closed&per_page=100")
        if pulls is None:
            out.append(Result("C", "C1", SKIP, "GitHub API не віддав pull request"))
        else:
            used = [
                (item.get("head") or {}).get("ref", "")
                for item in pulls
                if item.get("merged_at") and (item.get("head") or {}).get("ref")
            ]
            odd = [name for name in used if not any(name.lower().startswith(p) for p in declared)]
            shown = ", ".join(sorted(declared)[:5])
            if not used:
                out.append(Result("C", "C1", SKIP, "змержених pull request немає"))
            elif len(odd) <= 1:
                fit = len(used) - len(odd)
                out.append(
                    Result("C", "C1", OK, f"гілки за угодою ({shown}): {fit} з {len(used)}")
                )
            else:
                out.append(
                    Result(
                        "C",
                        "C1",
                        FAIL,
                        f"угода називає {shown}, а гілки інші: " + ", ".join(sorted(odd)[:5]),
                    )
                )

    commits = section_text(contributing, r"коміт")
    named = sorted({match.lower() for match in COMMIT_TYPE_IN_TEXT.findall(commits or "")})
    if commits is None:
        out.append(Result("C", "C2", FAIL, "у CONTRIBUTING.md немає розділу про коміти"))
    elif not named:
        out.append(
            Result(
                "C",
                "C2",
                FAIL,
                "у розділі про коміти не названо жодного типу: перелічіть ті, "
                "якими справді користуєтесь",
            )
        )
    else:
        ref = main_ref(repo)
        log = repo.run(["git", "log", "--format=%s", "-30", ref]) if ref else None
        subjects = log.stdout.splitlines() if log and log.returncode == 0 else []
        used = set()
        for line in subjects:
            match = COMMIT_TYPE_IN_SUBJECT.match(line.strip())
            if match and match.group(1).lower() in COMMIT_TYPES:
                used.add(match.group(1).lower())
        extra = sorted(used - set(named))
        if not used:
            out.append(Result("C", "C2", SKIP, "у main немає комітів за Conventional Commits"))
        elif not extra:
            out.append(
                Result(
                    "C",
                    "C2",
                    OK,
                    "типи комітів у main усі названі в угоді: " + ", ".join(sorted(used)),
                )
            )
        else:
            out.append(
                Result(
                    "C",
                    "C2",
                    FAIL,
                    f"в угоді названі {', '.join(named)}, а в main є ще: {', '.join(extra)}",
                )
            )

    return out


def run_lr5(repo: Repo, run_slow: bool) -> list[Result]:
    return run_lr4(repo, run_slow) + prefixed(
        check_lr5_screencast(repo) + check_lr5_pipeline(repo) + check_lr5_agreement(repo), "ЛР5"
    )


CHECKS = {1: run_lr1, 2: run_lr2, 3: run_lr3, 4: run_lr4, 5: run_lr5}


def render(results: list[Result], lr: int) -> str:
    lines = [f"# Валідатор курсу, ЛР{lr}", ""]
    order = {FAIL: 0, WARN: 1, SKIP: 2, OK: 3, LATER: 4}
    for block in ("A", "B", "C"):
        rows = [item for item in results if item.block == block]
        if not rows:
            continue
        titles = {"A": "A, артефакти", "B": "B, воно працює", "C": "C, слід процесу"}
        lines.append(f"## Блок {titles[block]}")
        lines.append("")
        for item in sorted(rows, key=lambda r: (order[r.level], r.code)):
            lines.append(f"- `{item.level:5}` **{item.code}** {item.message}")
        lines.append("")

    fails = [item for item in results if item.level == FAIL]
    if fails:
        lines.append(f"**Незакритих вимог: {len(fails)}.** Кожна з них це рядок FAIL вище.")
    else:
        lines.append(
            "**Незакритих вимог немає.** Рядки LATER це артефакти майбутніх робіт, "
            "з ними все гаразд."
        )
    return "\n".join(lines)


def rotation_lines(repo: Repo) -> list[str]:
    """Рядок таблиці ротації у вигляді готового шматка звіту. Питання «а кого мені
    рев'ювати» приходить кожного разу, а таблиця в issue довга, тому відповідь
    друкується там, куди студент і так дивиться після кожного push."""
    slug = repo.slug()
    if slug is None:
        return ["Не видно remote origin, тому невідомо, чий це репозиторій."]
    login = slug.split("/")[0]
    entry, table = rotation_entry(login)
    if table is None:
        return ["Таблиці ротації ще немає: вона з'являється до пари ЛР3."]
    if entry is None:
        return [
            f"Логіна `{login}` у таблиці ротації немає. Перевір, чи стоїть твій рядок "
            "у закріпленій issue «Репозиторії потоку 2026», і напиши про це в issue ротації."
        ]
    fallback = table.get("fallback", {})
    repos = {
        item.get("github", "").lower(): item.get("repo", "")
        for item in table.get("students", [])
    }
    titles = {
        "lr03": "ЛР3, рев'ю pull request",
        "lr08": "ЛР8, onboarding за README",
        "lr13": "ЛР13, рев'ю портфеля",
    }
    out = [f"{login}, підгрупа {entry.get('subgroup', '?')}, номер {entry.get('n', '?')} у кільці."]
    for work, title in titles.items():
        shown = []
        for name in entry.get(work) or []:
            if name:
                slug_other = repos.get(name.lower())
                shown.append(f"https://github.com/{slug_other}" if slug_other else name)
            else:
                spare = fallback.get(work) or "посилання буде в issue ротації до пари"
                shown.append(f"об'єкт курсу ({spare})")
        out.append(f"- {title}: {', '.join(shown) if shown else "об'єкт курсу"}")
    incoming = entry.get("lr03_reviewers") or []
    if incoming:
        out.append(f"- твій PR на ЛР3 читають: {', '.join(incoming)}")
    return out


def show_rotation(repo: Repo) -> int:
    """Режим --who для тих, у кого під рукою свіжа копія скрипта."""
    for line in rotation_lines(repo):
        print(line)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Валідатор репозиторію курсу")
    parser.add_argument("--repo", default=".", help="каталог репозиторію")
    parser.add_argument("--lr", type=int, help="номер роботи, за замовчуванням з tools/course.json")
    parser.add_argument(
        "--slow", action="store_true", help="запускати make test, make lint і сервіс"
    )
    parser.add_argument("--summary", help="записати звіт у файл")
    parser.add_argument(
        "--strict", action="store_true", help="повернути код 1, якщо є хоч один FAIL"
    )
    parser.add_argument(
        "--who", action="store_true", help="показати свій рядок таблиці ротації рев'ю"
    )
    args = parser.parse_args()

    if args.who:
        return show_rotation(Repo(Path(args.repo)))

    lr = args.lr or int(course_config().get("current_lr", 1))
    if lr not in CHECKS:
        print(f"Правил для ЛР{lr} ще немає. Доступні: {', '.join(str(k) for k in sorted(CHECKS))}.")
        return 0

    repo = Repo(Path(args.repo))
    results = CHECKS[lr](repo, args.slow)
    report = render(results, lr)

    if lr >= 3:
        report += "\n\n## Ротація рев'ю\n\n" + "\n".join(rotation_lines(repo)) + "\n"

    print(report)
    if args.summary:
        Path(args.summary).write_text(report + "\n", encoding="utf-8")

    if args.strict and any(item.level == FAIL for item in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
