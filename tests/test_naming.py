import pathlib
import subprocess
import tempfile
import unicodedata
import unittest
import unittest.mock

from atlassian_attachments import naming
from atlassian_attachments.naming import (
    base_dir,
    jira_dir,
    partial_name,
    sanitize_filename,
    slugify_title,
    unique_path,
    wiki_dir,
)


class SanitizeTest(unittest.TestCase):
    def test_keeps_cyrillic_spaces_and_brackets(self):
        name = "Сводный отчёт за квартал - 3 версия (с правками) (2).docx"
        self.assertEqual(sanitize_filename(name), name)

    def test_strips_directory_traversal(self):
        self.assertEqual(
            sanitize_filename("../../.ssh/authorized_keys"), "authorized_keys"
        )

    def test_strips_windows_separators(self):
        self.assertEqual(sanitize_filename(r"..\..\windows\system32\evil.dll"), "evil.dll")

    def test_strips_control_characters(self):
        self.assertEqual(sanitize_filename("re\x00port\nfinal.log"), "reportfinal.log")

    def test_dot_names_are_replaced(self):
        self.assertEqual(sanitize_filename(".."), "attachment")
        self.assertEqual(sanitize_filename("."), "attachment")
        self.assertEqual(sanitize_filename("   "), "attachment")

    def test_long_name_is_truncated_but_keeps_extension(self):
        name = "п" * 400 + ".log"
        result = sanitize_filename(name)
        self.assertLessEqual(len(result.encode("utf-8")), 255)
        self.assertTrue(result.endswith(".log"))


class PartialNameTest(unittest.TestCase):
    def test_a_short_name_just_gets_the_suffix(self):
        self.assertEqual(partial_name("trace.log"), "trace.log.part")

    def test_a_name_at_the_limit_leaves_room_for_the_suffix(self):
        # sanitize_filename отдаёт до 255 байт; «.part» поверх такого имени
        # переступал предел файловой системы, и os.open отвечал ENAMETOOLONG.
        name = "и" * 127 + "x"
        self.assertEqual(len(name.encode("utf-8")), 255)
        result = partial_name(name)
        self.assertLessEqual(len(result.encode("utf-8")), 255)
        self.assertTrue(result.endswith(".part"))

    def test_the_cut_never_splits_a_character(self):
        result = partial_name("и" * 200)
        self.assertLessEqual(len(result.encode("utf-8")), 255)
        self.assertTrue(result.replace(".part", "").endswith("и"))


class SlugTest(unittest.TestCase):
    def test_spaces_become_dashes(self):
        self.assertEqual(
            slugify_title("120.4.7. Сводный отчёт"), "120.4.7.-Сводный-отчёт"
        )

    def test_separators_are_removed(self):
        self.assertEqual(slugify_title("a/b\\c"), "abc")

    def test_repeated_dashes_collapse(self):
        self.assertEqual(slugify_title("a   -   b"), "a-b")

    def test_truncated_to_limit_without_trailing_dash(self):
        result = slugify_title("слово " * 40, limit=20)
        self.assertLessEqual(len(result), 20)
        self.assertFalse(result.endswith("-"))


class TempTree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name).resolve()
        self.addCleanup(self._tmp.cleanup)


class WindowsNamesTest(TempTree):
    """Имена, недопустимые на Windows.

    Правила включаются только там: на Unix двоеточие, вопрос и хвостовая точка —
    обычные символы имени, и вырезать их значило бы менять имена, которые
    сегодня сохраняются ровно как в источнике.
    """

    def on_windows(self, name):
        with unittest.mock.patch.object(naming, "_WINDOWS", True):
            return sanitize_filename(name)

    def test_reserved_device_names_get_out_of_the_way(self):
        self.assertEqual(self.on_windows("CON.log"), "_CON.log")
        self.assertEqual(self.on_windows("nul.txt"), "_nul.txt")
        self.assertEqual(self.on_windows("COM1"), "_COM1")

    def test_a_name_that_merely_starts_like_one_is_left_alone(self):
        self.assertEqual(self.on_windows("console.log"), "console.log")
        self.assertEqual(self.on_windows("auxiliary.txt"), "auxiliary.txt")

    def test_a_trailing_dot_is_dropped_before_windows_drops_it(self):
        # Windows срезает её сам, и «report.» открывается как «report»: два
        # разных вложения молча ложатся в один файл.
        self.assertEqual(self.on_windows("report."), "report")

    def test_a_colon_never_becomes_an_alternate_data_stream(self):
        # «a:b.png» на NTFS — поток файла «a»: содержимое уходит туда, где
        # пользователь его не увидит.
        self.assertNotIn(":", self.on_windows("a:b.png"))

    def test_characters_windows_refuses_do_not_fail_the_attachment(self):
        for name in ('q?.log', 'x"y.txt', "trace|1.log", "a*b.png", "a<b>c.png"):
            with self.subTest(name=name):
                result = self.on_windows(name)
                self.assertFalse(set(result) & set('<>:"|?*'))

    def test_a_trailing_dot_no_longer_hides_a_collision(self):
        with unittest.mock.patch.object(naming, "_WINDOWS", True):
            taken = set()
            first = unique_path(self.root, "report.log", "1", taken)
            second = unique_path(self.root, "report.log.", "2", taken)
        self.assertNotEqual(first.name, second.name)

    def test_a_page_title_with_a_colon_still_makes_a_folder(self):
        # Заголовок с двоеточием в Confluence обычнее, чем вложение с именем
        # устройства, а цена выше: mkdir отказывает — теряется вся страница.
        with unittest.mock.patch.object(naming, "_WINDOWS", True):
            slug = slugify_title("Раздел: Сводный отчёт")
        self.assertEqual(slug, "Раздел-Сводный-отчёт")

    def test_the_rules_are_off_on_every_other_platform(self):
        with unittest.mock.patch.object(naming, "_WINDOWS", False):
            self.assertEqual(sanitize_filename("CON.log"), "CON.log")
            self.assertEqual(sanitize_filename("a:b.png"), "a:b.png")
            self.assertEqual(sanitize_filename("report."), "report.")
            self.assertEqual(
                slugify_title("Раздел: Сводный отчёт"), "Раздел:-Сводный-отчёт"
            )


