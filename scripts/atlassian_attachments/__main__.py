"""CLI: скачивает вложения Jira и Confluence на диск."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import urllib.parse

from .client import Attachment, Client
from .credentials import resolve
from .errors import (
    EXIT_NO_CREDENTIALS,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PARTIAL,
    AttachmentError,
)
from .identifiers import parse_confluence, parse_jira
from .naming import base_dir, jira_dir, unique_path, wiki_dir

_MIME_COLUMN = 38
# Отметки состояний в таблице. «пересобран сервером» — тело, которое сервер
# собрал на лету и отдал не тем размером, что стоит в метаданных; такой файл
# скачивается заново каждый прогон, потому что пропуск по совпадению размера
# для него никогда не срабатывает.
_MARK = {"skipped": "  (уже был)", "rebuilt": "  (пересобран сервером)"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atlassian-attachments",
        description="Скачивает вложения Jira и Confluence напрямую на диск, минуя контекст модели.",
    )
    parser.add_argument("mode", choices=["jira", "wiki", "confluence"], help="jira или wiki")
    parser.add_argument("target", help="ключ тикета, id страницы, URL или SPACE:Заголовок")
    parser.add_argument("--dir", dest="directory", help="переопределить базовую директорию")
    parser.add_argument("--force", action="store_true", help="перекачать вместо пропуска")
    parser.add_argument("--only", help="фильтр: префиксы mime и расширения через запятую")
    parser.add_argument("--url", help="базовый URL")
    parser.add_argument("--token", help="токен")
    parser.add_argument("--insecure", action="store_true", help="не проверять TLS")
    parser.add_argument("--explain-auth", action="store_true", help="показать источник кред")
    return parser


def _safe_url(url: str) -> str:
    """Базовый URL без userinfo и query — то, что можно печатать.

    «https://svc:PAT@jira/…» в JIRA_URL встречается регулярно, ради этого и
    написан _safe() в client.py. --explain-auth отвечает на два вопроса — с
    какого хоста работаем и откуда взялись креды, — и userinfo не отвечает ни на
    один из них. А stdout уходит в контекст модели, то есть ровно в тот поток,
    ради обхода которого написан весь инструмент.

    Свой хелпер, а не Client._safe: тот приватный, и тащить его наружу значило бы
    либо сделать его публичным API, либо лезть под подчёркивание.
    """
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        # Битый IPv6 («https://[::1/») роняет сам разбор, а --explain-auth
        # запускают именно тогда, когда с адресом что-то не так: падать здесь
        # значит не ответить на единственный вопрос, ради которого позвали.
        return "<адрес не разобран>"
    if not host:
        # Без «//» urlsplit читает «svc:PAT@jira/…» как схему «svc» и путь
        # «PAT@jira/…»: userinfo остаётся в path и уехало бы в печать целиком.
        # Раз адрес не разобран, показывать из него нельзя ничего — тем более
        # что именно с таким URL инструмент не работает вовсе, а --explain-auth
        # запускают первым как раз тогда, когда он не работает.
        return "<адрес не разобран>"
    try:
        port = parsed.port
    except ValueError:
        # Нечисловой порт. Показать нечего, но и падать из-за отладочной
        # строки нельзя — netloc сюда брать тем более: там и живёт userinfo.
        port = None
    netloc = f"{host}:{port}" if port else host
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _short_mime(mime: str, limit: int = _MIME_COLUMN) -> str:
    """Укорачивает mime, выедая середину, чтобы колонка не разъезжалась.

    Обычный «application/vnd.openxmlformats-officedocument.wordprocessingml.document»
    — 71 символ: на тикете, где рядом лежат docx и png, имена файлов перестают
    попадать в колонку. Режется именно середина, а не хвост: у docx и xlsx общие
    первые 45 символов и разные концы, так что обрезка справа слепила бы их в
    одну неразличимую строку.
    """
    if len(mime) <= limit:
        return mime
    head = (limit - 1) // 2
    return f"{mime[:head]}…{mime[head + 1 - limit:]}"


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование числительного: 1 файл, 2 файла, 5 файлов."""
    if count % 100 in range(11, 15):
        return many
    last = count % 10
    if last == 1:
        return one
    if last in (2, 3, 4):
        return few
    return many


def _files_word(count: int) -> str:
    return _plural(count, "файл", "файла", "файлов")


def select(attachments: list[Attachment], only: str | None) -> list[Attachment]:
    if not only:
        return list(attachments)
    patterns = [p.strip().lower() for p in only.split(",") if p.strip()]
    chosen = []
    for item in attachments:
        mime = (item.mime or "").lower()
        name = (item.filename or "").lower()
        if any(mime.startswith(p) or (p.startswith(".") and name.endswith(p)) for p in patterns):
            chosen.append(item)
    return chosen


def main(argv: list[str] | None = None) -> int:
    """Верхняя сетка: наружу выходит код выхода, а не трейсбек.

    stdout и stderr скрипта уходят в контекст субагента — в тот самый поток,
    ради разгрузки которого весь инструмент и написан. Трейсбек там бесполезен,
    а код 1 при нём ещё и врёт: он означает «часть файлов скачана», хотя не
    скачано ничего. SystemExit от argparse и KeyboardInterrupt наследуют
    BaseException и сквозь эту сетку проходят, как и положено.
    """
    try:
        return _run(argv)
    except AttachmentError as error:
        print(f"ошибка: {error}", file=sys.stderr)
        return error.exit_code
    except Exception as error:
        # Тип, а не str(error): текст чужого исключения способен нести сырой
        # netloc — ровно то, против чего написаны _safe() и _safe_url().
        print(f"ошибка: неожиданный сбой — {type(error).__name__}", file=sys.stderr)
        return EXIT_PARTIAL


