"""Разбор аргумента-идентификатора: ключ, id, URL, SPACE:Title."""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass

# NotFoundError, а не голый AttachmentError: у базы exit_code = EXIT_PARTIAL,
# то есть «часть файлов скачана». Нераспознанный идентификатор — это нулевой
# результат, и код выхода должен быть 2, а не 1.
from .errors import NotFoundError

_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]*-\d+")
_BROWSE = re.compile(r"/browse/([A-Za-z][A-Za-z0-9_]*-\d+)")
_PAGES_ID = re.compile(r"/pages/(\d+)(?:/|$)")
_SPACE_KEY = re.compile(r"[A-Za-z0-9_]+")


@dataclass(frozen=True)
class PageRef:
    page_id: str | None
    space: str | None
    title: str | None


def _is_page_id(raw: str) -> bool:
    """id страницы — только ASCII-цифры.

    isdigit() истинно и для «١٢٣», и для «²», а значение уходит в путь
    REST-запроса, так что проверяем и алфавит.
    """
    return raw.isascii() and raw.isdigit()


def parse_jira(argument: str) -> str:
    value = (argument or "").strip()
    if _KEY.fullmatch(value):
        return value.upper()
    found = _BROWSE.search(value)
    if found:
        return found.group(1).upper()
    raise NotFoundError(
        f"не понял идентификатор тикета: {argument!r}. "
        "Ожидается ключ вида PROJ-123 или ссылка вида https://jira/browse/PROJ-123"
    )


def parse_confluence(argument: str) -> PageRef:
    value = (argument or "").strip()
    if not value:
        raise NotFoundError("пустой идентификатор страницы")

    if _is_page_id(value):
        return PageRef(value, None, None)

    if "://" in value:
        parsed = urllib.parse.urlparse(value)
        # parse_qs без keep_blank_values пустых значений не возвращает,
        # поэтому наличия ключа достаточно. А вот содержимое проверяем:
        # «?pageId=../../x» иначе ушло бы в путь REST-запроса.
        query = urllib.parse.parse_qs(parsed.query)
        if "pageId" in query and _is_page_id(query["pageId"][0]):
            return PageRef(query["pageId"][0], None, None)
        found = _PAGES_ID.search(parsed.path)
        if found:
            return PageRef(found.group(1), None, None)
        parts = [p for p in parsed.path.split("/") if p]
        if "display" in parts:
            at = parts.index("display")
            if len(parts) >= at + 3:
                title = urllib.parse.unquote_plus(parts[at + 2])
                return PageRef(None, parts[at + 1], title)
        raise NotFoundError(
            f"не нашёл идентификатор страницы в ссылке: {argument!r}"
        )

    # Ключ пространства — [A-Za-z0-9_]+, а заголовок не начинается с «/».
    # Без первого пространством становится любое слово слева от двоеточия,
    # без второго — «https:/wiki…» (опечатка в один слеш) читается как
    # пространство «https»: и то и другое уводит клиент искать несуществующее
    # пространство вместо отказа на месте.
    if ":" in value:
        space, _, title = value.partition(":")
        space, title = space.strip(), title.strip()
        if title and not title.startswith("/") and _SPACE_KEY.fullmatch(space):
            return PageRef(None, space, title)

    raise NotFoundError(
        f"не понял идентификатор страницы: {argument!r}. "
        "Ожидается числовой id, ссылка на страницу или SPACE:Заголовок"
    )
