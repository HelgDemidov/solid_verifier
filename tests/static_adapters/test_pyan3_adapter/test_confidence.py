# test_confidence.py — юнит-тесты маркировки confidence у рёбер call-graph.
#
# Логика confidence определяется двумя факторами:
#   1. Подозрительность блока-источника (_detect_suspicious_blocks) — если блок
#      помечен как suspicious, все его исходящие рёбра получают confidence="low".
#   2. По умолчанию (источник не подозрительный) — confidence="high".
# Каскадное распространение: suspicious-блок может стать целью другого ребра,
# что НЕ влияет на confidence этого входящего ребра — только источник важен.
#
# Примечание о pytest.warns:
# Тесты, намеренно создающие suspicious-блоки, вызывают adapter.run() целиком,
# включая проверку collision_rate. В мини-фикстурах (2–4 узла) даже 1 suspicious
# блок даёт 50% rate > порога 35%, что корректно эмитирует RuntimeWarning.
# pytest.warns явно фиксирует это как ожидаемый контракт: если адаптер
# перестанет эмитировать предупреждение — тест упадёт и регрессия поймается.
import pytest
from unittest.mock import patch, MagicMock

from tests.static_adapters.test_pyan3_adapter.helpers import (
    make_raw_output,
    assert_edge,
    assert_success_schema,
)


# Вспомогательная функция: запускает adapter.run() с подменённым subprocess.run
def _run_with_output(adapter, tmp_py_project, config, raw_output):
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = raw_output
    mock_result.stderr = ""
    with patch("solid_dashboard.adapters.pyan3_adapter.subprocess.run", return_value=mock_result):
        return adapter.run(str(tmp_py_project), {}, config)


# ---------------------------------------------------------------------------
# Группа 1: Базовое присвоение confidence
# ---------------------------------------------------------------------------

class TestConfidenceDefault:

    def test_clean_block_gets_high_confidence(self, adapter, tmp_py_project, base_config):
        # блок без дублей в [U]-именах → все его рёбра confidence="high"
        raw = make_raw_output([("A", "B"), ("A", "C")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert_edge(result["edges"], "A", "B", "high")
        assert_edge(result["edges"], "A", "C", "high")
        assert result["edge_count_high"] == 2
        assert result["edge_count_low"] == 0

    def test_suspicious_block_gets_low_confidence(self, adapter, tmp_py_project, base_config):
        # блок с дублем в [U]-именах → suspicious → рёбра confidence="low"
        # extra_used добавляет второй [U] B внутри блока A → коллизия
        #
        # Ожидаемый side-effect: 1 suspicious из 2 узлов = 50% > порога 35%
        # → адаптер эмитирует RuntimeWarning о высоком collision rate.
        # Фиксируем это как явный контракт через pytest.warns.
        raw = make_raw_output([("A", "B")], extra_used={"A": ["B"]})
        with pytest.warns(RuntimeWarning, match="high collision rate"):
            result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert_edge(result["edges"], "A", "B", "low")
        assert result["edge_count_low"] == 1
        assert result["edge_count_high"] == 0

    def test_mixed_blocks_correct_confidence_per_source(self, adapter, tmp_py_project, base_config):
        # два блока: один чистый, один suspicious — каждый маркируется независимо
        raw = (
            make_raw_output([("Clean", "X")]) +          # чистый блок
            make_raw_output([("Dirty", "Y")], extra_used={"Dirty": ["Y"]})  # suspicious
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert_edge(result["edges"], "Clean", "X", "high")
        assert_edge(result["edges"], "Dirty", "Y", "low")
        assert result["edge_count_high"] == 1
        assert result["edge_count_low"] == 1


# ---------------------------------------------------------------------------
# Группа 2: Каскадное распространение и граничные случаи
# ---------------------------------------------------------------------------

class TestConfidenceCascading:

    def test_target_of_suspicious_is_not_contaminated(self, adapter, tmp_py_project, base_config):
        # Y — цель suspicious-блока, но если Y сам чистый источник, его рёбра high
        # Dirty → Y (low),  Y → Z (high)
        raw = (
            make_raw_output([("Dirty", "Y")], extra_used={"Dirty": ["Y"]}) +
            make_raw_output([("Y", "Z")])
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert_edge(result["edges"], "Dirty", "Y", "low")
        assert_edge(result["edges"], "Y", "Z", "high")

    def test_all_edges_low_when_all_blocks_suspicious(self, adapter, tmp_py_project, base_config):
        # оба блока suspicious → все рёбра low
        #
        # Ожидаемый side-effect: 2 suspicious из 4 узлов = 50% > порога 35%
        # → адаптер эмитирует RuntimeWarning о высоком collision rate.
        # Фиксируем это как явный контракт через pytest.warns.
        raw = (
            make_raw_output([("A", "B")], extra_used={"A": ["B"]}) +
            make_raw_output([("C", "D")], extra_used={"C": ["D"]})
        )
        with pytest.warns(RuntimeWarning, match="high collision rate"):
            result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count_low"] == 2
        assert result["edge_count_high"] == 0

    def test_edge_count_high_low_sum_equals_edge_count(self, adapter, tmp_py_project, base_config):
        # инвариант: edge_count_high + edge_count_low == edge_count всегда
        raw = (
            make_raw_output([("Clean", "X"), ("Clean", "W")]) +
            make_raw_output([("Dirty", "Y"), ("Dirty", "Z")], extra_used={"Dirty": ["Y"]})
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count_high"] + result["edge_count_low"] == result["edge_count"]

    def test_no_edges_gives_zero_counters(self, adapter, tmp_py_project, base_config):
        # пустой вывод → все счётчики нулевые, нет исключений
        result = _run_with_output(adapter, tmp_py_project, base_config, "")
        assert_success_schema(result)
        assert result["edge_count_high"] == 0
        assert result["edge_count_low"] == 0
        assert result["edge_count"] == 0
