"""REST-доступ к Jira и Confluence и потоковое скачивание файлов."""

from __future__ import annotations

import contextlib
import errno
import http.client
import json
import os
import pathlib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .credentials import Credentials, is_cloud_url
from .errors import AttachmentError, NetworkError, NotFoundError
from .identifiers import PageRef
from .naming import partial_name

_CHUNK = 256 * 1024
_DEFAULT_PORT = {"http": 80, "https": 443}
# Что нельзя нести за редиректом на чужой адрес.
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization"})


@dataclass(frozen=True)
class Attachment:
    id: str
    filename: str
    size: int
    mime: str
    url: str


def _absolute(base_url: str, url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return base_url.rstrip("/") + "/" + url.lstrip("/")


def _int(value: object) -> int:
    """Число из поля, которому положено быть числом. Мусор — это 0.

    fileSize и size приходят от сервера, и кривые значения там встречаются:
    mcp-atlassian оборачивает свой int() ровно так же (models/jira/common.py:334).
    Ноль хуже настоящего размера — при нём отключается и сверка, и пропуск по
    совпадению, — но много лучше трейсбека посреди скачивания.
    """
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _origin(url: str) -> tuple[str, str, int]:
    """Схема, хост и порт — то, чем «свой» адрес отличается от чужого.

    hostname, а не сырой netloc: в netloc живут регистр и «user:pass@», из-за
    которых один и тот же хост выглядит по-разному, а чужой — знакомо.
    """
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        # Порт не число. Адресу, который сам urllib разобрать не смог, верить
        # нечего: возвращаем заведомо непригодный кортеж. Совпасть он может
        # только с таким же неразобранным адресом, а до сокета ни один из них
        # не доходит — http.client отвергает их раньше.
        return scheme, "", -1
    return scheme, parsed.hostname or "", port or _DEFAULT_PORT.get(scheme, 0)


# O_NOFOLLOW объявлен только на Unix; на Windows его в модуле os нет вовсе, и
# обращение к нему давало AttributeError. Тот пролетал мимо except AttachmentError
# в цикле скачивания и клал весь прогон на первом же вложении.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _open_partial(partial: pathlib.Path):
    """Открывает .part так, чтобы симлинк на его месте не увёл запись наружу.

    Проверка симлинка на цели смотрит не туда: байты принимает сосед .part, и
    подложенный по его имени симлинк дал бы запись в произвольный файл вне
    каталога загрузки — с затиранием того, что там лежало. O_NOFOLLOW закрывает
    это без TOCTOU: на симлинке os.open падает сразу, проверять заранее нечего.

    Там, где флага нет, остаётся проверка имени заранее. Она закрывает ту же
    модель угроз — симлинк, приехавший с чужим клоном, — но оставляет узкое
    окно между проверкой и открытием, которого на Unix не возникает.
    is_symlink() симлинк не разыменовывает, поэтому битый ловится тоже.

    O_TRUNC, а не O_EXCL: остаток .part от убитого по SIGKILL прогона должен
    переиспользоваться, а не блокировать скачивание навсегда.
    """
    if not _NOFOLLOW and partial.is_symlink():
        raise AttachmentError(f"на месте временного файла лежит симлинк: {partial}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW
    try:
        descriptor = os.open(partial, flags, 0o600)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK):
            raise AttachmentError(
                f"на месте временного файла лежит симлинк: {partial}"
            ) from None
        raise AttachmentError(
            f"не создать временный файл {partial}: {error.strerror}"
        ) from None
    return os.fdopen(descriptor, "wb")


class _StripAuthOnHostChange(urllib.request.HTTPRedirectHandler):
    """Снимает заголовок авторизации, если редирект уводит на другой адрес.

    urllib по умолчанию тащит заголовки за редиректом куда угодно, а редирект
    здесь штатный: Jira Cloud перебрасывает /secure/attachment/… на media-хост,
    Data Center за прокси — на файловое хранилище. Без этого PAT уезжает третьей
    стороне, и пострадает не наш пользователь, а тот, чей токен утёк. requests
    делает это сам (rebuild_auth), но requests спекой запрещён.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        # newurl здесь уже абсолютный: http_error_30x склеил его с исходным.
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in [n for n in new.headers if n.lower() in _SECRET_HEADERS]:
                del new.headers[name]
        return new


def parse_jira_attachments(payload: dict, base_url: str) -> list[Attachment]:
    raw = ((payload or {}).get("fields") or {}).get("attachment") or []
    result = []
    for item in raw:
        content = item.get("content") or ""
        result.append(
            Attachment(
                id=str(item.get("id", "")),
                filename=item.get("filename") or "attachment",
                size=_int(item.get("size") or 0),
                mime=item.get("mimeType") or "application/octet-stream",
                # Пустой адрес не склеиваем: _absolute(base, "") — это сам
                # базовый URL, то есть главная страница Jira, и она молча легла
                # бы на диск под именем вложения. См. download().
                url=_absolute(base_url, content) if content else "",
            )
        )
    return result


def _cloud_download_url(base_url: str, page_id: str, attachment_id: str, link: str) -> str:
    """Адрес v1 для скачивания вложения на Confluence Cloud.

    Cloud убрал легаси-эндпоинт /download/attachments/…, на который
    `_links.download` продолжает указывать: для API- и scoped-токенов он теперь
    отдаёт 401, хотя метаданные при этом приходят нормально. Обход тот же, что
    у mcp-atlassian (confluence/attachments.py:_resolve_attachment_download_url).
    Из query переносится только `version` — остальные легаси-параметры
    (api, cacheVersion, modificationDate) этому эндпоинту не нужны.
    """
    url = _absolute(
        base_url,
        f"/rest/api/content/{urllib.parse.quote(page_id, safe='')}"
        f"/child/attachment/{urllib.parse.quote(attachment_id, safe='')}/download",
    )
    version = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query).get("version", [""])[0]
    if version:
        url += "?" + urllib.parse.urlencode({"version": version})
    return url


def parse_confluence_attachments(
    payload: dict, base_url: str, page_id: str | None = None, *, cloud: bool = False
) -> list[Attachment]:
    raw = (payload or {}).get("results") or []
    result = []
    for item in raw:
        extensions = item.get("extensions") or {}
        download = ((item.get("_links") or {}).get("download")) or ""
        attachment_id = str(item.get("id", ""))
        if cloud and page_id and attachment_id:
            url = _cloud_download_url(base_url, page_id, attachment_id, download)
        else:
            url = _absolute(base_url, download) if download else ""
        result.append(
            Attachment(
                id=attachment_id,
                filename=item.get("title") or "attachment",
                size=_int(extensions.get("fileSize") or 0),
                mime=extensions.get("mediaType") or "application/octet-stream",
                url=url,
            )
        )
    return result


class Client:
    def __init__(self, credentials: Credentials, *, insecure: bool = False, timeout: int = 60):
        self._credentials = credentials
        self._timeout = timeout
        self._origin = _origin(credentials.url)
        self._cloud = is_cloud_url(credentials.url)
        handlers: list[urllib.request.BaseHandler] = [_StripAuthOnHostChange()]
        if insecure:
            handlers.append(
                urllib.request.HTTPSHandler(context=ssl._create_unverified_context())
            )
        # Свой opener, а не urlopen(): иначе редиректом занимается стандартный
        # обработчик, который заголовок авторизации не снимает.
        self._opener = urllib.request.build_opener(*handlers)

    # --- транспорт -------------------------------------------------------

    def _own_host(self, url: str) -> str:
        """Адрес из ответа сервера — на наш хост, с сохранением пути и query.

        content и _links.download у Jira Server абсолютны и собраны из настройки
        Base URL самого Jira, а она расходится с JIRA_URL пользователя сплошь и
        рядом: реверс-прокси, split-horizon DNS, http в конфиге против https в
        настройке. Отказ в этом месте клал скачивание целиком — ноль файлов на
        типовой инсталляции Server/DC, ради которой всё и писалось. Перевешиваем
        схему и хост на свои: на посторонний хост запрос по-прежнему не уходит и
        токен туда не отправляется, а типовое расхождение перестаёт быть
        фатальным. netloc берётся из базы целиком (вместе с userinfo, если она
        там есть) — ровно так же, как это делает _absolute.
        """
        try:
            if _origin(url) == self._origin:
                return url
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme in ("http", "https"):
                ours = urllib.parse.urlsplit(self._credentials.url)
                rehosted = urllib.parse.urlunsplit(
                    (ours.scheme, ours.netloc, parsed.path, parsed.query, "")
                )
                if _origin(rehosted) == self._origin:
                    return rehosted
        except ValueError:
            # urlsplit роняет ValueError на битом IPv6 («https://[::1/»), а адрес
            # пришёл от сервера. Падает одно вложение, а не весь прогон.
            pass
        # Сюда попадает то, что перевесить нельзя: чужая схема (file:, data:)
        # или базовый URL, который не разбирается сам.
        raise AttachmentError(
            f"ссылка ведёт на чужой хост: {self._safe(url)} — "
            f"наш адрес {self._safe(self._credentials.url)}. "
            "Если работать нужно с первым, задайте его через --url."
        )

    def _open(self, url: str, *, accept: str | None = None):
        url = self._own_host(url)

        request = urllib.request.Request(url)
        request.add_header("Authorization", self._credentials.auth)
        if accept:
            # Только на запросы за JSON: на скачивании бинарника этот заголовок
            # бессмыслен, а на WAF или прокси способен обернуться 406.
            request.add_header("Accept", accept)
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            # HTTPError сам по себе — открытый ответ с сокетом внутри. Не закрыть
            # его — оставить сокет висеть до сборки мусора, а 401/403/404 на пути
            # обхода вложений случаются пачками.
            error.close()
            if error.code in (401, 403, 404):
                raise NotFoundError(
                    f"HTTP {error.code} на {self._safe(url)} — нет доступа или объект не найден"
                ) from None
            raise NetworkError(f"HTTP {error.code} на {self._safe(url)}") from None
        except urllib.error.URLError as error:
            raise NetworkError(f"сеть недоступна: {error.reason}") from None
        except UnicodeEncodeError:
            # В запросную строку HTTP уходит только ASCII, а сервер имеет право
            # положить в content или _links.download неэкранированную кириллицу.
            # Без этой ветки одно такое вложение роняло трейсбеком весь прогон,
            # вместе с остальными файлами; теперь падает одно оно.
            raise NetworkError(
                f"адрес содержит неэкранированные символы: {self._safe(url)}"
            ) from None
        except http.client.HTTPException:
            # InvalidURL и родня не наследуют OSError, поэтому мимо веток выше и
            # ниже они уходили наружу сырыми. А в тексте InvalidURL стоит сырой
            # netloc — то есть пароль, если пользователь положил креды в URL.
            # Отсюда только вычищенный адрес и ни слова из str(error).
            raise NetworkError(f"некорректный адрес или ответ: {self._safe(url)}") from None
        except OSError as error:
            raise NetworkError(f"ошибка ввода-вывода: {error}") from None

    @staticmethod
    def _safe(url: str) -> str:
        """URL без секретов: ни query, ни userinfo.

        netloc целиком брать нельзя — это «user:password@host», а «https://
        svc:PAT@jira/…» в JIRA_URL встречается регулярно, и оттуда пароль уехал
        бы в каждое сообщение об ошибке. В query Atlassian кладёт os_username и
        следы сессии — им на stderr тоже нечего делать.
        """
        try:
            parsed = urllib.parse.urlsplit(url)
            host = parsed.hostname or ""
        except ValueError:
            # Битый IPv6 роняет сам разбор. Контракт _safe — вернуть печатаемое
            # при любом входе, в том числе когда его зовут из ветки обработки
            # этой самой ошибки.
            return "<адрес не разобран>"
        if not host:
            # Без «//» urlsplit читает «svc:PAT@jira/…» как схему «svc», а
            # «PAT@jira/…» оставляет в path — userinfo не срезается. Сегодня
            # такой адрес до сюда не доходит, его раньше перехватывает ветка
            # URLError, но контракт _safe — вернуть печатаемое при любом входе.
            return "<адрес не разобран>"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = f"{host}:{port}" if port else host
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    def _json(self, path: str) -> dict:
        url = _absolute(self._credentials.url, path)
        try:
            with self._open(url, accept="application/json") as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, http.client.HTTPException):
            # Свои ошибки _open уже превратил в NetworkError — это не OSError,
            # так что сюда падает только обрыв на чтении тела.
            raise NetworkError(f"обрыв при чтении ответа от {self._safe(url)}") from None
        except ValueError:
            # JSONDecodeError и UnicodeDecodeError — оба ValueError. За SSO
            # вместо JSON приходит HTML страницы логина, и это не экзотика, а
            # типичный первый запуск. Тело в текст не кладём: там может лежать
            # что угодно, включая куки в скрытом поле формы.
            raise NetworkError(f"неожиданный ответ от {self._safe(url)} — не JSON") from None

    # --- Jira ------------------------------------------------------------

    def jira_attachments(self, key: str) -> list[Attachment]:
        payload = self._json(f"/rest/api/2/issue/{urllib.parse.quote(key)}?fields=attachment")
        return parse_jira_attachments(payload, self._credentials.url)

    # --- Confluence ------------------------------------------------------

    def confluence_page(self, ref: PageRef) -> tuple[str, str]:
        if ref.page_id:
            payload = self._json(f"/rest/api/content/{urllib.parse.quote(ref.page_id)}")
            return ref.page_id, payload.get("title") or ref.page_id

        query = urllib.parse.urlencode({"spaceKey": ref.space, "title": ref.title, "limit": 1})
        payload = self._json(f"/rest/api/content?{query}")
        results = payload.get("results") or []
        if not results:
            raise NotFoundError(f"страница не найдена: {ref.space}:{ref.title}")
        return str(results[0]["id"]), results[0].get("title") or str(ref.title)

    def _next_path(self, link: str) -> str:
        """Ссылка пагинации — к пути относительно нашего REST-базиса.

        Confluence отдаёт _links.next относительно _links.base, а на Cloud эта
        база — адрес сайта вместе с /wiki. В ответах v1 префикса в ссылке нет, в
        ответах v2 он есть, и atlassian-python-api срезает его руками
        (atlassian/confluence/__init__.py:840). Живого Cloud-инстанса для
        проверки нет, поэтому обе формы приводятся к одной: срез идемпотентен, и
        на Server/DC, где база не кончается на /wiki, он не срабатывает вовсе.
        Ошибка здесь тихая — вторая страница выдачи ушла бы в 404, и половина
        вложений просто не скачалась бы.
        """
        if link.startswith("/wiki/") and self._credentials.url.endswith("/wiki"):
            return link[len("/wiki") :]
        return link

    def confluence_attachments(self, page_id: str) -> list[Attachment]:
        collected: list[Attachment] = []
        path = (
            f"/rest/api/content/{urllib.parse.quote(page_id)}"
            "/child/attachment?limit=200&expand=extensions"
        )
        # Единственный цикл в проекте, который может не кончиться: _links.next
        # приходит от сервера, и прокси, вернувший тот же путь, крутил бы его
        # вечно. Множество пройденного обрывает обход на первом повторе.
        seen = set()
        while path and path not in seen:
            seen.add(path)
            payload = self._json(path)
            collected.extend(
                parse_confluence_attachments(
                    payload, self._credentials.url, page_id, cloud=self._cloud
                )
            )
            path = self._next_path(((payload.get("_links") or {}).get("next")) or "")
        return collected

    # --- скачивание ------------------------------------------------------

    @staticmethod
    def _stream(response, sink, name: str) -> int:
        """Перекачивает тело в файл кусками, разводя обрыв связи и отказ диска.

        Читаем по частям, а не response.read(): вложение на 200 МБ иначе целиком
        окажется в памяти прежде, чем попадёт на диск. Ошибки чтения и записи
        разведены не для красоты — у них разные коды выхода: оборванная сеть это
        4, кончившееся место 1.
        """
        written = 0
        while True:
            try:
                chunk = response.read(_CHUNK)
            except (OSError, http.client.HTTPException):
                raise NetworkError(f"{name}: обрыв связи после {written} байт") from None
            if not chunk:
                return written
            try:
                sink.write(chunk)
            except OSError as error:
                raise AttachmentError(
                    f"{name}: не записать на диск — {error.strerror}"
                ) from None
            written += len(chunk)

    def download(
        self, attachment: Attachment, destination: pathlib.Path, *, force: bool = False
    ) -> str:
        destination = pathlib.Path(destination)
        if not attachment.url:
            # Вложение без адреса скачивания. Раньше пустой content схлопывался
            # в базовый URL, и вместо файла на диск ложилась главная страница —
            # при size == 0 сверка размера этого не ловила, и строка в таблице
            # честно говорила «скачано».
            raise AttachmentError(
                f"{attachment.filename}: сервер не дал адреса для скачивания"
            )
        # is_symlink() до exists(): у битого симлинка exists() ложно, и при
        # обратном порядке такой симлинк молча пролетел бы в скачивание.
        if destination.is_symlink():
            raise AttachmentError(f"на месте цели лежит симлинк: {destination}")
        # Иначе каталог доедет до replace() и вылетит голым IsADirectoryError,
        # мимо всей иерархии кодов выхода.
        if destination.is_dir():
            raise AttachmentError(f"на месте цели лежит каталог: {destination}")

        if not force and destination.exists():
            if attachment.size and destination.stat().st_size == attachment.size:
                return "skipped"

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise AttachmentError(
                f"не создать каталог {destination.parent}: {error.strerror}"
            ) from None

        partial = destination.with_name(partial_name(destination.name))
        state = "downloaded"
        try:
            # Ответ открывается первым: если .part открыть не удастся, ответ всё
            # равно будет закрыт — «with A() as a, B() as b» по семантике это
            # вложенные with, и a.__exit__ вызывается при падении B().
            with self._open(attachment.url) as response, _open_partial(partial) as sink:
                # Тело, собранное на лету: chunked и без Content-Length. Сверять
                # его с fileSize не с чем — метаданные описывают ХРАНИМЫЙ файл,
                # а сервер отдал другой. Confluence Server так отдаёт каждый
                # JPEG: пересжатым, и байт получается не столько, сколько в
                # fileSize, причём в обе стороны. Сверка удаляла здесь целый
                # валидный файл. Недокачку в этом режиме ловит само обрамление:
                # без завершающего нулевого куска http.client бросает
                # IncompleteRead, и _stream отдаёт её как NetworkError. Ответ
                # без длины и без chunked (HTTP/1.0, тело до закрытия связи)
                # таких гарантий не даёт, поэтому под послабление не подпадает.
                headers = response.headers
                rebuilt_on_the_fly = headers.get("Content-Length") is None and (
                    "chunked" in (headers.get("Transfer-Encoding") or "").lower()
                )
                written = self._stream(response, sink, attachment.filename)
            if attachment.size and written != attachment.size:
                if not rebuilt_on_the_fly:
                    raise AttachmentError(
                        f"{attachment.filename}: получено {written} байт вместо {attachment.size}"
                    )
                state = "rebuilt"
            try:
                partial.replace(destination)
            except OSError as error:
                raise AttachmentError(
                    f"{attachment.filename}: не уложить файл на место — {error.strerror}"
                ) from None
        finally:
            # unlink, а не «exists() и unlink»: exists() ложно для битого
            # симлинка, и такой .part пережил бы уборку, а следующий прогон
            # снова писал бы сквозь него. unlink симлинк не разыменовывает.
            # На успешном пути replace() уже унёс файл, и это no-op.
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
        return state
