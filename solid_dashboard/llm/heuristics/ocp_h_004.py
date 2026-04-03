# OCP-H-004: высокая цикломатическая сложность + isinstance() в одном методе.
#
# Двойной сигнал: метод и сложный (много путей исполнения), и содержит
# тип-диспетч через isinstance(). Это сильнее, чем каждый признак по отдельности.
# Эвристика дополняет OCP-H-001/002: она находит isinstance() в методах, где
# диспетч не оформлен как явная if/elif-цепочка, а перемешан со сложной логикой.
#
# Порог CC: _OCP_H004_CC_THRESHOLD = 5 (настраивается здесь).
# Использует _iter_method_nodes для корректного подсчёта CC без вложенных функций.

import ast
from typing import List

from ..types import ClassInfo, Finding
from ._shared import _compute_method_cc, _iter_method_nodes, _make_finding

# Минимальная цикломатическая сложность для срабатывания эвристики
_OCP_H004_CC_THRESHOLD: int = 5


def check(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # --- Шаг 1: цикломатическая сложность метода ---
        cc = _compute_method_cc(func)
        if cc < _OCP_H004_CC_THRESHOLD:
            continue

        # --- Шаг 2: наличие isinstance() в теле метода ---
        # Используем _iter_method_nodes, чтобы не захватывать вложенные функции
        has_isinstance = False
        for node in _iter_method_nodes(func):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                has_isinstance = True
                break

        if not has_isinstance:
            continue

        # --- Оба условия выполнены: высокая CC + isinstance ---
        findings.append(_make_finding(
            rule="OCP-H-004",
            class_info=class_info,
            message=(
                f"Method '{func.name}' has cyclomatic complexity {cc} "
                f"and uses isinstance() — mixed type-dispatch in complex method"
            ),
            principle="OCP",
            explanation=(
                f"'{func.name}' has a cyclomatic complexity of {cc} "
                f"(threshold: {_OCP_H004_CC_THRESHOLD}) and contains isinstance() "
                f"checks. High complexity combined with type-based branching "
                f"suggests this method handles multiple responsibilities, "
                f"each specific to a concrete type — a potential OCP violation."
            ),
            suggestion=(
                f"Consider extracting type-specific behavior into subclasses "
                f"or strategy objects. The '{func.name}' method should ideally "
                f"work with an abstraction, not branch on concrete types."
            ),
            method_name=func.name,
        ))
        # Одного finding на метод достаточно

    return findings
