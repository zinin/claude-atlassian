import base64
import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import unittest.mock

from atlassian_attachments.credentials import is_cloud_url, resolve
from atlassian_attachments.errors import CredentialsError


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        # Домашняя папка подменяется на временную. Без этого тесты читают
        # настоящий ~/.claude.json разработчика: там лежит рабочий
        # mcp-atlassian, и тест "кред нет" нашёл бы боевой токен и упал.
        self.home = self.root / "home"
        self.home.mkdir()
        patcher = unittest.mock.patch.object(
            pathlib.Path, "home", classmethod(lambda cls: self.home)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, payload):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class EnvironmentTest(TempTree):
    def test_personal_token_becomes_bearer(self):
        creds = resolve(
            "jira",
            environ={"JIRA_URL": "https://jira.example/", "JIRA_PERSONAL_TOKEN": "tok"},
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://jira.example")
        self.assertEqual(creds.auth, "Bearer tok")
        self.assertIn("env", creds.source)

    def test_username_and_api_token_become_basic(self):
        creds = resolve(
            "jira",
            environ={
                "JIRA_URL": "https://jira.example",
                "JIRA_USERNAME": "user@example.com",
                "JIRA_API_TOKEN": "secret",
            },
            start_dir=self.root,
        )
        expected = base64.b64encode(b"user@example.com:secret").decode("ascii")
        self.assertEqual(creds.auth, f"Basic {expected}")

    def test_confluence_reads_its_own_variables(self):
        creds = resolve(
            "confluence",
            environ={
                "JIRA_URL": "https://jira.example",
                "JIRA_PERSONAL_TOKEN": "jira-token",
                "CONFLUENCE_URL": "https://wiki.example",
                "CONFLUENCE_PERSONAL_TOKEN": "wiki-token",
            },
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://wiki.example")
        self.assertEqual(creds.auth, "Bearer wiki-token")

    def test_explicit_arguments_win_over_environment(self):
        creds = resolve(
            "jira",
            url="https://override.example",
            token="override-token",
            environ={"JIRA_URL": "https://jira.example", "JIRA_PERSONAL_TOKEN": "tok"},
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://override.example")
        self.assertEqual(creds.auth, "Bearer override-token")
        self.assertIn("--url", creds.source)


class McpConfigTest(TempTree):
    def test_args_with_equals_sign(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "command": "uvx",
                        "args": [
                            "mcp-atlassian",
                            "--jira-url=https://jira.example",
                            "--jira-personal-token=from-args",
                        ],
                    }
                }
            },
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        self.assertEqual(creds.url, "https://jira.example")
        self.assertEqual(creds.auth, "Bearer from-args")
        self.assertIn(".mcp.json", creds.source)

    def test_args_split_across_two_elements(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "args": [
                            "--jira-url",
                            "https://jira.example",
                            "--jira-personal-token",
                            "paired",
                        ]
                    }
                }
            },
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        self.assertEqual(creds.auth, "Bearer paired")

    def test_env_block_of_the_server_entry(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "command": "docker",
                        "env": {
                            "JIRA_URL": "https://jira.example",
                            "JIRA_PERSONAL_TOKEN": "from-env-block",
                        },
                    }
                }
            },
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        self.assertEqual(creds.auth, "Bearer from-env-block")

    def test_docker_dash_e_pairs(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "command": "docker",
                        "args": [
                            "run",
                            "-e",
                            "JIRA_URL=https://jira.example",
                            "-e",
                            "JIRA_PERSONAL_TOKEN=from-docker",
                            "ghcr.io/sooperset/mcp-atlassian",
                        ],
                    }
                }
            },
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        self.assertEqual(creds.auth, "Bearer from-docker")

    def test_config_found_by_walking_up_the_tree(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "args": [
                            "--jira-url=https://jira.example",
                            "--jira-personal-token=upstairs",
                        ]
                    }
                }
            },
        )
        deep = self.root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        creds = resolve("jira", environ={}, start_dir=deep)
        self.assertEqual(creds.auth, "Bearer upstairs")

    def test_server_recognised_by_arguments_when_name_differs(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "atlassian-of-my-own": {
                        "args": [
                            "--jira-url=https://jira.example",
                            "--jira-personal-token=renamed",
                        ]
                    }
                }
            },
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        self.assertEqual(creds.auth, "Bearer renamed")

    def test_projects_section_of_claude_json(self):
        payload = {
            "projects": {
                "/some/project": {
                    "mcpServers": {
                        "mcp-atlassian": {
                            "args": [
                                "--confluence-url=https://wiki.example",
                                "--confluence-personal-token=nested",
                            ]
                        }
                    }
                }
            }
        }
        (self.home / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
        creds = resolve("confluence", environ={}, start_dir=self.root)
        self.assertEqual(creds.url, "https://wiki.example")
        self.assertEqual(creds.auth, "Bearer nested")

    def test_environment_wins_over_config_file(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "args": [
                            "--jira-url=https://config.example",
                            "--jira-personal-token=from-config",
                        ]
                    }
                }
            },
        )
        creds = resolve(
            "jira",
            environ={"JIRA_URL": "https://env.example", "JIRA_PERSONAL_TOKEN": "from-env"},
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://env.example")

    def test_broken_json_is_skipped_not_fatal(self):
        (self.root / ".mcp.json").write_text("{ this is not json", encoding="utf-8")
        parent_config = self.root.parent / ".mcp.json"
        self.assertFalse(parent_config.exists(), "тест ожидает чистую родительскую папку")
        with self.assertRaises(CredentialsError):
            resolve("jira", environ={}, start_dir=self.root)


class CloudTest(TempTree):
    """Cloud: настоящее имя флага у токена и префикс /wiki у Confluence."""

    def cloud_entry(self, *args):
        self.write(".mcp.json", {"mcpServers": {"mcp-atlassian": {"args": list(args)}}})

    def test_args_form_of_a_cloud_token_is_recognised(self):
        # У mcp-atlassian флаг называется --jira-token (0.23.1, __init__.py:183);
        # --jira-api-token не существует. Через env та же связка работала, и
        # именно поэтому дыра не была видна: пользователь Cloud, настроивший
        # сервер аргументами, получал выход 3 и совет задать PERSONAL_TOKEN,
        # которого у Cloud не бывает.
        self.cloud_entry(
            "--jira-url=https://acme.atlassian.net",
            "--jira-username=me@acme.com",
            "--jira-token=CLOUD-API-TOKEN",
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        expected = base64.b64encode(b"me@acme.com:CLOUD-API-TOKEN").decode("ascii")
        self.assertEqual(creds.auth, f"Basic {expected}")
        self.assertEqual(creds.url, "https://acme.atlassian.net")

    def test_confluence_args_form_of_a_cloud_token_is_recognised(self):
        self.cloud_entry(
            "--confluence-url=https://acme.atlassian.net",
            "--confluence-username=me@acme.com",
            "--confluence-token=CLOUD-API-TOKEN",
        )
        creds = resolve("confluence", environ={}, start_dir=self.root)
        self.assertTrue(creds.auth.startswith("Basic "))

    def test_confluence_cloud_url_gets_the_wiki_prefix(self):
        # REST Confluence Cloud живёт под /wiki, а CONFLUENCE_URL сплошь и рядом
        # задан голым адресом сайта: без префикса каждый запрос уходит в 404.
        creds = resolve(
            "confluence",
            environ={
                "CONFLUENCE_URL": "https://acme.atlassian.net/",
                "CONFLUENCE_USERNAME": "me@acme.com",
                "CONFLUENCE_API_TOKEN": "tok",
            },
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://acme.atlassian.net/wiki")

    def test_the_wiki_prefix_is_never_doubled(self):
        creds = resolve(
            "confluence",
            environ={
                "CONFLUENCE_URL": "https://acme.atlassian.net/wiki/",
                "CONFLUENCE_PERSONAL_TOKEN": "tok",
            },
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://acme.atlassian.net/wiki")

    def test_server_confluence_url_is_left_alone(self):
        # Server/DC отдаёт REST прямо с корня: приписанный /wiki сломал бы всё.
        creds = resolve(
            "confluence",
            environ={
                "CONFLUENCE_URL": "https://wiki.example/confluence",
                "CONFLUENCE_PERSONAL_TOKEN": "tok",
            },
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://wiki.example/confluence")

    def test_jira_cloud_url_stays_bare(self):
        creds = resolve(
            "jira",
            environ={
                "JIRA_URL": "https://acme.atlassian.net",
                "JIRA_PERSONAL_TOKEN": "tok",
            },
            start_dir=self.root,
        )
        self.assertEqual(creds.url, "https://acme.atlassian.net")

    def test_cloud_hosts_are_told_from_server_ones(self):
        # Порядок проверок как у mcp-atlassian (utils/urls.py): приватные
        # адреса — всегда Server/DC, и суффикс проверяется через endswith,
        # иначе «evil-atlassian.net.example.com» сошёл бы за Cloud.
        for url in (
            "https://acme.atlassian.net",
            "https://acme.atlassian.net/wiki",
            "https://acme.jira.com",
            "https://api.atlassian.com/ex/confluence/x",
        ):
            self.assertTrue(is_cloud_url(url), url)
        for url in (
            "https://wiki.example",
            "https://jira.corp.example.com",
            "http://localhost:8090",
            "http://127.0.0.1:8090",
            "http://10.1.2.3/wiki",
            "http://192.168.0.5/wiki",
            "http://172.16.0.5/wiki",
            "https://evil-atlassian.net.example.com",
            "https://[::1/",
            "",
        ):
            self.assertFalse(is_cloud_url(url), url)


class MalformedConfigTest(TempTree):
    """Валидный JSON неожиданной структуры пропускается, а не роняет процесс."""

    def assert_skipped_and_cascade_continues(self, broken_config):
        """Кладёт кривой конфиг ближе, годный — дальше, и ждёт, что победит годный."""
        self.write(".mcp.json", broken_config)
        fallback = {
            "mcpServers": {
                "mcp-atlassian": {
                    "args": [
                        "--jira-url=https://fallback.example",
                        "--jira-personal-token=fallback-token",
                    ]
                }
            }
        }
        (self.home / ".claude.json").write_text(json.dumps(fallback), encoding="utf-8")

        creds = resolve("jira", environ={}, start_dir=self.root)

        self.assertEqual(creds.url, "https://fallback.example")
        self.assertEqual(creds.auth, "Bearer fallback-token")

    def test_mcp_servers_is_a_list(self):
        self.assert_skipped_and_cascade_continues({"mcpServers": ["mcp-atlassian"]})

    def test_projects_is_a_list(self):
        self.assert_skipped_and_cascade_continues({"projects": ["/some/project"]})

    def test_env_block_is_a_list(self):
        self.assert_skipped_and_cascade_continues(
            {"mcpServers": {"mcp-atlassian": {"env": ["JIRA_URL=https://jira.example"]}}}
        )

    def test_args_is_an_object(self):
        self.assert_skipped_and_cascade_continues(
            {"mcpServers": {"mcp-atlassian": {"args": {"--jira-url": "https://jira.example"}}}}
        )

    def test_unexpected_structure_alone_ends_in_credentials_error(self):
        # Контейнер непустой намеренно: пустой список отсеивается проверкой на
        # истинность и не доходит до .items(), поэтому "mcpServers": [] прошёл бы
        # и без исправления.
        self.write(".mcp.json", {"mcpServers": ["mcp-atlassian"], "projects": ["/p"]})
        with self.assertRaises(CredentialsError):
            resolve("jira", environ={}, start_dir=self.root)


class PartialEnvironmentTest(TempTree):
    """URL в окружении без токена: конфиг побеждает, но пользователя предупреждают."""

    def write_config(self):
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "args": [
                            "--jira-url=https://config.example",
                            "--jira-personal-token=from-config",
                            "--confluence-url=https://wiki-config.example",
                            "--confluence-personal-token=wiki-from-config",
                        ]
                    }
                }
            },
        )

    def test_url_without_token_warns_on_stderr_and_falls_through(self):
        self.write_config()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            creds = resolve(
                "jira", environ={"JIRA_URL": "https://env.example"}, start_dir=self.root
            )

        self.assertEqual(creds.url, "https://config.example")
        self.assertEqual(creds.auth, "Bearer from-config")

        warning = stderr.getvalue()
        self.assertIn("JIRA_URL", warning)
        self.assertIn(".mcp.json", warning)
        self.assertEqual(len(warning.strip().splitlines()), 1, "предупреждение — одна строка")

    def test_confluence_warns_the_same_way(self):
        self.write_config()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            creds = resolve(
                "confluence",
                environ={"CONFLUENCE_URL": "https://wiki-env.example"},
                start_dir=self.root,
            )

        self.assertEqual(creds.url, "https://wiki-config.example")
        self.assertIn("CONFLUENCE_URL", stderr.getvalue())

    def test_complete_environment_says_nothing(self):
        self.write_config()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            creds = resolve(
                "jira",
                environ={"JIRA_URL": "https://env.example", "JIRA_PERSONAL_TOKEN": "from-env"},
                start_dir=self.root,
            )

        self.assertEqual(creds.url, "https://env.example")
        self.assertEqual(stderr.getvalue(), "")

    def test_warning_never_carries_the_token_from_the_config(self):
        self.write_config()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            resolve("jira", environ={"JIRA_URL": "https://env.example"}, start_dir=self.root)
        self.assertNotIn("from-config", stderr.getvalue())


class FailureTest(TempTree):
    def test_message_lists_what_was_searched_and_never_leaks_a_token(self):
        with self.assertRaises(CredentialsError) as caught:
            resolve("jira", environ={"JIRA_PERSONAL_TOKEN": "orphan-token"}, start_dir=self.root)
        message = str(caught.exception)
        self.assertIn("JIRA_URL", message)
        self.assertIn(".mcp.json", message)
        self.assertNotIn("orphan-token", message)


class SourceDescriptionTest(TempTree):
    """`source` — это весь ответ --explain-auth: файл, запись и схема, без значений."""

    def test_source_names_the_entry_and_the_scheme(self):
        # Файла мало: _config_paths идёт вверх до корня ФС, а _server_entries
        # обходит все проекты в ~/.claude.json, так что победить может запись
        # из чужого проекта — с другим хостом. Имя записи и схема отвечают на
        # вопрос «чей это конфиг» целиком.
        self.write(
            ".mcp.json",
            {
                "mcpServers": {
                    "mcp-atlassian": {
                        "args": [
                            "--jira-url=https://jira.example",
                            "--jira-personal-token=s3cr3t",
                        ]
                    }
                }
            },
        )
        creds = resolve("jira", environ={}, start_dir=self.root)
        self.assertIn(".mcp.json", creds.source)
        self.assertIn('mcpServers["mcp-atlassian"].args', creds.source)
        self.assertIn("Bearer", creds.source)
        self.assertNotIn("s3cr3t", creds.source)

    def test_source_names_the_project_key_inside_claude_json(self):
        payload = {
            "projects": {
                "/some/other/project": {
                    "mcpServers": {
                        "renamed-atlassian": {
                            "env": {
                                "CONFLUENCE_URL": "https://wiki.example",
                                "CONFLUENCE_USERNAME": "me@example.com",
                                "CONFLUENCE_API_TOKEN": "s3cr3t",
                            }
                        }
                    }
                }
            }
        }
        (self.home / ".claude.json").write_text(json.dumps(payload), encoding="utf-8")
        creds = resolve("confluence", environ={}, start_dir=self.root)
        self.assertIn('projects["/some/other/project"]', creds.source)
        self.assertIn('mcpServers["renamed-atlassian"].env', creds.source)
        self.assertIn("Basic", creds.source)
        self.assertNotIn("s3cr3t", creds.source)

    def test_source_of_the_environment_names_the_scheme(self):
        creds = resolve(
            "jira",
            environ={
                "JIRA_URL": "https://jira.example",
                "JIRA_USERNAME": "me@example.com",
                "JIRA_API_TOKEN": "s3cr3t",
            },
            start_dir=self.root,
        )
        self.assertIn("env JIRA_*", creds.source)
        self.assertIn("Basic", creds.source)
        self.assertNotIn("s3cr3t", creds.source)

    def test_source_never_contains_the_token(self):
        creds = resolve(
            "jira",
            environ={"JIRA_URL": "https://jira.example", "JIRA_PERSONAL_TOKEN": "s3cr3t"},
            start_dir=self.root,
        )
        self.assertNotIn("s3cr3t", creds.source)

    def test_printing_the_object_never_leaks_the_token(self):
        # repr — это отладочный путь: print(creds), logging.debug("%s", creds),
        # f"{creds}" в тексте ошибки. Токен не должен всплыть ни в одном из них.
        creds = resolve(
            "jira",
            environ={"JIRA_URL": "https://jira.example", "JIRA_PERSONAL_TOKEN": "s3cr3t"},
            start_dir=self.root,
        )
        self.assertNotIn("s3cr3t", repr(creds))
        self.assertNotIn("s3cr3t", str(creds))
        self.assertNotIn("s3cr3t", f"{creds}")
        self.assertIn("https://jira.example", repr(creds))


if __name__ == "__main__":
    unittest.main()
