# test_deduplication.py — юнит-тесты дедупликации рёбер call-graph.
#
# Контракт дедупликации:
#   1. Дублирующиеся рёбра (одинаковые from+to) схлопываются в одно.
#   2. Правило разрешения конфликта confidence: если хотя бы одно из
#      дублирующихся рёбер имеет confidence="high", результирующее ребро
#      получает confidence="high" (оптимистичная стратегия).
#   3. Счётчики edge_count, edge_count_high, edge_count_low отражают
#      состояние ПОСЛЕ дедупликации.
#   4. Порядок рёбер в финальном списке не специфицирован — тесты
#      используют assert_edge, а не позиционный доступ.
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
# Группа 1: Базовая дедупликация
# ---------------------------------------------------------------------------

class TestEdgeDeduplication:

    def test_duplicate_high_high_collapses_to_one_high(self, adapter, tmp_py_project, base_config):
        # два идентичных чистых блока A→B: оба high, результат — одно ребро high
        raw = make_raw_output([("A", "B")]) + make_raw_output([("A", "B")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 1
        assert_edge(result["edges"], "A", "B", "high")
        assert result["edge_count_high"] == 1
        assert result["edge_count_low"] == 0

    def test_duplicate_low_low_collapses_to_one_low(self, adapter, tmp_py_project, base_config):
        # два suspicious блока A→B: оба low, результат — одно ребро low
        raw = (
            make_raw_output([("A", "B")], extra_used={"A": ["B"]}) +
            make_raw_output([("A", "B")], extra_used={"A": ["B"]})
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 1
        assert_edge(result["edges"], "A", "B", "low")
        assert result["edge_count_high"] == 0
        assert result["edge_count_low"] == 1

    def test_conflict_high_wins_over_low(self, adapter, tmp_py_project, base_config):
        # одно ребро A→B из чистого блока (high) + одно из suspicious (low):
        # оптимистичная стратегия — результат high
        raw = (
            make_raw_output([("A", "B")]) +                              # high
            make_raw_output([("A", "B")], extra_used={"A": ["B"]})      # low
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 1
        assert_edge(result["edges"], "A", "B", "high")
        assert result["edge_count_high"] == 1
        assert result["edge_count_low"] == 0

    def test_non_duplicate_edges_preserved(self, adapter, tmp_py_project, base_config):
        # A→B и A→C — разные рёбра, дедупликация не должна их трогать
        raw = make_raw_output([("A", "B"), ("A", "C")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 2
        assert_edge(result["edges"], "A", "B", "high")
        assert_edge(result["edges"], "A", "C", "high")

    def test_counters_reflect_post_deduplication_state(self, adapter, tmp_py_project, base_config):
        # 3 вхождения A→B (2 high + 1 low) + 1 уникальное A→C (high):
        # после дедупликации: A→B high, A→C high → edge_count=2, high=2, low=0
        raw = (
            make_raw_output([("A", "B")]) +                              # high
            make_raw_output([("A", "B")], extra_used={"A": ["B"]}) +    # low
            make_raw_output([("A", "B")]) +                              # high
            make_raw_output([("A", "C")])
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 2
        assert result["edge_count_high"] == 2
        assert result["edge_count_low"] == 0
        assert result["edge_count_high"] + result["edge_count_low"] == result["edge_count"]
