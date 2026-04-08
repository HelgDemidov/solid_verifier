# test_detect_suspicious_blocks.py — юнит-тесты для чистой функции _detect_suspicious_blocks().
#
# _detect_suspicious_blocks — фундамент confidence-системы всего адаптера:
# она определяет, какие блоки pyan3 содержат name collision.
# Все тесты — чистые юниты без какого-либо I/O: вход — строка, выход — множество строк.
import pytest
from tests.static_adapters.test_pyan3_adapter.helpers import _detect_suspicious_blocks


# ---------------------------------------------------------------------------
# Группа 1: Базовые случаи — пустой вход и один блок
# ---------------------------------------------------------------------------

class TestEmptyAndTrivialInput:

    def test_empty_output_returns_empty_set(self):
        # пустой raw_output — нет блоков, нет коллизий
        assert _detect_suspicious_blocks("") == set()

    def test_only_blank_lines_returns_empty_set(self):
        # вход состоит из одних пустых строк
        assert _detect_suspicious_blocks("\n\n\n") == set()

    def test_single_block_unique_used_names_not_suspicious(self):
        # блок foo.Bar с тремя уникальными [U]-именами
        raw = "foo.Bar\n  [U] alpha\n  [U] beta\n  [U] gamma\n"
        assert _detect_suspicious_blocks(raw) == set()

    def test_single_block_one_used_name_not_suspicious(self):
        # одно [U]-имя встречается ровно один раз — нет коллизии
        raw = "services.UserService\n  [U] repository.UserRepo\n"
        assert _detect_suspicious_blocks(raw) == set()


# ---------------------------------------------------------------------------
# Группа 2: Коллизия — кратное [U]-имя внутри одного блока
# ---------------------------------------------------------------------------

class TestCollisionDetection:

    def test_duplicate_used_name_marks_block_suspicious(self):
        # имя login встречается дважды — признак cross-attribution
        raw = "router.login\n  [U] login\n  [U] login\n"
        assert _detect_suspicious_blocks(raw) == {"router.login"}

    def test_triple_duplicate_also_marks_suspicious(self):
        # даже тройное вхождение одного имени — достаточно одного превышения
        raw = "A\n  [U] B\n  [U] B\n  [U] B\n"
        assert _detect_suspicious_blocks(raw) == {"A"}

    def test_self_loop_name_not_counted_as_collision(self):
        # [U] foo.Bar внутри блока foo.Bar — self-loop, не является коллизией
        raw = "foo.Bar\n  [U] foo.Bar\n  [U] foo.Bar\n"
        assert _detect_suspicious_blocks(raw) == set()

    def test_self_loop_plus_other_duplicate_still_suspicious(self):
        # self-loop не считается, но другое имя validate встречается дважды
        raw = "foo.Bar\n  [U] foo.Bar\n  [U] foo.Bar\n  [U] validate\n  [U] validate\n"
        assert _detect_suspicious_blocks(raw) == {"foo.Bar"}


# ---------------------------------------------------------------------------
# Группа 3: Несколько блоков
# ---------------------------------------------------------------------------

class TestMultipleBlocks:

    def test_one_of_two_blocks_suspicious(self):
        # первый блок чистый, второй содержит коллизию
        raw = (
            "clean.Block\n  [U] alpha\n  [U] beta\n"
            "dirty.Block\n  [U] login\n  [U] login\n"
        )
        result = _detect_suspicious_blocks(raw)
        assert result == {"dirty.Block"}

    def test_both_blocks_suspicious(self):
        # оба блока содержат коллизию
        raw = (
            "A\n  [U] x\n  [U] x\n"
            "B\n  [U] y\n  [U] y\n"
        )
        assert _detect_suspicious_blocks(raw) == {"A", "B"}

    def test_three_blocks_none_suspicious(self):
        # три блока, все чистые
        raw = (
            "A\n  [U] x\n"
            "B\n  [U] y\n"
            "C\n  [U] z\n"
        )
        assert _detect_suspicious_blocks(raw) == set()

    def test_last_block_in_file_is_processed(self):
        # последний блок не завершается новым блоком — должен обрабатываться через flush после EOF
        raw = "clean.Block\n  [U] alpha\nfinal.Block\n  [U] dup\n  [U] dup"
        result = _detect_suspicious_blocks(raw)
        assert result == {"final.Block"}


# ---------------------------------------------------------------------------
# Группа 4: Отсечение невалидных строк
# ---------------------------------------------------------------------------

class TestInvalidLinesFiltered:

    def test_diagnostic_prefix_WARNING_not_a_block(self):
        # строка без отступа, начинающаяся с "WARNING:" — не становится блоком
        raw = "WARNING: something went wrong\nreal.Block\n  [U] x\n"
        assert _detect_suspicious_blocks(raw) == set()

    def test_invalid_block_name_digits_not_a_block(self):
        # строка "123invalid" не соответствует _VALID_PY_NAME — не становится блоком
        raw = "123invalid\n  [U] dup\n  [U] dup\n"
        assert _detect_suspicious_blocks(raw) == set()

    def test_non_u_entries_ignored_in_counter(self):
        # строки [D], [C] и прочие — не [U] — не влияют на счетчик
        raw = "A\n  [D] login\n  [D] login\n  [C] login\n  [U] unique_name\n"
        assert _detect_suspicious_blocks(raw) == set()

    def test_empty_used_name_ignored(self):
        # "[U]" без имени после тега — пропускается, не падает в Counter
        raw = "A\n  [U]\n  [U]\n  [U] real_name\n"
        assert _detect_suspicious_blocks(raw) == set()

    def test_blank_lines_between_blocks_handled_correctly(self):
        # пустые строки между блоками не должны нарушать парсинг блоков
        raw = "A\n  [U] x\n\nB\n  [U] dup\n  [U] dup\n"
        assert _detect_suspicious_blocks(raw) == {"B"}
