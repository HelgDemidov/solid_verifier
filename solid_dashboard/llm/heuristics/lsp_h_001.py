# LSP-H-001: переопределённый метод бросает NotImplementedError.
#
# Сигнал нарушения LSP: подкласс явно отказывается от контракта родителя,
# делая подстановку невозможной — вызывающий код получит исключение там,
# где ожидал корректного поведения.
#
# Абстрактные классы (ABC, Protocol, interfaces) исключаются: для них
# raise NotImplementedError — законный способ объявить абстрактный метод.

import ast
from typing import List

from ..types import ClassInfo, Finding, ProjectMap
from ._shared import _is_abstract_class, _make_finding


def check(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
    project_map: ProjectMap,
) -> List[Finding]:
    # Абстрактные классы — контракты, а не нарушители LSP
    if _is_abstract_class(class_info, project_map):
        return []

    # Смотрим только на методы, помеченные is_override при Шаге 0
    override_names = {m.name for m in class_info.methods if m.is_override}
    if not override_names:
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if func.name not in override_names:
            continue

        for node in ast.walk(func):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if exc is None:
                continue

            # Паттерн 1: raise NotImplementedError (без аргументов)
            is_bare = isinstance(exc, ast.Name) and exc.id == "NotImplementedError"
            # Паттерн 2: raise NotImplementedError("msg")
            is_call = (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Name)
                and exc.func.id == "NotImplementedError"
            )

            if is_bare or is_call:
                findings.append(_make_finding(
                    rule="LSP-H-001",
                    class_info=class_info,
                    message=(
                        f"Overridden method '{func.name}' raises NotImplementedError — "
                        f"substitutability is broken"
                    ),
                    principle="LSP",
                    explanation=(
                        f"Method '{func.name}' overrides a parent method but raises "
                        f"NotImplementedError. Callers using the base type cannot "
                        f"substitute this class without catching unexpected exceptions."
                    ),
                    suggestion=(
                        "Implement the method according to the parent's contract, or "
                        "reconsider whether this class should extend the parent at all."
                    ),
                    method_name=func.name,
                ))
                break  # Одного finding на метод достаточно

    return findings
