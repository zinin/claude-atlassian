"""Имена файлов, имена папок и выбор базовой директории."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import unicodedata

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_DASHES = re.compile(r"-{2,}")
_MAX_NAME_BYTES = 255
_FALLBACK = "attachment"

# Ограничения имён, которые есть только на Windows. Правила включаются ровно
# там: на Unix двоеточие, звёздочка и хвостовая точка — обычные символы имени,
# и вырезать их значило бы менять имена, которые сегодня доходят до диска
# такими же, какими лежат в Jira и Confluence.
_WINDOWS = os.name == "nt"
_WINDOWS_INVALID = re.compile(r'[<>:"|?*]')
_WINDOWS_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{digit}" for digit in range(1, 10)]
    + [f"LPT{digit}" for digit in range(1, 10)]
)

# Файловые системы, на которых два разных написания — одна запись каталога:
# регистр не различают и NTFS, и APFS по умолчанию, а macOS вдобавок хранит
# имена нормализованными.
_FOLDED_NAMES = _WINDOWS or sys.platform == "darwin"


def _windows_safe(name: str) -> str:
    """Приводит имя к тому, что Windows согласна создать как обычный файл.

    Три разных беды, и опаснее не та, что заметнее. Имена устройств (CON,
    NUL, COM1) и символы <>:"|?* дают отказ на os.open — падает одно вложение,
    и об этом написано в stderr. А вот хвостовые точки и пробелы Windows
    срезает молча: «report.» открывается как «report», то есть два разных
    вложения ложатся в один файл, и множество занятых имён этого не видит —
    в нём лежат разные строки. Двоеточие того же рода: «a:b.png» на NTFS это
    альтернативный поток файла «a», содержимое уходит туда, где пользователь
    его не найдёт.

    Имя устройства проверяется по части до первой точки: Windows резервирует
    и «CON», и «CON.log», и «CON.чтоугодно».
    """
    cleaned = _WINDOWS_INVALID.sub("-", name).rstrip(". ")
    if not cleaned:
        return _FALLBACK
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def _truncate_bytes(text: str, limit: int) -> str:
    """Обрезает по байтам, а не по символам: лимит файловой системы байтовый.

    errors="ignore" при декодировании выбрасывает хвост, если разрез пришёлся
    на середину многобайтового символа. Для кириллицы это обычное дело.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", errors="ignore")


def partial_name(name: str, suffix: str = ".part") -> str:
    """Имя временного файла, влезающее в тот же байтовый лимит, что и само имя.

    sanitize_filename отдаёт до 255 байт — ровно предел большинства файловых
    систем. Приписанный к такому имени суффикс этот предел переступает, и
    os.open отвечает ENAMETOOLONG: вложение с длинным именем не скачивалось
    вовсе. Место под суффикс освобождается здесь, а не за счёт самого имени
    файла: обрезать надо временное имя, а не то, что останется на диске.
    """
    room = _MAX_NAME_BYTES - len(suffix.encode("utf-8"))
    return _truncate_bytes(name, room) + suffix


def sanitize_filename(name: str) -> str:
    """Приводит имя из недоверенного источника к безопасному имени файла.

    Имя приходит от того, кто загрузил вложение в Jira или Confluence, а
    результат подставляется в путь записи на диск. Всё, из чего можно собрать
    выход за пределы целевой папки, вырезается; кириллица, пробелы и скобки
    остаются как есть.
    """
    # Управляющие символы убираем первыми: иначе "a\x00/../b" сохранил бы "/.."
    # после того, как разделители уже разобраны.
    cleaned = _CONTROL.sub("", name or "")
    # \\ → / и последний сегмент: так отсекаются и "../../x", и "..\\..\\x",
    # и абсолютный "/etc/passwd".
    cleaned = cleaned.replace("\\", "/").split("/")[-1].strip()
    if cleaned in ("", ".", ".."):
        return _FALLBACK
    # До проверки длины: добавленный префикс тоже занимает байты.
    if _WINDOWS:
        cleaned = _windows_safe(cleaned)

    if len(cleaned.encode("utf-8")) <= _MAX_NAME_BYTES:
        return cleaned

    stem, dot, suffix = cleaned.rpartition(".")
    # Длинный «хвост» после точки — не расширение, а часть имени: обрезаем
    # всё целиком, иначе под сам stem не останется места.
    if dot and len(suffix.encode("utf-8")) < 32:
        room = _MAX_NAME_BYTES - len(suffix.encode("utf-8")) - 1
        return f"{_truncate_bytes(stem, room)}.{suffix}"
    return _truncate_bytes(cleaned, _MAX_NAME_BYTES)


def slugify_title(title: str, limit: int = 60) -> str:
    """Заголовок страницы → часть имени папки: пробелы становятся дефисами."""
    cleaned = _CONTROL.sub("", title or "")
    cleaned = cleaned.replace("\\", "").replace("/", "")
    if _WINDOWS:
        # Двоеточие в заголовке Confluence — обычное дело, а mkdir такого
        # каталога Windows отвергает: теряется не одно вложение, а вся
        # страница. Имена устройств здесь не при чём — папка всегда начинается
        # с id и дефиса.
        cleaned = _WINDOWS_INVALID.sub("-", cleaned)
    cleaned = "-".join(cleaned.split())
    cleaned = _DASHES.sub("-", cleaned).strip("-")
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip("-")
    if _WINDOWS:
        cleaned = cleaned.rstrip(". ")
    return cleaned or _FALLBACK


