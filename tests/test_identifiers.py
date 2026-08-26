import unittest

from atlassian_attachments.errors import EXIT_NOT_FOUND, AttachmentError
from atlassian_attachments.identifiers import PageRef, parse_confluence, parse_jira


class JiraTest(unittest.TestCase):
    def test_plain_key(self):
        self.assertEqual(parse_jira("PROJ-123"), "PROJ-123")

    def test_lowercase_key_is_upcased(self):
        self.assertEqual(parse_jira("proj-123"), "PROJ-123")

    def test_browse_url(self):
        self.assertEqual(
            parse_jira("https://jira.corp.example.com/browse/PROJ-123"), "PROJ-123"
        )

    def test_browse_url_with_query(self):
        self.assertEqual(
            parse_jira("https://jira.example/browse/PROJ-1?filter=42"), "PROJ-1"
        )

    def test_garbage_is_rejected(self):
        with self.assertRaises(AttachmentError) as caught:
            parse_jira("не ключ и не ссылка")
        # Код 2 («не найдено»), а не 1 («часть файлов скачана»): при
        # нераспознанном идентификаторе не скачано ничего.
        self.assertEqual(caught.exception.exit_code, EXIT_NOT_FOUND)


class ConfluenceTest(unittest.TestCase):
    def test_numeric_id(self):
        self.assertEqual(parse_confluence("123456789"), PageRef("123456789", None, None))

    def test_viewpage_url(self):
        self.assertEqual(
            parse_confluence(
                "https://wiki.example/pages/viewpage.action?pageId=123456789"
            ),
            PageRef("123456789", None, None),
        )

    def test_server_pages_url(self):
        self.assertEqual(
            parse_confluence("https://wiki.example/pages/123456/Some+Page"),
            PageRef("123456", None, None),
        )

    def test_cloud_spaces_url(self):
        self.assertEqual(
            parse_confluence(
                "https://acme.atlassian.net/wiki/spaces/DOCS/pages/123456789/Title"
            ),
            PageRef("123456789", None, None),
        )

    def test_display_url_gives_space_and_title(self):
        self.assertEqual(
            parse_confluence("https://wiki.example/display/DOCS/Some+Page+Title"),
            PageRef(None, "DOCS", "Some Page Title"),
        )

    def test_display_url_decodes_percent_escapes(self):
        self.assertEqual(
            parse_confluence("https://wiki.example/display/DOCS/%D0%9F%D0%B5%D1%87%D0%B0%D1%82%D1%8C"),
            PageRef(None, "DOCS", "Печать"),
        )

    def test_space_colon_title(self):
        self.assertEqual(
            parse_confluence("DOCS:120.4.7. Сводный отчёт"),
            PageRef(None, "DOCS", "120.4.7. Сводный отчёт"),
        )

    def test_garbage_is_rejected(self):
        garbage = [
            "",
            "просто текст",
            "https://wiki.example/",
            # Слева от двоеточия должен быть ключ пространства, а не что угодно.
            "не ключ: и не ссылка",
            # Опечатка в ссылке: один слеш вместо двух. Не пространство «https».
            "https:/wiki.example/display/DOCS/Page",
            # pageId из строки запроса — тоже число, а не любая строка.
            "https://wiki.example/pages/viewpage.action?pageId=abc",
            "https://wiki.example/pages/viewpage.action?pageId=../../x",
            # isdigit() истинно, но это не ASCII-цифры.
            "١٢٣",
        ]
        for value in garbage:
            with self.subTest(value=value):
                with self.assertRaises(AttachmentError) as caught:
                    parse_confluence(value)
                self.assertEqual(caught.exception.exit_code, EXIT_NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
