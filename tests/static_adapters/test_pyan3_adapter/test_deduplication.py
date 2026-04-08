# test_deduplication.py — юнит-тесты дедупликации рёбер call-graph.
#
# Контракт дедупликации:
#   1. Дублирующиеся рёбра (одинаковые from+to) схлопываются в одно.
#   2. Правило разрешения конфликта confidence: пессимистичная стратегия —
#      если хотя бы одно из дублирующихся рёбер имеет confidence="low",
#      результирующее ребро получает confidence="low" ("low" заражает "high").
#   3. Счётчики edge_count, edge_count_high, edge_count_low отражают
#      состояние ПОСЛЕ дедупликации.
#   4. Порядок рёбер в финальном списке не специфицирован — тесты
#      используют assert_edge, а не позиционный доступ.
#
# Примечание о pytest.warns:
# Тесты, намеренно создающие suspicious-блоки, вызывают adapter.run() целиком,
# включая проверку collision_rate. В мини-фикстурах (2 узла) даже 1 suspicious
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
        #
        # Ожидаемый side-effect: 1 suspicious из 2 узлов = 50% > порога 35%
        # → адаптер эмитирует RuntimeWarning о высоком collision rate.
        # Фиксируем это как явный контракт через pytest.warns.
        raw = (
            make_raw_output([("A", "B")], extra_used={"A": ["B"]}) +
            make_raw_output([("A", "B")], extra_used={"A": ["B"]})
        )
        with pytest.warns(RuntimeWarning, match="high collision rate"):
            result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 1
        assert_edge(result["edges"], "A", "B", "low")
        assert result["edge_count_high"] == 0
        assert result["edge_count_low"] == 1

    def test_conflict_low_wins_over_high(self, adapter, tmp_py_project, base_config):
        # одно ребро A→B из чистого блока (high) + одно из suspicious (low):
        # пессимистичная стратегия — "low" заражает "high", результат low.
        #
        # Обоснование: pyan3 объединяет несколько сущностей с одинаковым коротким
        # именем в один text-блок (name collision). Если хотя бы одно вхождение
        # ребра A→B пришло из suspicious-блока, значит часть вхождений — ложные
        # (cross-attribution). Повышать уверенность до "high" на основании
        # другого вхождения семантически неверно.
        # Реализация: pyan3_adapter.py, функция run(), блок "Де-дупликация рёбер",
        # условие: if current_conf is None or (e["confidence"] == "low" and current_conf == "high").
        #
        # Ожидаемый side-effect: 1 suspicious из 2 узлов = 50% > порога 35%
        # → адаптер эмитирует RuntimeWarning о высоком collision rate.
        # Фиксируем это как явный контракт через pytest.warns.
        raw = (
            make_raw_output([("A", "B")]) +                              # high
            make_raw_output([("A", "B")], extra_used={"A": ["B"]})      # low
        )
        with pytest.warns(RuntimeWarning, match="high collision rate"):
            result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 1
        assert_edge(result["edges"], "A", "B", "low")
        assert result["edge_count_high"] == 0
        assert result["edge_count_low"] == 1

    def test_non_duplicate_edges_preserved(self, adapter, tmp_py_project, base_config):
        # A→B и A→C — разные рёбра, дедупликация не должна их трогать
        raw = make_raw_output([("A", "B"), ("A", "C")])
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 2
        assert_edge(result["edges"], "A", "B", "high")
        assert_edge(result["edges"], "A", "C", "high")

    def test_counters_reflect_post_deduplication_state(self, adapter, tmp_py_project, base_config):
        # Сценарий: 3 вхождения A→B (2 high + 1 low) + 1 уникальное Y→C (high).
        #
        # Почему источник Y, а не A:
        # _detect_suspicious_blocks читает весь raw как единый поток. Блок "A"
        # с кратным [U] B (part2) помечает "A" как suspicious — после чего ВСЕ
        # рёбра с src="A" получают confidence="low", включая A→C, что давало
        # high=0,low=2 вместо high=1,low=1.
        # Источник Y изолирован от suspicious-пометки A и остаётся clean (high).
        #
        # Итог после дедупликации:
        #   A→B: low (low заразил high по пессимистичной стратегии)
        #   Y→C: high
        #   edge_count=2, high=1, low=1
        raw = (
            make_raw_output([("A", "B")]) +                              # A→B high
            make_raw_output([("A", "B")], extra_used={"A": ["B"]}) +    # A→B low (делает A suspicious)
            make_raw_output([("A", "B")]) +                              # A→B high (дубль)
            make_raw_output([("Y", "C")])                                # Y→C high (изолированный)
        )
        result = _run_with_output(adapter, tmp_py_project, base_config, raw)
        assert_success_schema(result)
        assert result["edge_count"] == 2
        assert result["edge_count_high"] == 1
        assert result["edge_count_low"] == 1
        assert result["edge_count_high"] + result["edge_count_low"] == result["edge_count"]
