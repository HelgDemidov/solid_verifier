# OCP-H-001: цепочка if/elif с тремя и более isinstance()-ветвями.
#
# Явный тип-диспетчер: добавление нового типа требует прямого изменения метода.
# Это нарушение принципа открытости/закрытости.
#
# Триггер: >= 3 ветвей цепочки if/elif, каждая из которых содержит isinstance().
# Порог в 3 выбран намеренно: 1–2 ветви — приемлемый guard-код, 3+ — smell.
#
# Отличие от монолитной версии: считаем не длину цепочки, а количество ветвей
# именно с isinstance(), что устраняет ложные срабатывания при наличии guard-условий
# без isinstance() в начале цепочки.

import ast
from typing import List

from ..types import ClassInfo, Finding
from ._shared import _count_elif_chain, _has_isinstance_call, _make_finding

# Минимальное число ветвей с isinstance() для срабатывания эвристики
_OCP_H001_THRESHOLD: int = 3


def _count_isinstance_branches(if_node: ast.If) -> int:
    # Считает ветви цепочки if/elif, в условии которых есть вызов isinstance().
    # Именно это число используется как триггер, а не общая длина цепочки
    count = 0
    current: ast.If | None = if_node
    while current is not None:
        if _has_isinstance_call(current.test):
            count += 1
        # Переходим к следующей elif-ветви, если она есть
        if (
            current.orelse
            and len(current.orelse) == 1
            and isinstance(current.orelse[0], ast.If)
        ):
            current = current.orelse[0]
        else:
            break
    return count


def check(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Собираем id внутренних elif-нод, чтобы не проверять их как корневые if
        elif_nodes: set[int] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.If):
                if (
                    node.orelse
                    and len(node.orelse) == 1
                    and isinstance(node.orelse[0], ast.If)
                ):
                    elif_nodes.add(id(node.orelse[0]))

        # Проходим только по корневым if-нодам (не elif)
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue
            if id(node) in elif_nodes:
                continue

            isinstance_branch_count = _count_isinstance_branches(node)
            if isinstance_branch_count < _OCP_H001_THRESHOLD:
                continue

            # chain_length — для информативного explanation, не для триггера
            chain_length = _count_elif_chain(node)
            findings.append(_make_finding(
                rule="OCP-H-001",
                class_info=class_info,
                message=(
                    f"Method '{func.name}' contains a type-dispatch chain "
                    f"({isinstance_branch_count} isinstance checks) — potential OCP violation"
                ),
                principle="OCP",
                explanation=(
                    f"'{func.name}' uses an if/elif chain of length {chain_length}, "
                    f"where {isinstance_branch_count} branches check concrete types via "
                    f"isinstance(). Mixed type-dispatch like this means adding a new type "
                    f"requires modifying this method directly, violating Open-Closed Principle."
                ),
                suggestion=(
                    "Consider replacing the type-checks with polymorphism: "
                    "extract each branch into a subclass or strategy, and let "
                    "Python's dispatch mechanism handle the routing."
                ),
                method_name=func.name,
            ))
            break  # Одного finding на метод достаточно

    return findings
