# LSP-H-004: __init__ подкласса без вызова super().__init__().
#
# Если родитель устанавливает атрибуты экземпляра в __init__, а подкласс
# не вызывает super().__init__(), эти атрибуты не будут инициализированы.
# Код, полагающийся на контракт родителя, получит AttributeError или
# молчаливо некорректное состояние — нарушение LSP.
#
# Исключения:
#   - @dataclass: __init__ генерируется автоматически, super() не нужен
#   - Классы из _LSP_H004_EXCLUDED_PARENTS (object, ABC, Protocol и др.):
#     их __init__ не несёт значимой инициализации

import ast
from typing import List

from ..types import ClassInfo, Finding
from ._shared import _make_finding

# Родительские классы, для которых отсутствие super().__init__() допустимо
_LSP_H004_EXCLUDED_PARENTS: set[str] = {
    "object",
    "ABC",
    "Protocol",
    "TypedDict",
    "NamedTuple",
    "BaseModel",
}


def _has_dataclass_decorator(class_node: ast.ClassDef) -> bool:
    # Проверяем наличие @dataclass или @dataclasses.dataclass в декораторах класса.
    # Поддерживаем три формы: @dataclass, @dataclasses.dataclass, @dataclass(frozen=True)
    for dec in class_node.decorator_list:
        if isinstance(dec, ast.Name) and dec.id == "dataclass":
            return True
        if isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
            return True
        if isinstance(dec, ast.Call):
            if isinstance(dec.func, ast.Name) and dec.func.id == "dataclass":
                return True
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == "dataclass":
                return True
    return False


def check(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    # Dataclass: __init__ генерируется автоматически, super() не нужен
    if _has_dataclass_decorator(class_node):
        return []

    # Только для реальных подклассов (пустая строка = динамическая база)
    real_parents = [p for p in class_info.parent_classes if p != ""]
    if not real_parents:
        return []

    # Если все родители — исключённые базовые типы, нарушения нет
    if all(parent in _LSP_H004_EXCLUDED_PARENTS for parent in real_parents):
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        # async __init__ в Python невозможен — проверяем только FunctionDef
        if not isinstance(func, ast.FunctionDef) or func.name != "__init__":
            continue

        has_super_init = False

        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            # Паттерн super().__init__(...):
            # Call(func=Attribute(value=Call(func=Name(id='super')), attr='__init__'))
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
            ):
                has_super_init = True
                break

        if not has_super_init:
            findings.append(_make_finding(
                rule="LSP-H-004",
                class_info=class_info,
                message=(
                    f"__init__ in '{class_info.name}' does not call super().__init__() "
                    f"— parent state may be uninitialized"
                ),
                principle="LSP",
                explanation=(
                    f"'{class_info.name}' inherits from {real_parents} but its __init__ "
                    f"does not call super().__init__(). If the parent sets instance "
                    f"attributes in __init__, they will be missing in this subclass, "
                    f"potentially breaking Liskov Substitution."
                ),
                suggestion=(
                    "Add super().__init__() (with appropriate arguments) as the first "
                    "statement in __init__, unless you intentionally want to skip "
                    "parent initialization (document this explicitly if so)."
                ),
                method_name=func.name,
            ))

    return findings
