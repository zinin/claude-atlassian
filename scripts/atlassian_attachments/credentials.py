"""Разрешение базового URL и токена: аргументы, окружение, конфиги MCP."""

from __future__ import annotations

import base64
import json
import pathlib
import re
import sys
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterator, Mapping

from .errors import CredentialsError

_PREFIX = {"jira": "JIRA", "confluence": "CONFLUENCE"}
_FLAG = {"jira": "jira", "confluence": "confluence"}
# Приватные диапазоны: инстанс на таком адресе — всегда Server/DC.
_PRIVATE_HOST = re.compile(r"^(127\.|192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)")
_CLOUD_SUFFIX = (
    ".atlassian.net",
    ".jira.com",
    ".jira-dev.com",
    ".atlassian.com",
    ".atlassian-us-gov-mod.net",
    ".atlassian-us-gov.net",
)


@dataclass(frozen=True)
class Credentials:
    url: str
    # repr=False: поле хранит токен, и печатать его по умолчанию не должно.
    # Иначе print(creds), logging.debug("%s", creds) или %r в тексте ошибки
    # унесут боевой Bearer в stdout, stderr или лог.
    auth: str = field(repr=False)
    source: str


def is_cloud_url(url: str) -> bool:
    """Cloud или Server/DC — по хосту, как это делает сам mcp-atlassian.

    Повторяет `utils/urls.py:is_atlassian_cloud_url` версии 0.23.1, включая
    порядок проверок: localhost и приватные диапазоны объявляются Server/DC до
    проверки суффиксов, иначе `10.0.0.1.atlassian.net` в /etc/hosts решал бы за
    нас. endswith, а не «in»: подстрока пустила бы сюда evil-atlassian.net.co.
    """
    if not url:
        return False
    try:
        hostname = (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        # Предикату положено отвечать, а не падать: битый адрес всё равно
        # умрёт ниже, в клиенте, с человеческим сообщением.
        return False
    if hostname == "localhost" or _PRIVATE_HOST.match(hostname):
        return False
    return hostname == "api.atlassian.com" or hostname.endswith(_CLOUD_SUFFIX)


def _rest_base(product: str, url: str) -> str:
    """Базовый URL для REST: на Confluence Cloud это адрес сайта плюс `/wiki`.

    CONFLUENCE_URL на Cloud сплошь и рядом задан голым адресом сайта, а REST
    живёт под `/wiki` — без префикса все запросы уходят в 404. mcp-atlassian
    правит это у себя тем же способом (`confluence/attachments.py:_rest_base_url`).
    Нормализация стоит здесь, а не в клиенте, чтобы Credentials.url был готовым
    REST-базисом и --explain-auth показывал ровно тот адрес, по которому пойдут
    запросы. endswith — чтобы не получить `/wiki/wiki` на уже готовом адресе.
    """
    url = (url or "").rstrip("/")
    if product == "confluence" and is_cloud_url(url) and not url.endswith("/wiki"):
        url += "/wiki"
    return url


def _scheme(auth: str) -> str:
    """Схема авторизации из готового заголовка — «Bearer» или «Basic».

    Только первое слово: за ним стоит сам токен, и в --explain-auth ему нечего
    делать ни целиком, ни куском.
    """
    return auth.split(" ", 1)[0] if auth else ""


def _basic(username: str, token: str) -> str:
    raw = f"{username}:{token}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _auth_header(pat: str | None, username: str | None, api_token: str | None) -> str | None:
    if pat:
        return f"Bearer {pat}"
    if username and api_token:
        return _basic(username, api_token)
    return None


def _from_environ(product: str, environ: Mapping[str, str]) -> tuple[str | None, str | None]:
    prefix = _PREFIX[product]
    url = environ.get(f"{prefix}_URL")
    auth = _auth_header(
        environ.get(f"{prefix}_PERSONAL_TOKEN"),
        environ.get(f"{prefix}_USERNAME"),
        environ.get(f"{prefix}_API_TOKEN"),
    )
    return url, auth


def _config_paths(start_dir: pathlib.Path, home: pathlib.Path) -> Iterator[pathlib.Path]:
    current = start_dir.resolve()
    for directory in [current, *current.parents]:
        yield directory / ".mcp.json"
    yield home / ".claude.json"
    yield home / ".mcp.json"
    yield current / ".claude" / "settings.local.json"
    yield current / ".claude" / "settings.json"


def _str_mapping(value: object) -> dict[str, str]:
    """Словарь только со строковыми ключами и значениями; всё прочее — пусто."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and isinstance(v, str)}


def _named_entries(servers: object, prefix: str = "") -> Iterator[tuple[str, str, dict]]:
    """Тройки «имя, путь до записи, запись»; всё неожиданной формы отбрасывается.

    Путь нужен --explain-auth: `_config_paths` идёт вверх до корня ФС, а
    `_server_entries` обходит все проекты в ~/.claude.json, так что победить
    может запись из чужого проекта — с другим хостом. Назвать один только файл
    значит ответить на вопрос «чей это конфиг» наполовину.
    """
    if not isinstance(servers, dict):
        return
    for name, entry in servers.items():
        if isinstance(name, str) and isinstance(entry, dict):
            yield name, f'{prefix}mcpServers["{name}"]', entry


def _server_entries(config: dict) -> Iterator[tuple[str, str, dict]]:
    yield from _named_entries(config.get("mcpServers"))
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return
    for key, project in projects.items():
        if isinstance(key, str) and isinstance(project, dict):
            yield from _named_entries(project.get("mcpServers"), f'projects["{key}"].')


def _args_as_mapping(args: object) -> dict[str, str]:
    """Собирает --flag=value, --flag value и docker -e KEY=VALUE в один словарь."""
    if not isinstance(args, list):
        return {}
    collected: dict[str, str] = {}
    index = 0
    while index < len(args):
        item = args[index]
        if not isinstance(item, str):
            index += 1
            continue
        if item == "-e" and index + 1 < len(args) and isinstance(args[index + 1], str):
            key, _, value = args[index + 1].partition("=")
            if value:
                collected[key] = value
            index += 2
            continue
        if item.startswith("--"):
            flag, separator, value = item.partition("=")
            if separator:
                collected[flag] = value
                index += 1
                continue
            if index + 1 < len(args) and isinstance(args[index + 1], str):
                following = args[index + 1]
                if not following.startswith("-"):
                    collected[flag] = following
                    index += 2
                    continue
        index += 1
    return collected


def _pick(
    args: Mapping[str, str], env: Mapping[str, str], keys: tuple[str, ...]
) -> tuple[str | None, str]:
    """Первое непустое значение по списку имён — и секция, где оно нашлось.

    Сначала весь список по args, потом весь по env: у записи сервера аргументы
    старше блока env, как и было до появления секции в выводе.
    """
    for source, mapping in (("args", args), ("env", env)):
        for key in keys:
            if mapping.get(key):
                return mapping[key], source
    return None, ""


def _from_entry(product: str, entry: dict) -> tuple[str | None, str | None, str]:
    """URL, готовый заголовок авторизации и секция записи, откуда взялся токен."""
    flag = _FLAG[product]
    prefix = _PREFIX[product]

    args = _args_as_mapping(entry.get("args"))
    env = _str_mapping(entry.get("env"))

    url, _ = _pick(args, env, (f"--{flag}-url", f"{prefix}_URL"))
    pat, pat_section = _pick(
        args, env, (f"--{flag}-personal-token", f"{prefix}_PERSONAL_TOKEN")
    )
    username, _ = _pick(args, env, (f"--{flag}-username", f"{prefix}_USERNAME"))
    api_token, api_section = _pick(
        args,
        env,
        (
            # --jira-token: настоящее имя флага у mcp-atlassian (0.23.1,
            # __init__.py:183), он же раскладывается в JIRA_API_TOKEN. Другого
            # имени у токена Cloud нет, и без этой строки пользователь Cloud,
            # настроивший сервер аргументами, получал «креды не найдены».
            f"--{flag}-token",
            # А такого флага не существует ни в одной версии сервера. Оставлен
            # запасным вариантом: безвреден, а чей-то форк может его завести.
            f"--{flag}-api-token",
            f"{prefix}_API_TOKEN",
        ),
    )
    auth = _auth_header(pat, username, api_token)
    if not auth:
        return url, None, ""
    # Секция считается по токену, а не по URL: строка --explain-auth отвечает
    # на вопрос «откуда взялась авторизация».
    return url, auth, pat_section if pat else api_section


def _looks_atlassian(entry: dict) -> bool:
    blob = json.dumps(entry, ensure_ascii=False).lower()
    return (
        "jira-url" in blob
        or "jira_url" in blob
        or "confluence-url" in blob
        or "confluence_url" in blob
    )


def _from_configs(
    product: str, start_dir: pathlib.Path, home: pathlib.Path
) -> tuple[str | None, str | None, str | None]:
    for path in _config_paths(start_dir, home):
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(config, dict):
            continue

        entries = list(_server_entries(config))
        preferred = [(w, e) for name, w, e in entries if name == "mcp-atlassian"]
        fallback = [(w, e) for _, w, e in entries if _looks_atlassian(e)]
        for where, entry in [*preferred, *fallback]:
            url, auth, section = _from_entry(product, entry)
            if url and auth:
                return url, auth, f"{path} → {where}.{section}"

        url, auth = _from_environ(product, _str_mapping(config.get("env")))
        if url and auth:
            return url, auth, f"{path} → env"
    return None, None, None


def resolve(
    product: str,
    *,
    url: str | None = None,
    token: str | None = None,
    environ: Mapping[str, str],
    start_dir: pathlib.Path,
    home: pathlib.Path | None = None,
) -> Credentials:
    if product not in _PREFIX:
        raise ValueError(f"неизвестный продукт: {product}")
    home = home or pathlib.Path.home()

    if url and token:
        auth = f"Bearer {token}"
        return Credentials(
            _rest_base(product, url), auth, f"аргументы --url и --token → {_scheme(auth)}"
        )

    env_url, env_auth = _from_environ(product, environ)
    if env_url and env_auth:
        return Credentials(
            _rest_base(product, env_url),
            env_auth,
            f"env {_PREFIX[product]}_* → {_scheme(env_auth)}",
        )

    prefix = _PREFIX[product]

    config_url, config_auth, source = _from_configs(product, start_dir, home)
    if config_url and config_auth:
        if env_url:
            # Хост из окружения задан, а токена к нему нет. Ошибкой это не делаем:
            # JIRA_URL часто выставлен глобально для других инструментов, а креды
            # лежат в конфиге. Но операция уйдёт не на тот хост, о котором просили,
            # поэтому пользователь должен об этом узнать.
            print(
                f"{prefix}_URL из окружения задан, но токена к нему нет"
                f" — беру креды из {source}",
                file=sys.stderr,
            )
        return Credentials(
            _rest_base(product, config_url),
            config_auth,
            f"конфиг MCP {source} → {_scheme(config_auth)}",
        )

    raise CredentialsError(
        f"не найдены креды для {product}.\n"
        f"Искал в таком порядке:\n"
        f"  1. аргументы --url и --token\n"
        f"  2. окружение: {prefix}_URL вместе с {prefix}_PERSONAL_TOKEN,\n"
        f"     либо {prefix}_USERNAME и {prefix}_API_TOKEN\n"
        f"  3. конфиги MCP: .mcp.json вверх от {start_dir}, затем\n"
        f"     {home}/.claude.json, {home}/.mcp.json, .claude/settings*.json\n"
        f"Задайте {prefix}_URL и {prefix}_PERSONAL_TOKEN либо передайте --url и --token."
    )
