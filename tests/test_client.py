import http.server
import os
import pathlib
import ssl
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request

from atlassian_attachments import client
from atlassian_attachments.client import (
    Attachment,
    Client,
    parse_confluence_attachments,
    parse_jira_attachments,
)
from atlassian_attachments.credentials import Credentials
from atlassian_attachments.errors import AttachmentError, NetworkError

JIRA_PAYLOAD = {
    "fields": {
        "attachment": [
            {
                "id": "100042",
                "filename": "data-import.log",
                "size": 1409583,
                "mimeType": "text/plain",
                "content": "https://jira.example/secure/attachment/100042/data-import.log",
            },
            {
                "id": "100045",
                "filename": "Выгрузка.zip",
                "size": 3253,
                "mimeType": "application/zip",
                "content": "https://jira.example/secure/attachment/100045/Vygruzka.zip",
            },
        ]
    }
}

CONFLUENCE_PAYLOAD = {
    "results": [
        {
            "id": "987654321",
            "title": "Сводный отчёт за квартал.docx",
            "extensions": {
                "fileSize": 36226,
                "mediaType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            },
            "_links": {"download": "/download/attachments/123456789/form.docx?version=1"},
        }
    ]
}


class ParseTest(unittest.TestCase):
    def test_jira_payload(self):
        result = parse_jira_attachments(JIRA_PAYLOAD, "https://jira.example")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].id, "100042")
        self.assertEqual(result[0].filename, "data-import.log")
        self.assertEqual(result[0].size, 1409583)
        self.assertEqual(result[0].mime, "text/plain")
        self.assertTrue(result[0].url.startswith("https://jira.example/secure/attachment/"))
        self.assertEqual(result[1].filename, "Выгрузка.zip")

    def test_jira_payload_without_attachments(self):
        self.assertEqual(parse_jira_attachments({"fields": {}}, "https://jira.example"), [])

    def test_confluence_payload_makes_download_url_absolute(self):
        result = parse_confluence_attachments(CONFLUENCE_PAYLOAD, "https://wiki.example")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "987654321")
        self.assertEqual(result[0].size, 36226)
        self.assertEqual(
            result[0].url,
            "https://wiki.example/download/attachments/123456789/form.docx?version=1",
        )

    def test_confluence_cloud_uses_the_v1_download_endpoint(self):
        # Cloud убрал легаси-эндпоинт, на который _links.download продолжает
        # указывать: метаданные приходят, а сам файл отдаёт 401. Список печатался
        # бы целиком, а на диск не ложилось бы ничего.
        result = parse_confluence_attachments(
            CONFLUENCE_PAYLOAD, "https://acme.atlassian.net/wiki", "123456789", cloud=True
        )
        self.assertEqual(
            result[0].url,
            "https://acme.atlassian.net/wiki/rest/api/content/123456789"
            "/child/attachment/987654321/download?version=1",
        )

    def test_server_keeps_the_legacy_download_link(self):
        # На Server/DC легаси-эндпоинт живой, и подменять его нечем: v1 там
        # есть не везде. Поведение обязано остаться прежним.
        result = parse_confluence_attachments(
            CONFLUENCE_PAYLOAD, "https://wiki.example", "123456789", cloud=False
        )
        self.assertEqual(
            result[0].url,
            "https://wiki.example/download/attachments/123456789/form.docx?version=1",
        )

    def test_an_attachment_without_an_address_gets_no_url(self):
        # _absolute(base, "") — это сам базовый URL: без этой ветки на диск
        # молча ложилась бы главная страница под именем вложения.
        jira = parse_jira_attachments(
            {"fields": {"attachment": [{"id": "1", "filename": "x.log", "size": 0}]}},
            "https://jira.example",
        )
        self.assertEqual(jira[0].url, "")
        wiki = parse_confluence_attachments(
            {"results": [{"id": "att1", "title": "x.log"}]}, "https://wiki.example"
        )
        self.assertEqual(wiki[0].url, "")

    def test_a_non_numeric_size_is_zero_and_not_a_crash(self):
        # Размер приходит от сервера; mcp-atlassian оборачивает свой int()
        # так же (models/jira/common.py:334) — значит, кривые значения им
        # попадались. Трейсбек посреди скачивания хуже нулевого размера.
        jira = parse_jira_attachments(
            {
                "fields": {
                    "attachment": [
                        {"id": "1", "filename": "x.log", "size": "12.3 kB", "content": "/a"}
                    ]
                }
            },
            "https://jira.example",
        )
        self.assertEqual(jira[0].size, 0)
        wiki = parse_confluence_attachments(
            {
                "results": [
                    {
                        "id": "att1",
                        "title": "x.log",
                        "extensions": {"fileSize": "36 kB"},
                        "_links": {"download": "/a"},
                    }
                ]
            },
            "https://wiki.example",
        )
        self.assertEqual(wiki[0].size, 0)


