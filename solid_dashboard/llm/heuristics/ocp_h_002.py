# OCP-H-002: конструкция match/case с тремя и более type-ветвями (Python 3.10+).
#
# match/case на типах — аналог isinstance-цепочки: добавление нового типа
# требует прямого изменения метода. Порог — те же 3 ветви, что и в OCP-H-001.
#
# Если интерпретатор не поддерживает match/case (Python < 3.10), ast.Match
# отсутствует — эвристика немедленно возвращает пустой список.

import ast
from typing import List, cast

from ..types import ClassInfo, Finding
from ._shared import _make_finding

# Минимальное число type-ветвей в match/case для срабатывания эвристики
_OCP_H002_THRESHOLD: int = 3


def check(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    findings: List[Finding] = []

    # Получаем классы AST для match/case — доступны только в Python 3.10+
    match_cls = getattr(ast, "Match", None)
    match_class_cls = getattr(ast, "MatchClass", None)
    match_or_cls = getattr(ast, "MatchOr", None)

    # Если интерпретатор не поддерживает match/case, эвристика ничего не делает
    if match_cls is None or match_class_cls is None:
        return findings

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for node in ast.walk(func):
            if not isinstance(node, match_cls):
                continue

            # cast нужен для подавления ошибки Pylance: "у AST нет .cases"
            match_node = cast(ast.AST, node)
            type_case_count = 0

            for case in getattr(match_node, "cases", []):
                # Паттерн вида case MyClass(): ...
                if isinstance(case.pattern, match_class_cls):
                    type_case_count += 1
                # Паттерн вида case A() | B(): ... — каждый подпаттерн считается
                if match_or_cls is not None and isinstance(case.pattern, match_or_cls):
                    for sub in case.pattern.patterns:
                        if isinstance(sub, match_class_cls):
                            type_case_count += 1

            if type_case_count >= _OCP_H002_THRESHOLD:
                findings.append(_make_finding(
                    rule="OCP-H-002",
                    class_info=class_info,
                    message=(
                        f"Method '{func.name}' uses match/case with {type_case_count} "
                        f"type branches — potential OCP violation"
                    ),
                    principle="OCP",
                    explanation=(
                        f"'{func.name}' dispatches behavior via a match/case statement "
                        f"with {type_case_count} type-specific branches. Adding a new "
                        f"type requires modifying this method directly — a violation of OCP."
                    ),
                    suggestion=(
                        "Consider replacing type-dispatch match/case with polymorphism "
                        "or a registration/strategy pattern."
                    ),
                    method_name=func.name,
                ))
                break  # Одного finding на метод достаточно

    return findings
