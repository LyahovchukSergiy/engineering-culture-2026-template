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

CONFIG_PATH = Path(__file__).with_name("course.json")


def course_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"current_lr": 1}


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


def branch_rule_types(repo: Repo, slug: str) -> set[str] | None:
    """Активні правила захисту main. Читає і рулсети, і класичний захист."""
    types: set[str] = set()
    seen = False

    rules = repo.gh_json(f"repos/{slug}/rules/branches/main")
    if isinstance(rules, list):
        seen = True
        for rule in rules:
            if isinstance(rule, dict) and rule.get("type"):
                types.add(rule["type"])

    classic = repo.gh_json(f"repos/{slug}/branches/main/protection")
    if isinstance(classic, dict):
        seen = True
        if classic.get("required_pull_request_reviews") is not None:
            types.add("pull_request")
        if (classic.get("required_linear_history") or {}).get("enabled"):
            types.add("required_linear_history")
        if not (classic.get("allow_force_pushes") or {}).get("enabled", False):
            types.add("non_fast_forward")

    return types if seen else None


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
    codes = ("C1", "C2", "C3", "C4", "C5", "C6")
    if not repo.gh_available() or slug is None:
        return [Result("C", code, SKIP, "потрібен gh і remote origin") for code in codes]

    out: list[Result] = []

    types = branch_rule_types(repo, slug)
    if types is None:
        note = "не вдалося прочитати захист main, потрібен доступ administration: read"
        out.extend(Result("C", code, SKIP, note) for code in ("C1", "C2", "C3"))
    else:
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


def run_lr2(repo: Repo, run_slow: bool) -> list[Result]:
    return prefixed(run_lr1(repo, run_slow), "ЛР1") + prefixed(
        check_lr2_files(repo) + check_lr2_process(repo), "ЛР2"
    )


CHECKS = {1: run_lr1, 2: run_lr2}


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
    args = parser.parse_args()

    lr = args.lr or int(course_config().get("current_lr", 1))
    if lr not in CHECKS:
        print(f"Правил для ЛР{lr} ще немає. Доступні: {', '.join(str(k) for k in sorted(CHECKS))}.")
        return 0

    repo = Repo(Path(args.repo))
    results = CHECKS[lr](repo, args.slow)
    report = render(results, lr)

    print(report)
    if args.summary:
        Path(args.summary).write_text(report + "\n", encoding="utf-8")

    if args.strict and any(item.level == FAIL for item in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
