# test_node_classification.py — unit-тесты классификации узлов графа вызовов.
#
# Тестируемая логика (pyan3_adapter.py, шаг «Разделение узлов»):
#
#   root_nodes — узлы без входящих ребер, но с исходящими (entry points)
#   dead_nodes — узлы без входящих И без исходящих ребер (мертвый код)
#   «нормальные» узлы — все остальные (есть входящие или входящие+исходящие)
#
# Классификация работает чисто над структурой ребер после парсинга;
# она не зависит от confidence и не вызывает subprocess.
# Поэтому тесты используют make_raw_output + мок subprocess —
# никакого реального pyan3 не требуется.
#
# Кейсы:
#   1. Линейная цепочка A→B→C: A — root, B — нормальный, C — dead
#   2. Два независимых root-узла в одном raw_output
#   3. Изолированный узел (нет ребер): попадает в dead_nodes
#   4. Узел с только входящими ребрами: нормальный (не root, не dead)
#   5. Все узлы связаны в цикл: ни root, ни dead
#   6. root_nodes и dead_nodes отсортированы лексикографически
#   7. Пустой граф (нет узлов): оба списка пусты, счетчики нулевые
import pytest
from unittest.mock import patch
from pathlib import Path

from .helpers import make_raw_output, assert_success_schema


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательный запуск через subprocess mock
# ──────────────────────────────────────────────────────────────────────────────

def _run_with_raw(adapter, tmp_py_project: Path, raw_output: str) -> dict:
    # Подменяем subprocess.run, чтобы адаптер «получил» заготовленный raw_output
    mock_cp = type("CP", (), {"returncode": 0, "stdout": raw_output, "stderr": ""})()
    with patch("solid_dashboard.adapters.pyan3_adapter.subprocess.run", return_value=mock_cp):
        return adapter.run(str(tmp_py_project), {}, {"ignore_dirs": []})


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 1: линейная цепочка A→B→C
# ──────────────────────────────────────────────────────────────────────────────

def test_linear_chain_root_and_leaf(adapter, tmp_py_project):
    # A→B→C: A — единственный root (нет входящих, есть исходящие),
    # B — нормальный (входящие от A, исходящие к C),
    # C — leaf без исходящих: в dead_nodes НЕ попадает, т.к. есть входящее от B
    raw = make_raw_output([("A", "B"), ("B", "C")])
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert result["root_nodes"] == ["A"]
    assert result["root_node_count"] == 1
    # C имеет входящее ребро от B → не dead
    assert "C" not in result["dead_nodes"]
    assert result["dead_node_count"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 2: два независимых root-узла
# ──────────────────────────────────────────────────────────────────────────────

def test_two_independent_root_nodes(adapter, tmp_py_project):
    # Entry1→Shared и Entry2→Shared: оба entry не имеют входящих, Shared — нормальный
    raw = make_raw_output([("Entry1", "Shared"), ("Entry2", "Shared")])
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert "Entry1" in result["root_nodes"]
    assert "Entry2" in result["root_nodes"]
    assert result["root_node_count"] == 2
    # Shared имеет два входящих ребра → не root
    assert "Shared" not in result["root_nodes"]
    assert result["dead_node_count"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 3: изолированный узел (нет ребер) → dead_nodes
# ──────────────────────────────────────────────────────────────────────────────

def test_isolated_node_is_dead(adapter, tmp_py_project):
    # Граф: A→B и изолированный Orphan без ребер.
    # make_raw_output строит только A→B; Orphan добавляем вручную как блок без [U]
    raw = make_raw_output([("A", "B")]) + "Orphan\n"
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert "Orphan" in result["dead_nodes"]
    assert result["dead_node_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 4: узел только с входящими ребрами — нормальный (не root, не dead)
# ──────────────────────────────────────────────────────────────────────────────

def test_sink_node_not_root_not_dead(adapter, tmp_py_project):
    # A→Sink и B→Sink: у Sink есть входящие, исходящих нет
    raw = make_raw_output([("A", "Sink"), ("B", "Sink")])
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert "Sink" not in result["root_nodes"]
    assert "Sink" not in result["dead_nodes"]


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 5: цикл A→B→C→A — ни root, ни dead
# ──────────────────────────────────────────────────────────────────────────────

def test_cycle_no_root_no_dead(adapter, tmp_py_project):
    # Замкнутый цикл: каждый узел имеет и входящее, и исходящее ребро
    raw = make_raw_output([("A", "B"), ("B", "C"), ("C", "A")])
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert result["root_nodes"] == []
    assert result["root_node_count"] == 0
    assert result["dead_nodes"] == []
    assert result["dead_node_count"] == 0


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 6: сортировка root_nodes и dead_nodes лексикографическая
# ──────────────────────────────────────────────────────────────────────────────

def test_classification_lists_are_sorted(adapter, tmp_py_project):
    # Несколько root-узлов и dead-узлов — результат должен быть отсортирован
    # Root: Zebra→X, Alpha→Y (у Zebra и Alpha нет входящих)
    # Dead: добавляем три изолированных блока
    raw = (
        make_raw_output([("Zebra", "X"), ("Alpha", "Y")])
        + "Orphan_C\nOrphan_A\nOrphan_B\n"
    )
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert result["root_nodes"] == sorted(result["root_nodes"])
    assert result["dead_nodes"] == sorted(result["dead_nodes"])
    # Конкретные ожидаемые root: Alpha и Zebra
    assert result["root_nodes"] == ["Alpha", "Zebra"]
    # Конкретные dead: Orphan_A, Orphan_B, Orphan_C
    assert result["dead_nodes"] == ["Orphan_A", "Orphan_B", "Orphan_C"]


# ──────────────────────────────────────────────────────────────────────────────
# Кейс 7: пустой граф — нет узлов вообще
# ──────────────────────────────────────────────────────────────────────────────

def test_empty_raw_output_no_nodes(adapter, tmp_py_project):
    # pyan3 вернул пустой stdout (корректный exit 0, но без узлов и ребер)
    raw = ""
    result = _run_with_raw(adapter, tmp_py_project, raw)

    assert_success_schema(result)
    assert result["root_nodes"] == []
    assert result["root_node_count"] == 0
    assert result["dead_nodes"] == []
    assert result["dead_node_count"] == 0
    assert result["node_count"] == 0
    assert result["edge_count"] == 0
