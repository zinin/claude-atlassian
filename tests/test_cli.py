import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
from contextlib import redirect_stderr, redirect_stdout

from atlassian_attachments.__main__ import (
    _files_word,
    _safe_url,
    _short_mime,
    main,
    select,
)
from atlassian_attachments.client import Attachment
from atlassian_attachments.errors import (
    EXIT_NETWORK,
    EXIT_NO_CREDENTIALS,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_PARTIAL,
)

FILES = {
    "/rest/api/2/issue/PROJ-1": {
        "fields": {
            "attachment": [
                {
                    "id": "1",
                    "filename": "trace.log",
                    "size": 5,
                    "mimeType": "text/plain",
                    "content": "/secure/attachment/1/trace.log",
                },
                {
                    "id": "2",
                    "filename": "shot.png",
                    "size": 5,
                    "mimeType": "image/png",
                    "content": "/secure/attachment/2/shot.png",
                },
            ]
        }
    },
    # Второй тикет для частичного отказа: «good» отдаётся, «denied» — 403.
    "/rest/api/2/issue/PROJ-2": {
        "fields": {
            "attachment": [
                {
                    "id": "3",
                    "filename": "good.log",
                    "size": 5,
                    "mimeType": "text/plain",
                    "content": "/secure/attachment/3/good.log",
                },
                {
                    "id": "4",
                    "filename": "denied.bin",
                    "size": 5,
                    "mimeType": "application/octet-stream",
                    "content": "/secure/attachment/deny/denied.bin",
                },
            ]
        }
    },
    # Тикет вообще без вложений: вторая ветка того же условия.
    "/rest/api/2/issue/PROJ-3": {"fields": {"attachment": []}},
    # Вложение, которое сервер отдаёт пересобранным на лету: chunked, без
    # Content-Length, и байт больше, чем в метаданных. Так Confluence Server
    # отдаёт JPEG.
    "/rest/api/2/issue/PROJ-4": {
        "fields": {
            "attachment": [
                {
                    "id": "5",
                    "filename": "photo.jpg",
                    "size": 5,
                    "mimeType": "image/jpeg",
                    "content": "/secure/attachment/rebuilt/photo.jpg",
                }
            ]
        }
    },
}

REBUILT_BODY = b"rebuilt bytes"

PAGE_ID = "123456789"
PAGE_TITLE = "120.4.7. Сводный отчёт"
LOOPING_PAGE_ID = "999"
# Ссылка на вторую страницу вложений — в том виде, в каком её отдаёт Confluence.
NEXT_LINK = f"/rest/api/content/{PAGE_ID}/child/attachment?limit=200&start=200&expand=extensions"


def _attachment(number: str, name: str, mime: str) -> dict:
    return {
        "id": f"att{number}",
        "title": name,
        "extensions": {"fileSize": 5, "mediaType": mime},
        "_links": {"download": f"/download/attachments/{PAGE_ID}/{name}?version=1"},
    }


PAGES = {
    PAGE_ID: {"id": PAGE_ID, "title": PAGE_TITLE},
    LOOPING_PAGE_ID: {"id": LOOPING_PAGE_ID, "title": "Страница с петлёй"},
}

ATTACHMENT_PAGES = {
    # Первая страница выдачи отдаёт _links.next, вторая — нет.
    "": {
        "results": [_attachment("1", "form.docx", "application/msword")],
        "_links": {"next": NEXT_LINK},
    },
    "200": {
        "results": [_attachment("2", "diagram.png", "image/png")],
        "_links": {},
    },
}

