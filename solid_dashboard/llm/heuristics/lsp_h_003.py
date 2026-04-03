# LSP-H-003: isinstance() в методе, параметр которого аннотирован базовым типом.
#
# Метод обещает работать с любым Animal, но внутри делает isinstance(x, Dog).
# Это нарушение LSP со стороны потребителя: подтипы не взаимозаменяемы для него.
#
# Алгоритм:
#   1. Собираем параметры метода с аннотацией типа из ProjectMap (базовый тип).
#   2. Ищем isinstance(param, ...) в теле.
#   3. Если нашли — это нарушение.
#
# Ограничение: проверяем только простые аннотации (ast.Name), не Union/Optional.
# Это сознательный выбор: сложные аннотации редки в реальных нарушениях.

import ast
from typing import List

from ..types import ClassInfo, Finding, ProjectMap
from ._shared import _make_finding


def check(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
    project_map: ProjectMap,
) -> List[Finding]:
    # Все известные имена базовых классов и интерфейсов проекта
    known_base_names: set[str] = (
        set(project_map.classes.keys()) | set(project_map.interfaces.keys())
    )

    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # --- Шаг 1: параметры с аннотацией базового типа (кроме self/cls) ---
        annotated_base_params: set[str] = set()
        for arg in func.args.args:
            if arg.arg in ("self", "cls"):
                continue
            if arg.annotation is None:
                continue
            # Поддерживаем только простую аннотацию: def foo(self, x: Animal)
            if isinstance(arg.annotation, ast.Name) and arg.annotation.id in known_base_names:
                annotated_base_params.add(arg.arg)

        if not annotated_base_params:
            continue

        # --- Шаг 2: ищем isinstance(param, ...) в теле метода ---
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
                and len(node.args) >= 1
            ):
                continue

            # Первый аргумент isinstance() — проверяемый объект
            subject = node.args[0]
            if not (isinstance(subject, ast.Name) and subject.id in annotated_base_params):
                continue

            findings.append(_make_finding(
                rule="LSP-H-003",
                class_info=class_info,
                message=(
                    f"Method '{func.name}' checks isinstance() on a parameter "
                    f"annotated with a base type — type-dispatch against LSP"
                ),
                principle="LSP",
                explanation=(
                    f"'{func.name}' accepts '{subject.id}' typed as a project base "
                    f"class/interface, but uses isinstance() to branch on its concrete "
                    f"type. This means the method is not truly open to all subtypes — "
                    f"a violation of the Liskov Substitution Principle."
                ),
                suggestion=(
                    "Replace isinstance() branching with polymorphism: add a method "
                    "to the base type and override it in each subtype."
                ),
                method_name=func.name,
            ))
            break  # Одного finding на метод достаточно

    return findings
