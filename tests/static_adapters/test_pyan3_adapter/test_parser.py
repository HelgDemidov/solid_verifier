# test_parser.py — юнит-тесты для второго прохода парсера: raw_output → nodes и edges.
#
# Стратегия изоляции: subprocess.run полностью заменяется через unittest.mock.patch;
# os.walk заменяется через tmp_py_project — реальный временный каталог с .py-файлами.
# Тесты проверяют только поведение парсера, confidence не тестируется здесь — это задача test_confidence.py.
import warnings
import pytest
from unittest.mock import patch, MagicMock

from tests.static_adapters.test_pyan3_adapter.helpers import make_raw_output, assert_edge


# Вспомогательная функция: запускает adapter.run() с подмененным subprocess.run
def _run_with_output(adapter, tmp_py_project, config, raw_output, returncode=0, stderr=""):
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = raw_output
    mock_result.stderr = stderr
    with patch("solid_dashboard.adapters.pyan3_adapter.subprocess.run", return_value=mock_result):
        return adapter.run(str(tmp_py_project), {}, config)


# ---------------------------------------------------------------------------
# Группа 1: Базовые случаи парсинга
# ---------------------------------------------------------------------------

class TestBasicParsing:

    def test_empty_raw_output_gives_no_nodes_no_edges(self, adapter, tmp_py_project, base_config):
        # пустой stdout — ни узлов, ни рёбер
        result = _run_with_output(adapter, tmp_py_project, base_config, "")
        assert result["is_success"] is True
        assert result["nodes"] == []
        assert result["edges"] == []
        assert result["node_count"] == 0
        assert result["edge_count"] == 0

    def test_single_edge_parsed_correctly(self, adapter, tmp_py_project, base_config):
        # простейший кейс: один блок, одно [U]-имя
        raw = make_raw_output([("A", "B")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["is_success"] is True
        assert set(result["nodes"]) == {"A", "B"}
        assert result["edge_count"] == 1
        assert result["edges"][0]["from"] == "A"
        assert result["edges"][0]["to"] == "B"

    def test_multiple_edges_from_one_block(self, adapter, tmp_py_project, base_config):
        # один блок A ссылается на три разных узла
        raw = make_raw_output([("A", "B"), ("A", "C"), ("A", "D")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["edge_count"] == 3
        assert set(result["nodes"]) == {"A", "B", "C", "D"}

    def test_nodes_include_both_source_and_target(self, adapter, tmp_py_project, base_config):
        # узел цели также добавляется в nodes, даже если блока для него нет
        raw = make_raw_output([("services.Auth", "repo.UserRepo")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert "services.Auth" in result["nodes"]
        assert "repo.UserRepo" in result["nodes"]

    def test_nodes_list_is_sorted(self, adapter, tmp_py_project, base_config):
        # финальный список узлов всегда отсортирован
        raw = make_raw_output([("Z", "A"), ("M", "B")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["nodes"] == sorted(result["nodes"])


# ---------------------------------------------------------------------------
# Группа 2: Фильтрация невалидных строк
# ---------------------------------------------------------------------------

class TestParserFiltering:

    def test_self_loop_edge_not_created(self, adapter, tmp_py_project, base_config):
        # self-loop отфильтровывается парсером; sanity check видит nodes>0 + edges=0
        # и выдаёт RuntimeWarning — это ожидаемый контракт данного сценария
        raw = "A\n  [U] A\n"
        with pytest.warns(RuntimeWarning, match="Sanity check"):
            result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["edge_count"] == 0
        assert "A" in result["dead_nodes"]

    def test_non_u_lines_in_block_ignored(self, adapter, tmp_py_project, base_config):
        # строки [D], [C] и другие внутри блока — не становятся рёбрами
        raw = "A\n  [D] B\n  [C] C\n  some_text\n  [U] D\n"
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["edge_count"] == 1
        assert result["edges"][0]["to"] == "D"

    def test_diagnostic_line_without_indent_not_a_block(self, adapter, tmp_py_project, base_config):
        # строка "WARNING: ..." без отступа не становится блоком—источником
        raw = "WARNING: pyan3 name resolution failed\nreal.Block\n  [U] target\n"
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        # единственный блок — real.Block, единственное ребро real.Block→target
        assert result["edge_count"] == 1
        assert "WARNING" not in result["nodes"]
        assert "real.Block" in result["nodes"]

    def test_invalid_block_name_digits_not_a_block(self, adapter, tmp_py_project, base_config):
        # блок с невалидным именем не становится блоком—источником, [U]-строки после него висячие
        raw = "123invalid\n  [U] B\nvalid.Block\n  [U] C\n"
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        # только ребро valid.Block→C должно пройти
        assert result["edge_count"] == 1
        assert result["edges"][0]["from"] == "valid.Block"
        assert "123invalid" not in result["nodes"]
        # B не создана как ребро (висячее ребро), но может попасть в dead_nodes как узел—цель
        assert "B" not in result["nodes"]

    def test_invalid_used_name_in_u_line_skipped(self, adapter, tmp_py_project, base_config):
        # [U]-цель с невалидным именем — ребро не создается
        raw = "A\n  [U] 123bad_name\n  [U] valid_target\n"
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["edge_count"] == 1
        assert result["edges"][0]["to"] == "valid_target"
        assert "123bad_name" not in result["nodes"]

    def test_dangling_u_line_without_source_block_skipped(self, adapter, tmp_py_project, base_config):
        # [U]-строка до появления любого блока — висячее ребро, пропускается
        raw = "  [U] orphan_target\nreal.Block\n  [U] real_target\n"
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert result["edge_count"] == 1
        assert "orphan_target" not in result["nodes"]


# ---------------------------------------------------------------------------
# Группа 3: Предупреждение sanity check
# ---------------------------------------------------------------------------

class TestSanityWarning:

    def test_sanity_warning_emitted_when_nodes_but_no_edges(self, adapter, tmp_py_project, base_config):
        # узлы есть, рёбер нет — признак несовместимости формата, должно выдать RuntimeWarning
        # Вызывается когда: есть блоки без отступа, но ни одной строки с [U]-отступом
        raw = "isolated.Node\n"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        runtime_warnings = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert len(runtime_warnings) == 1
        assert "Sanity check" in str(runtime_warnings[0].message)
        # результат все равно is_success=True — предупреждение не прерывает пайплайн
        assert result["is_success"] is True