class _Handler(http.server.BaseHTTPRequestHandler):
    body = b"payload"
    seen_auth = []
    seen_paths = []
    seen_accept = []
    redirect_to = ""

    def do_GET(self):
        self.seen_auth.append(self.headers.get("Authorization"))
        self.seen_paths.append(self.path)
        self.seen_accept.append(self.headers.get("Accept"))
        # startswith, а не ==: путь приходит вместе с query, а один из тестов
        # кладёт в query секрет и смотрит, что тот не всплыл в тексте ошибки.
        if self.path.startswith("/missing"):
            self.send_error(404, "not found")
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", self.redirect_to)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class _Elsewhere(http.server.BaseHTTPRequestHandler):
    """Второй хост. Сюда уводит редирект, и сюда не должно приехать ничего секретного."""

    body = b"y" * 1234
    seen_auth = []

    def do_GET(self):
        self.seen_auth.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class _Rebuilder(http.server.BaseHTTPRequestHandler):
    """Сервер, который собирает тело на лету: chunked, без Content-Length.

    Так Confluence Server отдаёт JPEG — пересжатыми, поэтому байт всегда не
    столько, сколько в extensions.fileSize, и сверять записанное не с чем.
    Целость такого потока стережёт само обрамление: без завершающего нулевого
    куска http.client бросает IncompleteRead.
    """

    protocol_version = "HTTP/1.1"
    body = b"z" * 1500
    truncate = False

    def do_GET(self):
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        # Сервер в тестах однопоточный, а HTTP/1.1 по умолчанию держит
        # соединение открытым: без Connection: close следующий тест ждал бы
        # освобождения сокета.
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(f"{len(self.body):x}\r\n".encode() + self.body + b"\r\n")
        if not self.truncate:
            self.wfile.write(b"0\r\n\r\n")
        self.close_connection = True

    def log_message(self, *args):
        pass