class FoldedNamesTest(TempTree):
    """Файловые системы, где два написания — одна запись каталога.

    NTFS и APFS по умолчанию не различают регистр, а macOS вдобавок хранит
    имена нормализованными. На Linux это разные файлы, и сводить их значило бы
    без нужды дописывать id к имени.
    """

    def test_a_case_variant_does_not_take_an_earlier_name(self):
        with unittest.mock.patch.object(naming, "_FOLDED_NAMES", True):
            taken = set()
            first = unique_path(self.root, "Report.log", "1", taken)
            second = unique_path(self.root, "report.log", "2", taken)
        self.assertEqual(first.name, "Report.log")
        self.assertNotEqual(second.name.casefold(), "report.log")

    def test_the_part_neighbour_is_matched_the_same_way(self):
        with unittest.mock.patch.object(naming, "_FOLDED_NAMES", True):
            taken = set()
            unique_path(self.root, "report.log", "1", taken)
            second = unique_path(self.root, "REPORT.LOG.part", "2", taken)
        self.assertNotEqual(second.name.casefold(), "report.log.part")

    def test_a_normalisation_variant_is_the_same_entry_too(self):
        # macOS хранит имена в NFD: «ё» составное и разложенное — один файл.
        composed = "отчёт.log"
        decomposed = unicodedata.normalize("NFD", composed)
        self.assertNotEqual(composed, decomposed)
        with unittest.mock.patch.object(naming, "_FOLDED_NAMES", True):
            taken = set()
            unique_path(self.root, composed, "1", taken)
            second = unique_path(self.root, decomposed, "2", taken)
        self.assertNotEqual(unicodedata.normalize("NFC", second.name), composed)

    def test_case_still_tells_names_apart_where_the_system_does(self):
        with unittest.mock.patch.object(naming, "_FOLDED_NAMES", False):
            taken = set()
            first = unique_path(self.root, "Report.log", "1", taken)
            second = unique_path(self.root, "report.log", "2", taken)
        self.assertEqual(first.name, "Report.log")
        self.assertEqual(second.name, "report.log")


