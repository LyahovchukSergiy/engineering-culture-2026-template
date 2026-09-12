#!/usr/bin/env python3
"""AI-рев'ю pull request: читає диф, складає рев'ю і кладе його коментарем.

Коментує і нічого більше. Approve не ставить, злиття не блокує, код не міняє.

Модель підключається трьома значеннями з налаштувань репозиторію:

    vars.AI_REVIEW_URL      базова адреса, сумісна з OpenAI API
    vars.AI_REVIEW_MODEL    назва моделі
    secrets.AI_REVIEW_KEY   ключ

Ключа в цьому файлі немає і бути не може: він приходить зі GitHub Secrets через
оточення. Ключ, укладений у файл workflow, це той самий витік, який ви ловили на
ЛР7, тільки зроблений власноруч.

Якщо ключа немає, скрипт не падає і не мовчить: він робить рев'ю за правилами
зі слайда 18 лекції L8 і прямо пише в коментарі, що це правила, а не модель.
"""

import json
import os
import sys
import urllib.error
import urllib.request

MARKER = "<!-- ai-review -->"
API = "https://api.github.com"


def api(
    path: str,
    token: str,
    data: dict | None = None,
    method: str = "GET",
    accept: str = "",
) -> str:
    request = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(data).encode() if data else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": accept or "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def model_review(diff: str, url: str, model: str, key: str) -> str:
    """Питає модель через будь-який сумісний з OpenAI API ендпойнт."""
    prompt = (
        "Ти рев'юер pull request. Дай не більше семи зауважень по цьому дифу. "
        "Для кожного: файл, у чому проблема, чим загрожує. "
        "Окремо перевір: чи існують викликані методи бібліотек, чи тести "
        "перевіряють поведінку, чи немає неперевіреного вводу в шляхах і "
        "запитах, чи немає зайвої абстракції. Не хвали, не переказуй диф.\n\n"
        f"{diff[:60000]}"
    )
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
    }
    request = urllib.request.Request(
        f"{url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        answer = json.loads(response.read().decode())
    return answer["choices"][0]["message"]["content"].strip()


def rule_review(diff: str) -> str:
    """Запасне рев'ю за правилами: п'ять перевірок зі слайда 18 лекції L8."""
    files: list[str] = []
    added: list[str] = []
    removed_in_tests = 0
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            files.append(current)
        elif line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:])
        elif line.startswith("-") and not line.startswith("---") and current.startswith("tests/"):
            removed_in_tests += 1

    notes = []
    source = [name for name in files if name.startswith("src/")]
    tests = [name for name in files if name.startswith("tests/")]
    if source and not tests:
        notes.append("Змінено код у `src/`, а в `tests/` не змінено нічого. Чого бракує тесту.")
    if removed_in_tests:
        notes.append(
            f"У `tests/` прибрано або переписано {removed_in_tests} рядків. "
            "Переписаний тест означає, що контракт підігнали під код: перевірте це першим."
        )
    starts = ("import ", "from ")
    imports = sorted({line.strip() for line in added if line.strip().startswith(starts)})
    if imports:
        listing = "\n".join(f"    {line}" for line in imports)
        notes.append("Нові рядки імпорту, перевірте, що кожен пакет і метод існують:\n" + listing)
    if len(added) > 200:
        notes.append(
            f"Додано {len(added)} рядків. Такий diff рев'юється погано, ділиться на частини."
        )
    risky = [
        name for name in files if name.startswith(".github/workflows/") or name.endswith(".env")
    ]
    if risky:
        notes.append(
            "Зачеплено пайплайн або оточення: " + ", ".join(risky) + ". Це зона окремого PR."
        )
    if not notes:
        notes.append("Механічних зауважень немає. Це не означає, що диф правильний.")

    return (
        "Модель не налаштована, тому це рев'ю за правилами, а не за змістом. "
        "Щоб отримати рев'ю моделі, додайте `AI_REVIEW_URL` і `AI_REVIEW_MODEL` у "
        "Variables і `AI_REVIEW_KEY` у Secrets репозиторію.\n\n"
        + "\n\n".join(f"{number}. {note}" for number, note in enumerate(notes, 1))
    )


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN", "")
    slug = os.environ.get("GITHUB_REPOSITORY", "")
    number = os.environ.get("PR_NUMBER", "")
    if not (token and slug and number):
        print("Немає GITHUB_TOKEN, GITHUB_REPOSITORY або PR_NUMBER.", file=sys.stderr)
        return 1

    diff = api(f"/repos/{slug}/pulls/{number}", token, accept="application/vnd.github.v3.diff")

    url = os.environ.get("AI_REVIEW_URL", "").strip()
    model = os.environ.get("AI_REVIEW_MODEL", "").strip()
    key = os.environ.get("AI_REVIEW_KEY", "").strip()
    if url and model and key:
        try:
            text = f"Рев'ю моделі `{model}`.\n\n{model_review(diff, url, model, key)}"
        except Exception as error:  # noqa: BLE001 крок рев'ю не має права падати
            text = (
                f"Модель `{model}` не відповіла: {type(error).__name__}. "
                f"Нижче рев'ю за правилами.\n\n{rule_review(diff)}"
            )
    else:
        text = rule_review(diff)

    body = f"{MARKER}\n### AI-рев'ю\n\n{text}\n\nКоментар, не approve. Читає і вирішує людина."
    existing = json.loads(api(f"/repos/{slug}/issues/{number}/comments?per_page=100", token))
    mine = [item for item in existing if MARKER in item.get("body", "")]
    if mine:
        api(f"/repos/{slug}/issues/comments/{mine[-1]['id']}", token, {"body": body}, "PATCH")
        print(f"Коментар {mine[-1]['id']} оновлено.")
    else:
        api(f"/repos/{slug}/issues/{number}/comments", token, {"body": body}, "POST")
        print("Коментар доданий.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