# Все запросы по порядку: тесты пагинации смотрят не только на файлы на диске,
# но и на то, по какому адресу ушёл второй запрос.
REQUESTS: list[str] = []


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        REQUESTS.append(self.path)
        path, _, raw_query = self.path.partition("?")
        query = urllib.parse.parse_qs(raw_query)

        if path in FILES:
            body = json.dumps(FILES[path]).encode("utf-8")
        elif path == "/rest/api/content" and query.get("spaceKey"):
            # Поиск страницы по пространству и заголовку.
            found = [p for p in PAGES.values() if p["title"] == query.get("title", [""])[0]]
            body = json.dumps({"results": found}).encode("utf-8")
        elif path == f"/rest/api/content/{LOOPING_PAGE_ID}/child/attachment":
            # Прокси, вернувший ссылку на уже пройденный путь. Без защиты от
            # петли обход не кончился бы никогда.
            body = json.dumps(
                {"results": [_attachment("9", "loop.txt", "text/plain")],
                 "_links": {"next": self.path}}
            ).encode("utf-8")
        elif path == f"/rest/api/content/{PAGE_ID}/child/attachment":
            body = json.dumps(
                ATTACHMENT_PAGES[query.get("start", [""])[0]]
            ).encode("utf-8")
        elif path.startswith("/rest/api/content/") and path.count("/") == 4:
            page = PAGES.get(path.rsplit("/", 1)[-1])
            if page is None:
                self.send_error(404)
                return
            body = json.dumps(page).encode("utf-8")
        elif path.startswith("/download/attachments/"):
            body = b"bytes"
        elif path.startswith("/secure/attachment/rebuilt/"):
            # Тело собирается на лету и длиннее заявленного. Ветка обязана
            # стоять выше общего обработчика вложений, как и «deny».
            self.protocol_version = "HTTP/1.1"
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(
                f"{len(REBUILT_BODY):x}\r\n".encode() + REBUILT_BODY + b"\r\n0\r\n\r\n"
            )
            self.close_connection = True
            return
        elif path.startswith("/secure/attachment/deny/"):
            # Проверка ветки отказа: до этой строки порядок важен, иначе
            # запрос перехватит общий обработчик вложений ниже.
            self.send_error(403)
            return
        elif path.startswith("/secure/attachment/"):
            body = b"bytes"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            Attachment("1", "trace.log", 5, "text/plain", "u"),
            Attachment("2", "shot.png", 5, "image/png", "u"),
        ]

    def test_no_filter_keeps_everything(self):
        self.assertEqual(len(select(self.items, None)), 2)

    def test_mime_prefix(self):
        self.assertEqual([a.filename for a in select(self.items, "image")], ["shot.png"])

    def test_extension(self):
        self.assertEqual([a.filename for a in select(self.items, ".log")], ["trace.log"])

    def test_comma_separated(self):
        self.assertEqual(len(select(self.items, "image,.log")), 2)


class TableTest(unittest.TestCase):
    OFFICE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

    def test_short_mime_is_left_alone(self):
        self.assertEqual(_short_mime("image/png"), "image/png")

    def test_long_mime_fits_the_column(self):
        # Иначе имя файла уезжает из колонки на каждом docx.
        self.assertEqual(len(_short_mime(self.OFFICE)), 38)

    def test_office_types_stay_distinguishable(self):
        # Обрезка справа склеила бы docx, xlsx и pptx: общее начало — 45 символов.
        sheet = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        self.assertNotEqual(_short_mime(self.OFFICE), _short_mime(sheet))

    def test_plural_agreement(self):
        got = [_files_word(n) for n in (0, 1, 2, 5, 11, 12, 21, 22, 25, 101)]
        self.assertEqual(
            got,
            [
                "файлов",
                "файл",
                "файла",
                "файлов",
                "файлов",
                "файлов",
                "файл",
                "файла",
                "файлов",
                "файл",
            ],
        )


class _Served(unittest.TestCase):
    """Локальный http.server и временный каталог: общая обвязка для CLI-тестов."""

    def setUp(self):
        REQUESTS.clear()
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # server_close зарегистрирован раньше, а значит отработает вторым:
        # сокет закрывается уже после остановки serve_forever, иначе прогон
        # засоряется ResourceWarning про незакрытый сокет.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        host, port = self.server.server_address
        self.base = f"http://{host}:{port}"

        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_cli(self, *extra, target="PROJ-1", mode="jira", url=None, directory=None):
        # Оба потока, а не только stdout: сообщения об ошибках уходят на stderr,
        # и без перехвата они сыпались бы в вывод самого прогона тестов.
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(
                [
                    mode,
                    target,
                    "--url",
                    url or self.base,
                    "--token",
                    "secret-token",
                    "--dir",
                    directory or str(self.root),
                    *extra,
                ]
            )
        return code, out.getvalue(), err.getvalue()