class BaseDirTest(TempTree):
    def test_git_root_wins(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        deep = self.root / "a" / "b"
        deep.mkdir(parents=True)
        self.assertEqual(base_dir(deep), self.root)

    def test_falls_back_to_the_directory_itself(self):
        outside = self.root / "plain"
        outside.mkdir()
        self.assertEqual(base_dir(outside), outside)


class DirectoryTest(TempTree):
    def test_jira_directory(self):
        self.assertEqual(
            jira_dir(self.root, "PROJ-123"),
            self.root / "docs" / "jira-attachments" / "PROJ-123",
        )

    def test_wiki_directory_combines_id_and_title(self):
        self.assertEqual(
            wiki_dir(self.root, "123456789", "120.4.7. Сводный отчёт"),
            self.root / "docs" / "wiki-attachments" / "123456789-120.4.7.-Сводный-отчёт",
        )

    def test_wiki_directory_reuses_existing_folder_after_rename(self):
        existing = self.root / "docs" / "wiki-attachments" / "123456789-старое-имя"
        existing.mkdir(parents=True)
        self.assertEqual(
            wiki_dir(self.root, "123456789", "Новое имя"), existing
        )

    def test_a_page_folder_never_exceeds_the_byte_limit(self):
        # Слуг ограничен символами, а предел файловой системы байтовый:
        # заголовок из четырёхбайтовых символов при длинном id переступал 255
        # байт, и mkdir отвечал ENAMETOOLONG — тогда не скачивалось ни одно
        # вложение страницы.
        folder = wiki_dir(self.root, "1" * 15, "\U0001f642" * 80)
        self.assertLessEqual(len(folder.name.encode("utf-8")), 255)
        self.assertTrue(folder.name.startswith("1" * 15 + "-"))

    def test_a_page_folder_of_ordinary_length_is_untouched(self):
        # Обычные заголовки правило про байты задевать не должно.
        self.assertEqual(
            wiki_dir(self.root, "123456789", "120.4.7. Сводный отчёт").name,
            "123456789-120.4.7.-Сводный-отчёт",
        )

    def test_wiki_directory_ignores_a_symlinked_folder(self):
        # is_dir() разыменовывает симлинк, поэтому подделка выглядела бы
        # обычным каталогом и увела бы запись за пределы проекта.
        outside = self.root / "outside"
        outside.mkdir()
        parent = self.root / "docs" / "wiki-attachments"
        parent.mkdir(parents=True)
        (parent / "123456789-подделка").symlink_to(outside, target_is_directory=True)
        result = wiki_dir(self.root, "123456789", "Настоящая страница")
        self.assertFalse(result.is_symlink())
        self.assertEqual(result.name, "123456789-Настоящая-страница")


class UniquePathTest(TempTree):
    def test_plain_name_when_free(self):
        taken = set()
        result = unique_path(self.root, "report.log", "100042", taken)
        self.assertEqual(result, self.root / "report.log")
        self.assertIn("report.log", taken)

    def test_collision_gets_the_attachment_id(self):
        taken = {"report.log"}
        result = unique_path(self.root, "report.log", "100042", taken)
        self.assertEqual(result, self.root / "report-100042.log")

    def test_collision_without_extension(self):
        taken = {"README"}
        result = unique_path(self.root, "README", "77", taken)
        self.assertEqual(result, self.root / "README-77")

    def test_the_reserved_part_name_is_the_one_that_will_be_opened(self):
        """Резервировать надо то имя .part, которое реально откроется.

        У длинного имени временное усечено, а бронировалось «<имя>.part» —
        строка, которой не может существовать. Вложение, названное этим
        усечённым именем, проходило как свободное, скачивалось, а потом
        соседнее длинное открывало его как свой .part с O_TRUNC и уносило
        replace()-ом. Оба при этом отмечались скачанными.
        """
        long_name = "и" * 127 + "x"
        temporary = partial_name(long_name)
        self.assertNotEqual(temporary, long_name + ".part")

        taken = set()
        unique_path(self.root, long_name, "1", taken)
        self.assertIn(temporary, taken)

        second = unique_path(self.root, temporary, "2", taken)
        self.assertNotEqual(second.name, temporary)

    def test_taken_id_suffix_does_not_overwrite_the_earlier_file(self):
        # Вложение может само называться «report-100042.log»: тогда имя с
        # подставленным id занято, и остановиться на нём — молча затереть
        # уже скачанный файл.
        taken = {"report.log", "report-100042.log"}
        result = unique_path(self.root, "report.log", "100042", taken)
        self.assertNotIn(result.name, ("report.log", "report-100042.log"))
        self.assertEqual(result.parent, self.root)
        self.assertTrue(result.name.endswith(".log"))
        self.assertIn(result.name, taken)

    def test_a_part_neighbour_never_collides_with_a_real_attachment(self):
        # Байты вложения принимает сосед «<имя>.part» в том же каталоге, так что
        # тикет с парой report.log и report.log.part затирал один файл другим —
        # в обоих порядках, и оба помечались как скачанные.
        taken = set()
        first = unique_path(self.root, "report.log", "1", taken)
        second = unique_path(self.root, "report.log.part", "2", taken)
        self.assertNotEqual(second.name, f"{first.name}.part")
        self.assertNotEqual(first, second)

        taken = set()
        first = unique_path(self.root, "report.log.part", "1", taken)
        second = unique_path(self.root, "report.log", "2", taken)
        self.assertNotEqual(first.name, f"{second.name}.part")
        self.assertNotEqual(first, second)

    def test_suffix_keeps_the_name_within_the_byte_limit(self):
        # 125 кириллических символов — 250 байт, вплотную к границе; суффикс
        # не имеет права выбить имя за 255 байт, иначе запись падает с
        # OSError: File name too long.
        name = "п" * 125 + ".log"
        first = sanitize_filename(name)
        taken = {first}
        result = unique_path(self.root, name, "100042", taken)
        self.assertLessEqual(len(result.name.encode("utf-8")), 255)
        self.assertNotEqual(result.name, first)
        self.assertTrue(result.name.endswith(".log"))


if __name__ == "__main__":
    unittest.main()