def _start(handler):
    """Поднимает локальный сервер на свободном порту и возвращает (сервер, базовый URL)."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


class DownloadTest(unittest.TestCase):
    def setUp(self):
        _Handler.seen_auth = []
        _Handler.seen_paths = []
        _Handler.seen_accept = []
        _Handler.body = b"x" * 1234
        self.server, self.base = _start(_Handler)
        # Уборка идёт в обратном порядке: сначала shutdown() останавливает цикл,
        # потом server_close() закрывает слушающий сокет. Без второго каждый тест
        # оставляет сокет висеть, и прогон тонет в ResourceWarning.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.client = Client(Credentials(self.base, "Bearer test-token", "тест"))

    def attachment(self, size=1234, name="file.bin"):
        return Attachment("1", name, size, "application/octet-stream", f"{self.base}/file")

    def test_downloads_bytes_to_disk(self):
        target = self.root / "file.bin"
        self.assertEqual(self.client.download(self.attachment(), target), "downloaded")
        self.assertEqual(target.read_bytes(), b"x" * 1234)

    def test_sends_the_authorization_header(self):
        self.client.download(self.attachment(), self.root / "file.bin")
        self.assertIn("Bearer test-token", _Handler.seen_auth)

    def test_second_run_skips_a_file_of_the_same_size(self):
        target = self.root / "file.bin"
        self.client.download(self.attachment(), target)
        self.assertEqual(self.client.download(self.attachment(), target), "skipped")

    def test_force_redownloads(self):
        target = self.root / "file.bin"
        self.client.download(self.attachment(), target)
        self.assertEqual(
            self.client.download(self.attachment(), target, force=True), "downloaded"
        )

    def test_size_mismatch_is_an_error_and_leaves_no_partial_file(self):
        target = self.root / "file.bin"
        with self.assertRaises(AttachmentError):
            self.client.download(self.attachment(size=999), target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_symlink_at_the_destination_is_refused(self):
        target = self.root / "link.bin"
        target.symlink_to(self.root / "elsewhere")
        with self.assertRaises(AttachmentError):
            self.client.download(self.attachment(), target)

    def test_an_attachment_without_an_address_is_an_error(self):
        empty = Attachment("1", "file.bin", 0, "application/octet-stream", "")
        with self.assertRaises(AttachmentError):
            self.client.download(empty, self.root / "file.bin")
        self.assertFalse((self.root / "file.bin").exists())

    def test_json_asks_for_json_and_a_download_does_not(self):
        # Заголовок Accept на скачивании бинарника бессмыслен, а на WAF или
        # прокси способен обернуться 406.
        self.client.download(self.attachment(), self.root / "file.bin")
        self.assertEqual(_Handler.seen_accept, [None])
        with self.assertRaises(AttachmentError):
            self.client.jira_attachments("PROJ-1")  # сервер отдаёт не JSON
        self.assertEqual(_Handler.seen_accept[-1], "application/json")

    def test_a_non_ascii_address_fails_one_attachment_not_the_run(self):
        # В запросную строку HTTP уходит только ASCII: неэкранированная
        # кириллица в content роняла трейсбеком весь прогон.
        cyrillic = Attachment(
            "1", "файл.bin", 1234, "application/octet-stream", f"{self.base}/файл"
        )
        with self.assertRaises(AttachmentError) as caught:
            self.client.download(cyrillic, self.root / "file.bin")
        self.assertIn("неэкранированные", str(caught.exception))

    def test_http_error_is_reported_without_the_token(self):
        broken = Attachment("2", "gone.bin", 10, "text/plain", f"{self.base}/missing")
        with self.assertRaises(AttachmentError) as caught:
            self.client.download(broken, self.root / "gone.bin")
        self.assertNotIn("test-token", str(caught.exception))

    # --- симлинк на месте .part ------------------------------------------

    def test_symlink_at_the_part_file_is_refused(self):
        """Защита на цели смотрит не туда: байты принимает сосед .part."""
        outside = self.root / "outside.txt"
        outside.write_bytes(b"important pre-existing content")
        target = self.root / "file.bin"
        (self.root / "file.bin.part").symlink_to(outside)

        with self.assertRaises(AttachmentError):
            self.client.download(self.attachment(), target)

        self.assertEqual(outside.read_bytes(), b"important pre-existing content")
        self.assertFalse(target.exists())

    def test_broken_symlink_at_the_part_file_creates_nothing(self):
        victim = self.root / "victim.txt"
        target = self.root / "file.bin"
        (self.root / "file.bin.part").symlink_to(victim)

        with self.assertRaises(AttachmentError):
            self.client.download(self.attachment(), target)

        self.assertFalse(victim.exists())

    def test_a_platform_without_o_nofollow_still_downloads(self):
        """O_NOFOLLOW есть только на Unix; на Windows его в os просто нет.

        Без запасного пути флаг собирался в AttributeError, тот пролетал мимо
        except AttachmentError в цикле и клал ВЕСЬ прогон на первом же
        вложении: пустой stdout, «неожиданный сбой», ноль файлов.
        """
        target = self.root / "file.bin"
        with unittest.mock.patch.object(client, "_NOFOLLOW", 0):
            self.assertEqual(self.client.download(self.attachment(), target), "downloaded")
        self.assertEqual(target.read_bytes(), b"x" * 1234)

    def test_a_platform_without_o_nofollow_still_refuses_a_symlink(self):
        # Там, где флага нет, имя проверяется заранее: модель угроз — симлинк,
        # приехавший с чужим клоном, — закрыта и без O_NOFOLLOW.
        outside = self.root / "outside.txt"
        outside.write_bytes(b"important pre-existing content")
        target = self.root / "file.bin"
        (self.root / "file.bin.part").symlink_to(outside)

        with unittest.mock.patch.object(client, "_NOFOLLOW", 0):
            with self.assertRaises(AttachmentError):
                self.client.download(self.attachment(), target)

        self.assertEqual(outside.read_bytes(), b"important pre-existing content")
        self.assertFalse(target.exists())

    def test_a_name_at_the_length_limit_still_downloads(self):
        """Имя длиной ровно в лимит плюс «.part» уже не влезает в лимит.

        sanitize_filename такое имя пропускает — 255 байт, — а os.open по
        соседнему .part на 260 байт отвечает ENAMETOOLONG. Вложение не
        скачивалось вовсе.
        """
        name = "и" * 127 + "x"
        self.assertEqual(len(name.encode("utf-8")), 255)
        target = self.root / name
        self.assertEqual(self.client.download(self.attachment(name=name), target), "downloaded")
        self.assertEqual(target.read_bytes(), b"x" * 1234)
        self.assertEqual(list(self.root.glob("*.part")), [])

    def test_a_leftover_part_file_does_not_block_the_download(self):
        """O_TRUNC, а не O_EXCL: остаток от убитого прогона переиспользуется."""
        target = self.root / "file.bin"
        (self.root / "file.bin.part").write_bytes(b"leftover")
        self.assertEqual(self.client.download(self.attachment(), target), "downloaded")
        self.assertEqual(target.read_bytes(), b"x" * 1234)

    # --- секреты в тексте ошибки -----------------------------------------

    def test_query_is_stripped_from_the_error_text(self):
        broken = Attachment(
            "2", "gone.bin", 10, "text/plain",
            f"{self.base}/missing?os_username=bob&token=SECRET-IN-QUERY",
        )
        with self.assertRaises(AttachmentError) as caught:
            self.client.download(broken, self.root / "gone.bin")
        self.assertNotIn("SECRET-IN-QUERY", str(caught.exception))

    def test_userinfo_in_the_base_url_never_reaches_the_error_text(self):
        """Креды в JIRA_URL роняли http.client.InvalidURL — не наследника OSError,
        поэтому он шёл наружу сырым, а пароль стоял открытым текстом в его тексте.

        Хост без порта здесь обязателен: именно на нём http.client режет netloc
        по последнему двоеточию и объявляет «нечисловой порт». В сеть проба не
        ходит — InvalidURL поднимается при сборке HTTPConnection, до сокета.
        """
        leaky = Client(Credentials("http://svc:SECRET-PAT@jira.example", "Bearer t", "тест"))
        with self.assertRaises(AttachmentError) as caught:
            leaky.jira_attachments("PROJ-1")
        self.assertNotIn("SECRET-PAT", str(caught.exception))

    def test_safe_refuses_to_echo_an_address_it_could_not_parse(self):
        """Без схемы urlsplit читает «svc» как схему, а «PAT@host» оставляет в
        path — userinfo не срезается, и _safe возвращает токен обратно.

        Через публичный API этот URL сегодня до _safe не доходит: ветка URLError
        перехватывает его раньше («unknown url type»). Но контракт _safe — отдать
        строку, которую можно печатать, при любом входе, и следующий вызов,
        добавленный где угодно, унёс бы токен на stderr.
        """
        shown = Client._safe("svc:SECRET-PAT@jira.example.com/jira")
        self.assertNotIn("SECRET-PAT", shown)
        self.assertNotIn("jira.example.com", shown)

    # --- всё наружу выходит как AttachmentError ---------------------------

    def test_non_json_response_becomes_an_attachment_error(self):
        """За SSO вместо JSON приходит HTML страницы логина."""
        with self.assertRaises(AttachmentError):
            self.client.jira_attachments("PROJ-1")

    @unittest.skipIf(os.geteuid() == 0, "root игнорирует права доступа")
    def test_unwritable_directory_becomes_an_attachment_error(self):
        locked = self.root / "locked"
        locked.mkdir()
        locked.chmod(0o500)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaises(AttachmentError):
            self.client.download(self.attachment(), locked / "file.bin")

    def test_directory_at_the_destination_is_refused(self):
        target = self.root / "dir.bin"
        target.mkdir()
        with self.assertRaises(AttachmentError):
            self.client.download(self.attachment(), target)


class RebuiltBodyTest(unittest.TestCase):
    """Сервер отдал не тот файл, что хранит.

    Confluence Server пересобирает JPEG на лету: chunked, без Content-Length, и
    байт всегда не столько, сколько в fileSize. Проверено на живом Server —
    расхождение идёт в обе стороны, тело при этом полное и валидное. Сверка с
    fileSize здесь удаляла исправно скачанный файл.
    """

    def setUp(self):
        _Rebuilder.body = b"z" * 1500
        _Rebuilder.truncate = False
        self.server, self.base = _start(_Rebuilder)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.client = Client(Credentials(self.base, "Bearer test-token", "тест"))

    def attachment(self, size):
        return Attachment("1", "photo.jpg", size, "image/jpeg", f"{self.base}/file")

    def test_a_body_the_server_rebuilt_is_kept_not_discarded(self):
        target = self.root / "photo.jpg"
        self.assertEqual(self.client.download(self.attachment(1234), target), "rebuilt")
        self.assertEqual(target.read_bytes(), b"z" * 1500)

    def test_a_rebuilt_body_of_the_declared_size_is_a_plain_download(self):
        # Отметка появляется только там, где размер и правда разошёлся: иначе
        # ею была бы помечена половина Confluence.
        target = self.root / "photo.jpg"
        self.assertEqual(self.client.download(self.attachment(1500), target), "downloaded")

    def test_a_truncated_chunked_body_is_still_an_error(self):
        # То, ради чего сверка вообще стоит. Обрамление chunked ловит недокачку
        # само, поэтому отказ от сверки по fileSize ничего здесь не ослабляет.
        _Rebuilder.truncate = True
        target = self.root / "photo.jpg"
        with self.assertRaises(NetworkError):
            self.client.download(self.attachment(1500), target)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob("*.part")), [])


class NextLinkTest(unittest.TestCase):
    """Ссылка пагинации на Cloud приходит в двух формах — с /wiki и без."""

    def client(self, base):
        return Client(Credentials(base, "Bearer t", "тест"))

    def test_the_wiki_prefix_is_not_doubled(self):
        # Иначе вторая страница выдачи уходит в /wiki/wiki/rest/… и 404,
        # а половина вложений просто не скачивается — молча.
        client = self.client("https://acme.atlassian.net/wiki")
        self.assertEqual(
            client._next_path("/wiki/rest/api/content/1/child/attachment?start=200"),
            "/rest/api/content/1/child/attachment?start=200",
        )

    def test_a_link_without_the_prefix_is_left_alone(self):
        client = self.client("https://acme.atlassian.net/wiki")
        self.assertEqual(
            client._next_path("/rest/api/content/1/child/attachment?start=200"),
            "/rest/api/content/1/child/attachment?start=200",
        )

    def test_a_server_base_is_never_touched(self):
        client = self.client("https://wiki.example")
        self.assertEqual(client._next_path("/wiki/rest/api/x"), "/wiki/rest/api/x")


class RedirectTest(unittest.TestCase):
    """Куда уезжает заголовок авторизации, когда запрос уводят на сторону."""

    def setUp(self):
        _Handler.seen_auth = []
        _Handler.seen_paths = []
        _Handler.seen_accept = []
        _Handler.body = b"x" * 1234
        _Handler.redirect_to = ""
        _Elsewhere.seen_auth = []

        self.server, self.base = _start(_Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.other, self.other_base = _start(_Elsewhere)
        self.addCleanup(self.other.server_close)
        self.addCleanup(self.other.shutdown)

        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.client = Client(Credentials(self.base, "Bearer secret-pat", "тест"))

    def moved(self):
        return Attachment(
            "1", "file.bin", 1234, "application/octet-stream", f"{self.base}/redirect"
        )

    def test_redirect_to_another_host_drops_the_authorization_header(self):
        _Handler.redirect_to = f"{self.other_base}/stolen"
        target = self.root / "file.bin"

        self.assertEqual(self.client.download(self.moved(), target), "downloaded")

        # Редирект действительно случился: на диске тело со второго хоста.
        self.assertEqual(target.read_bytes(), b"y" * 1234)
        # Первый хост заголовок видел — значит он вообще отправлялся.
        self.assertIn("Bearer secret-pat", _Handler.seen_auth)
        # А второй не увидел ничего. Ровно один запрос, и в нём заголовка нет.
        self.assertEqual(_Elsewhere.seen_auth, [None])

    def test_redirect_within_the_same_host_keeps_the_header(self):
        """Снимать заголовок на своём же хосте не за что — иначе сломается 302 на себя."""
        _Handler.redirect_to = f"{self.base}/file"
        self.client.download(self.moved(), self.root / "file.bin")
        self.assertEqual(_Handler.seen_auth, ["Bearer secret-pat", "Bearer secret-pat"])

    def test_url_pointing_at_another_host_is_fetched_from_ours(self):
        """content у Jira Server собран из Base URL самого Jira, а она расходится
        с JIRA_URL сплошь и рядом: реверс-прокси, split-horizon DNS, http против
        https. Отказ в этом месте клал скачивание целиком — ноль файлов. Путь и
        query остаются, схема и хост берутся наши: на чужой хост мы не ходим и
        токен туда не отправляем.
        """
        foreign = Attachment(
            "1",
            "file.bin",
            1234,
            "application/octet-stream",
            f"{self.other_base}/file?version=7",
        )
        target = self.root / "file.bin"

        self.assertEqual(self.client.download(foreign, target), "downloaded")

        # Тело — с нашего хоста, а не с чужого: у них разные байты.
        self.assertEqual(target.read_bytes(), b"x" * 1234)
        self.assertEqual(_Elsewhere.seen_auth, [])
        self.assertIn("/file?version=7", _Handler.seen_paths)
        self.assertIn("Bearer secret-pat", _Handler.seen_auth)

    def test_a_rehosted_link_can_never_land_on_a_foreign_host(self):
        # Путь берётся из чужого адреса как есть, и «//host/x» в нём — попытка
        # прочитаться как netloc. Токен уходит именно по этому адресу, так что
        # проверяем сам инвариант, а не только удачные случаи.
        for hostile in (
            f"{self.other_base}//attacker.test/x",
            f"{self.other_base}/..//x?a=b",
            "http://svc:PASSWORD@attacker.test/x",
        ):
            with self.subTest(hostile):
                rehosted = self.client._own_host(hostile)
                self.assertTrue(rehosted.startswith(f"{self.base}/"), rehosted)
                self.assertNotIn("attacker.test", rehosted.split("/", 3)[2])
                self.assertNotIn("PASSWORD", rehosted)

    def test_a_link_with_a_foreign_scheme_is_still_refused(self):
        """Перевесить можно только http(s): file: и data: остаются отказом."""
        foreign = Attachment(
            "1", "file.bin", 1234, "application/octet-stream", "file:///etc/passwd"
        )
        with self.assertRaises(AttachmentError) as caught:
            self.client.download(foreign, self.root / "file.bin")
        self.assertIn("--url", str(caught.exception))
        self.assertFalse((self.root / "file.bin").exists())


class TlsTest(unittest.TestCase):
    """TLS проверяется по умолчанию, и insecure=True — единственный способ это снять.

    Тест намеренно белоящичный: убедиться, что контекст действительно проверяет
    сертификат, иначе можно только настоящим рукопожатием с самоподписанным
    сертификатом. Инвариант того стоит — он глобальный и молча ломается.
    """

    def context(self, client):
        for handler in client._opener.handlers:
            if isinstance(handler, urllib.request.HTTPSHandler):
                return handler._context
        self.fail("в opener нет HTTPSHandler")

    def client(self, **kwargs):
        return Client(Credentials("https://jira.example", "Bearer t", "тест"), **kwargs)

    def test_certificates_are_verified_by_default(self):
        context = self.context(self.client())
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_insecure_is_the_only_way_to_disable_verification(self):
        self.assertEqual(self.context(self.client(insecure=True)).verify_mode, ssl.CERT_NONE)

    def test_the_stock_redirect_handler_is_replaced(self):
        """Иначе заголовок авторизации снова поедет за редиректом на чужой хост."""
        handlers = [
            type(h).__name__
            for h in self.client()._opener.handlers
            if isinstance(h, urllib.request.HTTPRedirectHandler)
        ]
        self.assertEqual(handlers, ["_StripAuthOnHostChange"])


if __name__ == "__main__":
    unittest.main()