def base_dir(start: pathlib.Path) -> pathlib.Path:
    """Корень git-репозитория, а если это не репозиторий — сама директория.

    CLAUDE_PROJECT_DIR здесь недоступна: её видят только хуки, не скрипт.
    """
    start = pathlib.Path(start).resolve()
    try:
        finished = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return start
    if finished.returncode == 0 and finished.stdout.strip():
        return pathlib.Path(finished.stdout.strip()).resolve()
    return start


def jira_dir(base: pathlib.Path, key: str) -> pathlib.Path:
    # Ключ уже прошёл parse_jira: [A-Za-z][A-Za-z0-9_]*-\d+, разделителей в нём
    # быть не может.
    return pathlib.Path(base) / "docs" / "jira-attachments" / key


def wiki_dir(base: pathlib.Path, page_id: str, title: str) -> pathlib.Path:
    """Папка страницы: `<id>-<слаг>`.

    Id идёт первым и не меняется, поэтому переименованная страница находится
    по префиксу `<id>-` и продолжает писать в свою старую папку, а не заводит
    рядом вторую.
    """
    parent = pathlib.Path(base) / "docs" / "wiki-attachments"
    if parent.is_dir():
        # sorted(), чтобы при нескольких подходящих папках выбор был
        # одинаковым от запуска к запуску: iterdir() порядка не обещает.
        for candidate in sorted(parent.iterdir()):
            if (
                candidate.is_dir()
                and not candidate.is_symlink()
                and candidate.name.startswith(f"{page_id}-")
            ):
                return candidate
    # Слуг ограничен символами, а предел файловой системы байтовый: заголовок
    # из четырёхбайтовых символов при длинном id переступает 255 байт, и mkdir
    # отвечает ENAMETOOLONG — тогда не скачивается ни одно вложение страницы.
    # Обрезается слуг, а не префикс: по «<id>-» страница находит свою папку
    # после переименования, и он обязан уцелеть целиком.
    prefix = f"{page_id}-"
    room = max(_MAX_NAME_BYTES - len(prefix.encode("utf-8")), 0)
    return parent / (prefix + _truncate_bytes(slugify_title(title), room).rstrip("-"))


def _insert_marker(name: str, marker: str) -> str:
    """Вставляет `-<marker>` перед расширением, не выходя за лимит в байтах.

    Обрезается stem, а не хвост: расширение и сам маркер должны уцелеть, иначе
    теряется и тип файла, и различимость имён.
    """
    stem, dot, suffix = name.rpartition(".")
    if dot and len(suffix.encode("utf-8")) < 32:
        tail = f"-{marker}.{suffix}"
    else:
        stem, tail = name, f"-{marker}"
    room = max(_MAX_NAME_BYTES - len(tail.encode("utf-8")), 0)
    return f"{_truncate_bytes(stem, room)}{tail}"


def _key(name: str) -> str:
    """Ключ брони: то, что файловая система считает одним и тем же именем.

    NTFS и APFS по умолчанию не различают регистр, а macOS вдобавок хранит
    имена нормализованными. Там «Report.log» и «report.log» — одна запись
    каталога, и бронировать их надо вместе: иначе второе вложение либо
    объявляется «уже был» при совпавшем размере, либо затирает первое, а в
    таблице обе строки отмечены скачанными. На Linux это разные файлы, и
    сводить их значило бы без нужды дописывать id к имени, которое ни с чем не
    сталкивается.
    """
    if not _FOLDED_NAMES:
        return name
    return unicodedata.normalize("NFC", name).casefold()


def _free(name: str, taken: set[str]) -> bool:
    """Имя свободно, только если свободен и его сосед `.part`.

    Байты вложения принимает временный файл `<имя>.part`, и он в том же
    каталоге. Тикет с парой «report.log» и «report.log.part» иначе даёт
    затирание: скачивание первого пишет ровно в имя второго, и оба при этом
    отмечаются как скачанные.
    """
    return _key(name) not in taken and _key(partial_name(name)) not in taken


def _claim(name: str, taken: set[str]) -> None:
    taken.add(_key(name))
    taken.add(_key(partial_name(name)))


def unique_path(
    directory: pathlib.Path, filename: str, attachment_id: str, taken: set[str]
) -> pathlib.Path:
    """Путь для вложения; при совпадении имён вставляет id перед расширением.

    Имя с подставленным id тоже может оказаться занятым — вложение имеет право
    называться «report-100042.log». Поэтому не одна попытка, а цикл: молча
    отдать чужое имя значит затереть уже скачанный файл.
    """
    safe = sanitize_filename(filename)
    if _free(safe, taken):
        _claim(safe, taken)
        return pathlib.Path(directory) / safe

    candidate = _insert_marker(safe, attachment_id)
    counter = 2
    while not _free(candidate, taken):
        candidate = _insert_marker(safe, f"{attachment_id}-{counter}")
        counter += 1
    _claim(candidate, taken)
    return pathlib.Path(directory) / candidate
