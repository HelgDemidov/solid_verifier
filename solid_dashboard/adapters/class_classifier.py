# ===================================================================================================
# Классификатор семантического типа класса (Class Classifier)
#
# Модуль вынесен из cohesion_adapter для соблюдения SRP:
# classify_class — чистая функция, не использует состояние адаптера,
# оперирует только переданным ast.ClassDef.
#
# Используется:
#   - CohesionAdapter._build_class_info (cohesion_adapter.py)
#   - unit-тесты: test_classify_class.py, test_cohesion_adapter_edge_cases.py
# ===================================================================================================

import ast
from typing import Set

# явно объявляем публичный API модуля 
__all__ = ["classify_class"]

def classify_class(class_node: ast.ClassDef) -> str:
    """
    Определяет семантический тип класса по AST-узлу.

    Чистая функция уровня модуля: не использует внешнее состояние,
    оперирует только переданным class_node.

    Возвращает одно из четырех значений:
      "interface"  — все non-dunder методы абстрактны (только @abstractmethod / pass / raise)
                     И класс наследуется от ABC или Protocol
      "abstract"   — наследуется от ABC/Protocol, но есть хотя бы один конкретный метод
      "dataclass"  — декоратор @dataclass ИЛИ базовый класс BaseModel / declarative Base
      "concrete"   — всё остальное (дефолт)

    Порядок проверок важен: dataclass проверяется первым, затем ABC/Protocol-иерархия.
    """
    # имена базовых классов (только простые Name и Attribute.attr, без полных путей)
    base_names: Set[str] = set()
    for base in class_node.bases:
        if isinstance(base, ast.Name):
            base_names.add(base.id)
        elif isinstance(base, ast.Attribute):
            # учитываем последний компонент: models.Model -> "Model"
            base_names.add(base.attr)

    # имена декораторов класса (только простые Name)
    class_decorator_names: Set[str] = set()
    for dec in class_node.decorator_list:
        if isinstance(dec, ast.Name):
            class_decorator_names.add(dec.id)
        elif isinstance(dec, ast.Attribute):
            class_decorator_names.add(dec.attr)

    # 1. dataclass: @dataclass ИЛИ BaseModel / Base в bases
    _DATACLASS_BASES = {"BaseModel", "Base", "DeclarativeBase", "DeclarativeBaseNoMeta"}
    if "dataclass" in class_decorator_names or base_names & _DATACLASS_BASES:
        return "dataclass"

    # 2. проверяем ABC/Protocol в иерархии
    _ABSTRACT_BASES = {"ABC", "Protocol", "ABCMeta"}
    is_abc_derived = bool(base_names & _ABSTRACT_BASES)

    if not is_abc_derived:
        # нет ABC/Protocol в bases — класс конкретный
        return "concrete"

    # 3. считаем abstractmethod-методы среди non-dunder методов
    # non-dunder: все методы, кроме __xxx__ (магические методы не участвуют в классификации)
    non_dunder_count = 0
    abstract_method_count = 0

    for node in class_node.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        method_name: str = node.name
        if method_name.startswith("__") and method_name.endswith("__"):
            continue
        non_dunder_count += 1

        for dec in node.decorator_list:
            if (isinstance(dec, ast.Name) and dec.id == "abstractmethod") or (
                isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod"
            ):
                abstract_method_count += 1
                break

    if non_dunder_count == 0:
        return "interface"
    if abstract_method_count == non_dunder_count:
        return "interface"
    return "abstract"
