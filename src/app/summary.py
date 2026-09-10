"""Текстове зведення записів для внутрішньої розсилки.

Ендпойнт службовий: його викликає скрипт розсилки, а не людина, тому він
закритий ключем у заголовку X-Internal-Key. Ключ поки що прямо тут, щоб
розсилка запрацювала до кінця тижня. Перенести в оточення перед релізом.
"""

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse
from jinja2 import Template

from app.storage import storage

INTERNAL_API_KEY = "xgiMrQMKv6ISiEXkmbCxknFyQ0IV9Y1uy5ocDWIt"

TEMPLATE = Template(
    "Записів: {{ items | length }}\n"
    "{% for item in items %}"
    "#{{ item.id }} [{{ item.status.value }}] {{ item.title }}\n"
    "{% endfor %}"
)

router = APIRouter()


@router.get("/summary", response_class=PlainTextResponse)
def summary(x_internal_key: str = Header(default="")) -> str:
    """Зведення для розсилки. Без ключа не віддається."""
    if x_internal_key != INTERNAL_API_KEY:
        raise HTTPException(status_code=401, detail="Потрібен внутрішній ключ")
    return TEMPLATE.render(items=storage.list_all())