def _run(argv: list[str] | None = None) -> int:
    options = _parser().parse_args(argv)

    # --url и --token учитываются только парой: resolve() требует оба сразу,
    # поэтому в одиночку они молча отбрасываются, и поиск уходит в конфиг —
    # возможно, на совсем другой хост, чем просил пользователь.
    if bool(options.url) != bool(options.token):
        print(
            "ошибка: --url и --token задаются только вместе — "
            "поодиночке они игнорируются, и креды взялись бы из конфига, "
            "возможно для другого хоста.",
            file=sys.stderr,
        )
        return EXIT_NO_CREDENTIALS

    product = "jira" if options.mode == "jira" else "confluence"

    try:
        # Конфиг MCP ищется от текущей директории, а не от --dir:
        # --dir говорит, куда складывать файлы, а не где искать креды.
        credentials = resolve(
            product,
            url=options.url,
            token=options.token,
            environ=os.environ,
            start_dir=pathlib.Path.cwd(),
        )
    except AttachmentError as error:
        print(f"ошибка: {error}", file=sys.stderr)
        return error.exit_code

    if options.explain_auth:
        # Две строки, как в дизайне: источник с путём до записи и схемой — и
        # отдельно адрес, по которому реально пойдут запросы.
        print(f"auth: {credentials.source}\n      url={_safe_url(credentials.url)}")

    client = Client(credentials, insecure=options.insecure)
    base = pathlib.Path(options.directory) if options.directory else base_dir(pathlib.Path.cwd())

    try:
        if product == "jira":
            key = parse_jira(options.target)
            attachments = client.jira_attachments(key)
            target_dir = jira_dir(base, key)
            label = key
        else:
            page_id, title = client.confluence_page(parse_confluence(options.target))
            attachments = client.confluence_attachments(page_id)
            target_dir = wiki_dir(base, page_id, title)
            label = f"{page_id} {title}"
    except AttachmentError as error:
        print(f"ошибка: {error}", file=sys.stderr)
        return error.exit_code

    found = len(attachments)
    attachments = select(attachments, options.only)
    if not attachments:
        if found:
            # Пустой результат при непустом тикете бывает только от фильтра.
            # Сказать здесь «вложений нет» значит соврать про содержимое тикета
            # в единственном канале, которым скрипт разговаривает с моделью:
            # скилл положит строку в контекст, и пользователь услышит, что
            # вложений нет вовсе.
            print(
                f"{label}: {found} {_plural(found, 'вложение', 'вложения', 'вложений')}, "
                f"под фильтр «{options.only}» не подошло ни одно"
            )
        else:
            print(f"{label}: вложений нет")
        return EXIT_OK

    # O_NOFOLLOW в client.py защищает только последний компонент пути.
    # Симлинк на месте самого каталога увёл бы всю запись наружу.
    if target_dir.is_symlink():
        print(f"ошибка: целевой каталог — симлинк: {target_dir}", file=sys.stderr)
        return EXIT_NOT_FOUND

    # А эта проверка смотрит на путь целиком. is_symlink() видит только
    # последний компонент: симлинк на docs/ или на docs/jira-attachments/
    # проходит мимо неё, mkdir(parents=True) идёт сквозь него, и запись уезжает
    # за пределы дерева — с перезаписью того, что там лежало. Имена файлов при
    # этом задаёт тот, кто прикрепляет вложения к тикету, а симлинк приезжает
    # вместе с чужим клоном: git их хранит. resolve() разворачивает симлинки на
    # всех компонентах, поэтому сравниваем уже развёрнутые пути. С --dir базой
    # становится названный пользователем каталог, так что явное указание пути
    # продолжает работать.
    base_real = base.resolve()
    target_real = target_dir.resolve()
    if target_real != base_real and base_real not in target_real.parents:
        print(
            f"ошибка: целевой каталог уводит за пределы {base_real}: {target_dir}",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        # Чаще всего это FileExistsError: на месте каталога лежит обычный файл.
        print(
            f"ошибка: не создать каталог {target_dir}: {error.strerror}",
            file=sys.stderr,
        )
        return EXIT_NOT_FOUND

    taken: set[str] = set()
    rows, failures, total = [], [], 0

    for item in attachments:
        destination = unique_path(target_dir, item.filename, item.id, taken)
        try:
            state = client.download(item, destination, force=options.force)
        except AttachmentError as error:
            failures.append(str(error))
            continue
        size = item.size
        if state == "rebuilt":
            # В метаданных лежит размер хранимого файла, а на диск лёг другой.
            # В таблице должно стоять то, что увидит ls: по ней агент решает,
            # что открывать.
            try:
                size = destination.stat().st_size
            except OSError:
                pass
        total += size
        rows.append((item.id, size, item.mime, destination.name, state))

    try:
        shown = target_dir.relative_to(pathlib.Path.cwd())
    except ValueError:
        shown = target_dir

    print(
        f"{label} → {shown}/   "
        f"({len(rows)} {_files_word(len(rows))}, {total / 1024 / 1024:.2f} MiB)"
    )
    for attachment_id, size, mime, name, state in rows:
        mark = _MARK.get(state, "")
        print(
            f"  {attachment_id:>10}  {size:>12,}  "
            f"{_short_mime(mime):<{_MIME_COLUMN}}  {name}{mark}"
        )

    for problem in failures:
        print(f"не скачано: {problem}", file=sys.stderr)

    return EXIT_PARTIAL if failures else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