class CliTest(_Served):
    def test_downloads_into_the_expected_layout(self):
        code, output, _ = self.run_cli()
        self.assertEqual(code, EXIT_OK)
        folder = self.root / "docs" / "jira-attachments" / "PROJ-1"
        self.assertEqual((folder / "trace.log").read_bytes(), b"bytes")
        self.assertEqual((folder / "shot.png").read_bytes(), b"bytes")
        self.assertIn("trace.log", output)
        self.assertIn("PROJ-1", output)
        self.assertIn("2 файла", output)

    def test_output_never_contains_the_token(self):
        # Обе трубы: stdout уезжает в контекст модели, stderr — в лог и на
        # экран. Токену не место ни там, ни там.
        _, output, errors = self.run_cli()
        self.assertNotIn("secret-token", output)
        self.assertNotIn("secret-token", errors)

    def test_only_filter(self):
        code, _, _ = self.run_cli("--only", "image")
        self.assertEqual(code, EXIT_OK)
        folder = self.root / "docs" / "jira-attachments" / "PROJ-1"
        self.assertTrue((folder / "shot.png").exists())
        self.assertFalse((folder / "trace.log").exists())

    def test_filter_matching_nothing_does_not_claim_the_ticket_is_empty(self):
        # «Вложений нет» на тикете, где их два, — ложь в единственном канале,
        # которым скрипт разговаривает с моделью: скилл положит эту строку в
        # контекст, и пользователь услышит, что в тикете пусто.
        code, output, _ = self.run_cli("--only", "video")
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("вложений нет", output)
        self.assertIn("2", output)
        self.assertIn("video", output)

    def test_a_body_the_server_rebuilt_is_kept_and_marked(self):
        # Прежде такое вложение скачивалось целиком и удалялось, а прогон давал
        # код 1. На Confluence Server под это подпадал КАЖДЫЙ JPEG.
        code, output, errors = self.run_cli(target="PROJ-4")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(errors, "")
        folder = self.root / "docs" / "jira-attachments" / "PROJ-4"
        self.assertEqual((folder / "photo.jpg").read_bytes(), REBUILT_BODY)
        self.assertIn("пересобран сервером", output)
        # В строке стоит размер, который увидит ls, а не тот, что в метаданных.
        self.assertIn(f"{len(REBUILT_BODY)}", output)

    def test_a_ticket_without_attachments_says_so_plainly(self):
        # Вторая ветка того же условия: здесь «вложений нет» — правда.
        code, output, _ = self.run_cli(target="PROJ-3")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("вложений нет", output)

    def test_url_without_token_is_rejected(self):
        # Молчаливое падение в конфиг при половине пары опаснее ошибки:
        # пользователь думает, что работает с указанным хостом, а скрипт
        # берёт креды другого.
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(["jira", "PROJ-1", "--url", self.base, "--dir", str(self.root)])
        self.assertEqual(code, EXIT_NO_CREDENTIALS)

    def test_token_without_url_is_rejected(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(["jira", "PROJ-1", "--token", "x", "--dir", str(self.root)])
        self.assertEqual(code, EXIT_NO_CREDENTIALS)

    def test_missing_credentials_exit_code(self):
        # Изоляция обязательна: main() ищет конфиг MCP от Path.cwd(), а в рабочей
        # копии лежит .mcp.json с настоящими токенами. Без подмены cwd, home и
        # окружения этот тест нашёл бы боевые креды и не увидел бы ошибки.
        empty_home = self.root / "home"
        empty_home.mkdir()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        buffer = io.StringIO()
        with unittest.mock.patch.object(
            pathlib.Path, "cwd", classmethod(lambda cls: elsewhere)
        ), unittest.mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: empty_home)
        ), unittest.mock.patch.dict(
            os.environ, {}, clear=True
        ), redirect_stdout(buffer), redirect_stderr(io.StringIO()):
            code = main(["jira", "PROJ-1", "--dir", str(self.root)])
        self.assertEqual(code, EXIT_NO_CREDENTIALS)


