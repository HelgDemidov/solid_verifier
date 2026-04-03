# LSP-H-002: переопределённый метод с пустым телом (только pass или docstring).
#
# Пустое тело переопределения — подкласс молча игнорирует контракт родителя.
# Вызывающий код, ожидающий поведения базового класса, не получает ничего.
# Это тихое нарушение LSP опаснее, чем raise NotImplementedError, потому что
# не проявляется в виде исключения.
#
# Абстрактные классы исключаются по той же причине, что и в LSP-H-001.

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

    override_names = {m.name for m in class_info.methods if m.is_override}
    if not override_names:
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if func.name not in override_names:
            continue

        body = func.body
        # Пустое тело = ровно один стейтмент; больше — уже что-то делается
        if len(body) != 1:
            continue

        stmt = body[0]
        is_pass = isinstance(stmt, ast.Pass)
        # Docstring-only: ast.Expr с ast.Constant строкового типа
        is_docstring_only = (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )

        if is_pass or is_docstring_only:
            body_desc = "pass" if is_pass else "docstring only"
            findings.append(_make_finding(
                rule="LSP-H-002",
                class_info=class_info,
                message=(
                    f"Overridden method '{func.name}' has an empty body "
                    f"({body_desc})"
                ),
                principle="LSP",
                explanation=(
                    f"Method '{func.name}' overrides a parent method but its body is "
                    f"effectively empty. This silently breaks the parent's contract: "
                    f"callers expecting behavior from the base class will get nothing."
                ),
                suggestion=(
                    "Implement the method's expected behavior, or use NotImplementedError "
                    "explicitly if the override is intentionally unsupported (though this "
                    "also violates LSP and should be reconsidered)."
                ),
                method_name=func.name,
            ))

    return findings