class WikiCliTest(_Served):
    """Сквозной путь Confluence: страница → папка → файлы на диске.

    Через сокет он не был проверен ни разу, а из четырёх находок финального
    ревью три сидели именно на нём.
    """

    def folder(self, name=f"{PAGE_ID}-120.4.7.-Сводный-отчёт"):
        return self.root / "docs" / "wiki-attachments" / name

    def test_downloads_a_page_into_an_id_prefixed_folder(self):
        code, output, _ = self.run_cli(mode="wiki", target=PAGE_ID)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((self.folder() / "form.docx").read_bytes(), b"bytes")
        self.assertIn(PAGE_ID, output)
        self.assertIn("Сводный отчёт", output)

    def test_pagination_collects_both_pages_from_the_named_address(self):
        # Вторая страница выдачи существует только в _links.next: без обхода
        # пагинации половина вложений тихо не скачалась бы.
        code, _, _ = self.run_cli(mode="wiki", target=PAGE_ID)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((self.folder() / "form.docx").read_bytes(), b"bytes")
        self.assertEqual((self.folder() / "diagram.png").read_bytes(), b"bytes")
        self.assertIn(NEXT_LINK, REQUESTS)

    def test_a_next_link_pointing_at_itself_does_not_loop(self):
        code, _, _ = self.run_cli(mode="wiki", target=LOOPING_PAGE_ID)
        self.assertEqual(code, EXIT_OK)
        listings = [p for p in REQUESTS if "child/attachment" in p]
        self.assertEqual(len(listings), 1)

    def test_a_renamed_page_keeps_writing_into_its_old_folder(self):
        # Id идёт первым и не меняется: переименованная страница находится по
        # префиксу «<id>-» и не заводит рядом вторую папку.
        old = self.folder(f"{PAGE_ID}-Прежний-заголовок")
        old.mkdir(parents=True)

        code, _, _ = self.run_cli(mode="wiki", target=PAGE_ID)

        self.assertEqual(code, EXIT_OK)
        self.assertEqual((old / "form.docx").read_bytes(), b"bytes")
        self.assertEqual(
            [p.name for p in (self.root / "docs" / "wiki-attachments").iterdir()],
            [old.name],
        )

    def test_a_page_named_by_space_and_title_is_found(self):
        code, output, _ = self.run_cli(mode="wiki", target=f"DOCS:{PAGE_TITLE}")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual((self.folder() / "form.docx").read_bytes(), b"bytes")
        self.assertIn(PAGE_ID, output)

    def test_a_missing_page_is_not_found(self):
        code, _, errors = self.run_cli(mode="wiki", target="404404")
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertNotIn("secret-token", errors)


class ExplainAuthTest(unittest.TestCase):
    """--explain-auth не должен печатать токен, спрятанный в самом URL.

    «https://svc:PAT@jira/…» в JIRA_URL встречается регулярно — ради этого в
    client.py написан _safe(). stdout уходит в контекст модели, то есть ровно в
    тот поток, ради обхода которого написан весь инструмент, так что живому PAT
    там не место даже по отладочному флагу.
    """

    TOKEN_IN_URL = "PAT-SECRET-42"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_userinfo_is_stripped_but_the_host_survives(self):
        # Идентификатор заведомо мусорный: строка auth печатается до любого
        # обращения к сети, а разбор ключа падает сразу за ней. Так тест не
        # ходит в сеть вообще, а не полагается на то, что запрос быстро умрёт.
        environment = {
            "JIRA_URL": f"https://svc:{self.TOKEN_IN_URL}@jira.example.com/jira",
            "JIRA_PERSONAL_TOKEN": "personal-token-value",
        }
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(
            out
        ), redirect_stderr(err):
            main(["jira", "не-ключ", "--explain-auth", "--dir", str(self.root)])

        shown = out.getvalue()
        self.assertNotIn(self.TOKEN_IN_URL, shown)
        self.assertNotIn(self.TOKEN_IN_URL, err.getvalue())
        self.assertNotIn("personal-token-value", shown)
        # Без этой половины тест выродился бы в проверку пустой строки: флаг
        # обязан по-прежнему отвечать, с какого хоста работаем и откуда креды.
        self.assertIn("jira.example.com", shown)
        self.assertIn("env JIRA_*", shown)

    def test_it_names_the_config_entry_and_the_scheme_but_no_values(self):
        # Флаг запускают, когда скрипт работает не с тем инстансом или не
        # работает вовсе: _config_paths идёт вверх до корня ФС, а _server_entries
        # обходит все проекты в ~/.claude.json, так что победить может запись из
        # чужого проекта. Один только путь к файлу на вопрос «чей это конфиг»
        # не отвечает.
        home = self.root / "home"
        home.mkdir()
        (home / ".claude.json").write_text(
            json.dumps(
                {
                    "projects": {
                        "/some/other/project": {
                            "mcpServers": {
                                "mcp-atlassian": {
                                    "args": [
                                        "--jira-url=https://jira.example",
                                        "--jira-personal-token=PAT-SECRET-42",
                                    ]
                                }
                            }
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()

        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(
            pathlib.Path, "cwd", classmethod(lambda cls: elsewhere)
        ), unittest.mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: home)
        ), unittest.mock.patch.dict(
            os.environ, {}, clear=True
        ), redirect_stdout(out), redirect_stderr(err):
            main(["jira", "не-ключ", "--explain-auth", "--dir", str(self.root)])

        shown = out.getvalue()
        self.assertIn(".claude.json", shown)
        self.assertIn('projects["/some/other/project"]', shown)
        self.assertIn('mcpServers["mcp-atlassian"].args', shown)
        self.assertIn("Bearer", shown)
        self.assertIn("url=https://jira.example", shown)
        self.assertNotIn("PAT-SECRET-42", shown + err.getvalue())

    def test_safe_url_keeps_scheme_host_port_and_path(self):
        self.assertEqual(
            _safe_url("https://svc:PAT@jira.example.com:8443/jira?os_username=svc"),
            "https://jira.example.com:8443/jira",
        )

    def test_safe_url_leaves_a_clean_url_alone(self):
        self.assertEqual(_safe_url("https://jira.example.com"), "https://jira.example.com")

    def test_safe_url_refuses_to_echo_an_unparsable_address(self):
        # urlsplit читает «svc» как схему, а «PAT@host/jira» оставляет в path,
        # так что userinfo не срезается и токен уходит в печать. Разобрать такой
        # адрес мы не можем — значит показывать из него нельзя ничего. Ирония
        # злая: с таким URL инструмент не работает вообще, а --explain-auth это
        # ровно то, что запускают первым, когда он не работает.
        shown = _safe_url("svc:PAT-SECRET-42@jira.example.com/jira")
        self.assertNotIn("PAT-SECRET-42", shown)
        self.assertNotIn("jira.example.com", shown)

    def test_safe_url_survives_an_address_urlsplit_refuses(self):
        # Битый IPv6 роняет сам разбор, а --explain-auth запускают именно тогда,
        # когда с адресом что-то не так.
        self.assertEqual(_safe_url("https://[::1/x"), "<адрес не разобран>")

    def test_safe_url_survives_a_broken_port(self):
        # urlsplit.port бросает ValueError на нечисловом порте; молчать об этом
        # нельзя падением всего запуска ради отладочной строки.
        self.assertNotIn("PAT", _safe_url("https://svc:PAT@jira.example.com:порт/x"))


class ExitCodeTest(_Served):
    """Коды выхода, которые раньше проверялись только руками.

    Двенадцать ручных сценариев ушли вместе с /tmp — здесь остаются те,
    что спека называет гарантиями.
    """

    def test_partial_failure_keeps_what_was_downloaded(self):
        # Названная спекой гарантия: часть упала — скачанное остаётся на диске,
        # а код выхода отличается и от полного успеха, и от «ничего не вышло».
        code, output, errors = self.run_cli(target="PROJ-2")
        self.assertEqual(code, EXIT_PARTIAL)
        folder = self.root / "docs" / "jira-attachments" / "PROJ-2"
        self.assertEqual((folder / "good.log").read_bytes(), b"bytes")
        self.assertFalse((folder / "denied.bin").exists())
        # Ни .part, ни пустышки на месте упавшего файла.
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["good.log"])
        self.assertIn("good.log", output)
        self.assertIn("403", errors)

    def test_symlinked_target_directory_is_refused(self):
        # O_NOFOLLOW в client.py стережёт только последний компонент пути;
        # симлинк на месте самого каталога увёл бы всю запись наружу.
        outside = pathlib.Path(self._tmp.name) / "outside"
        outside.mkdir()
        planted = self.root / "docs" / "jira-attachments"
        planted.mkdir(parents=True)
        (planted / "PROJ-1").symlink_to(outside, target_is_directory=True)

        code, _, errors = self.run_cli()
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIn("симлинк", errors)

    def test_a_symlinked_parent_directory_cannot_lead_the_write_outside(self):
        # Третий обход симлинк-защиты: is_symlink() смотрит только на последний
        # компонент пути. Симлинк выше — на docs/ или docs/jira-attachments/ —
        # она не видит, mkdir(parents=True) идёт сквозь него, а O_NOFOLLOW в
        # client.py стережёт имя файла, а не каталог. Запись уезжала за пределы
        # дерева и молча затирала чужой файл, с кодом выхода 0. Сценарий не
        # теоретический: git хранит симлинки, значит подмена приезжает вместе с
        # чужим клоном, а имена перезаписываемых файлов задаёт тот, кто
        # прикрепляет вложения к тикету.
        outside_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(outside_tmp.cleanup)
        outside = pathlib.Path(outside_tmp.name)
        (outside / "PROJ-1").mkdir()
        victim = outside / "PROJ-1" / "trace.log"
        victim.write_bytes(b"MY REAL KEY")

        (self.root / "docs").mkdir()
        (self.root / "docs" / "jira-attachments").symlink_to(
            outside, target_is_directory=True
        )

        code, _, errors = self.run_cli()

        self.assertEqual(victim.read_bytes(), b"MY REAL KEY")
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertIn("за пределы", errors)

    def test_a_file_where_the_target_directory_belongs_is_reported(self):
        # Иначе mkdir() бросал FileExistsError мимо всей иерархии кодов выхода.
        planted = self.root / "docs" / "jira-attachments"
        planted.mkdir(parents=True)
        (planted / "PROJ-1").write_text("не каталог", encoding="utf-8")

        code, _, errors = self.run_cli()

        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertIn("не создать каталог", errors)

    def test_an_unexpected_failure_never_leaves_a_traceback(self):
        # Битый IPv6 в базовом URL роняет urlsplit ValueError'ом. Без верхней
        # сетки трейсбек уходил в stdout/stderr субагента — в тот самый поток,
        # ради разгрузки которого написан весь инструмент, — а код 1 при этом
        # врал: он означает «часть файлов скачана».
        code, output, errors = self.run_cli(url="https://[::1/")

        self.assertEqual(code, EXIT_PARTIAL)
        self.assertIn("неожиданный сбой", errors)
        self.assertNotIn("Traceback", errors)
        self.assertNotIn("secret-token", errors + output)

    def test_unparsable_identifier_is_not_found(self):
        code, _, errors = self.run_cli(target="не-ключ")
        self.assertEqual(code, EXIT_NOT_FOUND)
        self.assertIn("не понял идентификатор", errors)

    def test_unreachable_host_is_a_network_error(self):
        # Порт 1 без root занять нельзя, так что соединение гарантированно
        # отвергается сразу и локально — в сеть тест не ходит.
        code, _, errors = self.run_cli(url="http://127.0.0.1:1")
        self.assertEqual(code, EXIT_NETWORK)
        self.assertIn("сеть недоступна", errors)


class LauncherTest(unittest.TestCase):
    def test_launcher_runs_and_prints_help(self):
        launcher = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "atlassian-attachments.py"
        finished = subprocess.run(
            [sys.executable, str(launcher), "--help"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(finished.returncode, 0)
        self.assertIn("jira", finished.stdout)
        self.assertIn("wiki", finished.stdout)


if __name__ == "__main__":
    unittest.main()
