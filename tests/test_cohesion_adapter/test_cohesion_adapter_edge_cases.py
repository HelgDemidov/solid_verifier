# ---------------------------------------------------------------------------
# Интеграционные тесты граничных случаев CohesionAdapter.
# Каждый тест создает временный файл с Python-кодом, запускает адаптер
# и проверяет конкретное свойство выходного словаря.
#
# Группы:
#   TestLcom4GraphEdgeCases    — граничные случаи графа LCOM4 (Блок A)
#   TestClassKindClassification — классификация class_kind (Блок B)
#   TestNestedClasses           — вложенные классы (Блок C)
# ---------------------------------------------------------------------------

import ast
import textwrap
from pathlib import Path

import pytest

from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _run(tmp_path: Path, source: str, config: dict | None = None) -> dict:
    """Записывает source в target.py, запускает адаптер, возвращает результат."""
    (tmp_path / "target.py").write_text(textwrap.dedent(source), encoding="utf-8")
    adapter = CohesionAdapter()
    return adapter.run(str(tmp_path), context={}, config=config or {})


def _class_by_name(result: dict, name: str) -> dict:
    """Возвращает запись из result['classes'] по имени класса."""
    matches = [c for c in result["classes"] if c["name"] == name]
    assert matches, f"Класс '{name}' не найден в result['classes']"
    return matches[0]


# ===========================================================================
# Блок A — граничные случаи графа LCOM4
# ===========================================================================

class TestLcom4GraphEdgeCases:

    def test_single_method_class_lcom4_is_one(self, tmp_path):
        # Класс с одним методом: LCOM4 = 1 по определению (одна компонента)
        result = _run(tmp_path, """
            class Service:
                def __init__(self):
                    self.value = 0

                def get_value(self):
                    return self.value
        """)
        cls = _class_by_name(result, "Service")
        assert cls["cohesion_score"] == 1.0
        assert cls["methods_count"] == 1  # __init__ исключен, остается get_value

    def test_shared_attribute_no_calls_lcom4_is_one(self, tmp_path):
        # Все методы используют один атрибут, но не вызывают друг друга:
        # ребра строятся через пересечение used_attributes — LCOM4 = 1
        result = _run(tmp_path, """
            class Repo:
                def __init__(self):
                    self.db = None

                def read(self):
                    return self.db

                def write(self, val):
                    self.db = val

                def delete(self):
                    self.db = None
        """)
        cls = _class_by_name(result, "Repo")
        assert cls["cohesion_score"] == 1.0, (
            "Все методы разделяют атрибут 'db' — должна быть одна компонента"
        )

    def test_no_shared_attributes_no_calls_lcom4_equals_method_count(self, tmp_path):
        # Каждый метод использует уникальный атрибут, вызовов нет:
        # каждый метод — отдельная компонента, LCOM4 = N
        result = _run(tmp_path, """
            class Fragmented:
                def __init__(self):
                    self.a = 1
                    self.b = 2
                    self.c = 3

                def use_a(self):
                    return self.a

                def use_b(self):
                    return self.b

                def use_c(self):
                    return self.c
        """)
        cls = _class_by_name(result, "Fragmented")
        assert cls["cohesion_score"] == 3.0, (
            "Три независимые компоненты: LCOM4 = 3"
        )
        assert cls["methods_count"] == 3

    def test_property_methods_excluded_from_lcom4_graph(self, tmp_path):
        # @property-методы исключаются из графа LCOM4 полностью
        result = _run(tmp_path, """
            class User:
                def __init__(self):
                    self.first = ""
                    self.last = ""

                @property
                def full_name(self):
                    return f"{self.first} {self.last}"

                def reset(self):
                    self.first = ""
                    self.last = ""
        """)
        cls = _class_by_name(result, "User")
        # full_name — @property, исключена; остается только reset
        assert cls["methods_count"] == 1
        assert cls["cohesion_score"] == 1.0

    def test_classmethod_included_in_graph_via_cls(self, tmp_path):
        # @classmethod остается в графе; cls.attr — это атрибут класса, не self.xxx
        # classmethod с cls.attr не добавит used_attributes (нет self-like для cls.attr)
        # но если classmethod вызывает другой метод — ребро строится
        result = _run(tmp_path, """
            class Factory:
                default_name = "default"

                def __init__(self):
                    self.name = Factory.default_name

                @classmethod
                def create(cls):
                    return cls()

                def get_name(self):
                    return self.name
        """)
        cls = _class_by_name(result, "Factory")
        # create — @classmethod, get_name — instance-метод; нет общих атрибутов/вызовов
        assert cls["methods_count"] == 2  # create + get_name (__init__ исключен)
        assert cls["cohesion_score"] == 2.0, (
            "@classmethod и instance-метод без связей — две компоненты"
        )

    def test_staticmethod_included_with_no_self_attributes(self, tmp_path):
        # @staticmethod исключает self-like регистрацию — used_attributes будет пустым
        # если у статического метода нет self.xxx, он образует отдельную компоненту
        result = _run(tmp_path, """
            class Util:
                def __init__(self):
                    self.value = 0

                @staticmethod
                def compute(x):
                    return x * 2

                def apply(self):
                    return self.value + Util.compute(1)
        """)
        cls = _class_by_name(result, "Util")
        # compute — @staticmethod, нет used_attributes; apply — использует value
        # apply вызывает compute через Util.compute — это Attribute на Name("Util"),
        # а не self.compute — поэтому вызов НЕ регистрируется в called_methods
        # ожидаем две компоненты: compute отдельно, apply отдельно
        assert cls["methods_count"] == 2
        assert cls["cohesion_score"] == 2.0

    def test_empty_method_excluded_from_graph(self, tmp_path):
        # Метод с телом pass — is_empty=True, исключается из графа
        result = _run(tmp_path, """
            class Base:
                def __init__(self):
                    self.data = []

                def process(self):
                    pass

                def load(self):
                    return self.data
        """)
        cls = _class_by_name(result, "Base")
        # process — is_empty, исключен; остается только load
        assert cls["methods_count"] == 1
        assert cls["cohesion_score"] == 1.0

    def test_methods_connected_by_calls_only_lcom4_is_one(self, tmp_path):
        # Методы не делят атрибуты, но один вызывает другой — ребро по вызову
        result = _run(tmp_path, """
            class Pipeline:
                def step_one(self):
                    return 1

                def step_two(self):
                    return self.step_one() + 1
        """)
        cls = _class_by_name(result, "Pipeline")
        assert cls["cohesion_score"] == 1.0, (
            "step_two вызывает step_one — одна компонента"
        )


# ===========================================================================
# Блок B — классификация class_kind
# ===========================================================================

class TestClassKindClassification:

    def test_dataclass_decorator_yields_kind_dataclass(self, tmp_path):
        # @dataclass → class_kind = "dataclass", excluded_from_aggregation = True
        result = _run(tmp_path, """
            from dataclasses import dataclass

            @dataclass
            class Point:
                x: float
                y: float

                def distance_to(self, other):
                    return ((self.x - other.x)**2 + (self.y - other.y)**2) ** 0.5
        """)
        cls = _class_by_name(result, "Point")
        assert cls["class_kind"] == "dataclass"
        assert cls["excluded_from_aggregation"] is True

    def test_protocol_all_abstract_yields_kind_interface(self, tmp_path):
        # class Foo(Protocol) только с абстрактными методами → "interface"
        result = _run(tmp_path, """
            from typing import Protocol
            from abc import abstractmethod

            class IReader(Protocol):
                @abstractmethod
                def read(self) -> str: ...

                @abstractmethod
                def close(self) -> None: ...
        """)
        cls = _class_by_name(result, "IReader")
        assert cls["class_kind"] == "interface"
        assert cls["excluded_from_aggregation"] is True

    def test_protocol_with_concrete_method_yields_kind_abstract(self, tmp_path):
        # Protocol с одним конкретным методом → "abstract", не "interface"
        # Это специфика _classify_class: наличие хотя бы одного non-abstract метода
        result = _run(tmp_path, """
            from typing import Protocol
            from abc import abstractmethod

            class MixedProtocol(Protocol):
                @abstractmethod
                def required(self) -> str: ...

                def optional(self) -> str:
                    return "default"
        """)
        cls = _class_by_name(result, "MixedProtocol")
        assert cls["class_kind"] == "abstract"
        assert cls["excluded_from_aggregation"] is True

    def test_typeddict_yields_kind_concrete(self, tmp_path):
        # TypedDict не в _DATACLASS_BASES и не в _ABSTRACT_BASES →
        # классифицируется как "concrete" и попадает в агрегаты
        # Это задокументированное поведение (не баг, а известное ограничение)
        result = _run(tmp_path, """
            from typing import TypedDict

            class Config(TypedDict):
                host: str
                port: int
        """)
        # Config не имеет методов (methods_count == 0) — не попадет в class_results вообще
        # Проверяем через total_classes_analyzed == 0
        assert result["total_classes_analyzed"] == 0, (
            "TypedDict без методов не должен попасть в class_results; "
            "при добавлении методов будет classifed как 'concrete'"
        )

    def test_typeddict_with_method_classified_as_concrete(self, tmp_path):
        # TypedDict с методом — явная проверка что он concrete, не dataclass
        # Это задокументированный пробел: TypedDict не распознается как исключаемый
        result = _run(tmp_path, """
            from typing import TypedDict

            class Config(TypedDict):
                host: str
                port: int

                def validate(self) -> bool:
                    return bool(self.host)
        """)
        cls = _class_by_name(result, "Config")
        assert cls["class_kind"] == "concrete", (
            "Известное ограничение: TypedDict классифицируется как 'concrete', "
            "а не как 'dataclass'. Если поведение изменится — обновить тест."
        )
        assert cls["excluded_from_aggregation"] is False

    def test_abc_subclass_all_abstract_yields_interface(self, tmp_path):
        # ABC со всеми абстрактными non-dunder методами → "interface"
        result = _run(tmp_path, """
            from abc import ABC, abstractmethod

            class IRepository(ABC):
                @abstractmethod
                def find(self, id: int): ...

                @abstractmethod
                def save(self, entity): ...
        """)
        cls = _class_by_name(result, "IRepository")
        assert cls["class_kind"] == "interface"

    def test_abc_subclass_mixed_methods_yields_abstract(self, tmp_path):
        # ABC с одним конкретным методом → "abstract"
        result = _run(tmp_path, """
            from abc import ABC, abstractmethod

            class BaseService(ABC):
                @abstractmethod
                def process(self): ...

                def log(self):
                    print("logging")
        """)
        cls = _class_by_name(result, "BaseService")
        assert cls["class_kind"] == "abstract"

    def test_non_concrete_classes_excluded_from_aggregation_counts(self, tmp_path):
        # Агрегаты считаются только по concrete-классам
        # Interface + abstract не должны влиять на mean_cohesion_all
        result = _run(tmp_path, """
            from abc import ABC, abstractmethod

            class IService(ABC):
                @abstractmethod
                def execute(self): ...

            class ConcreteService:
                def __init__(self):
                    self.state = None

                def execute(self):
                    self.state = "done"

                def reset(self):
                    self.state = None
        """)
        assert result["concrete_classes_count"] == 1
        assert result["mean_cohesion_all"] == 1.0  # ConcreteService связный

    def test_low_cohesion_excluded_count_for_non_concrete(self, tmp_path):
        # non-concrete класс с LCOM4 > threshold попадает в low_cohesion_excluded_count
        result = _run(tmp_path, """
            from abc import ABC, abstractmethod

            class AbstractSplitService(ABC):
                def __init__(self):
                    self.alpha = 1
                    self.beta = 2

                def use_alpha(self):
                    return self.alpha

                def use_beta(self):
                    return self.beta
        """)
        # AbstractSplitService: ABC без abstractmethod-методов — kind = "abstract"
        # use_alpha + use_beta не делят атрибуты, не вызывают друг друга → LCOM4 = 2
        assert result["low_cohesion_count"] == 0, "abstract не должен попасть в low_cohesion_count"
        assert result["low_cohesion_excluded_count"] == 1
        assert result["low_cohesion_excluded_classes"][0]["name"] == "AbstractSplitService"


# ===========================================================================
# Блок C — вложенные классы
# ===========================================================================

class TestNestedClasses:

    def test_inner_class_collected_independently(self, tmp_path):
        # ast.walk обходит все ClassDef включая вложенные;
        # Inner должен появиться в result['classes'] как самостоятельная запись
        result = _run(tmp_path, """
            class Outer:
                def __init__(self):
                    self.value = 0

                def outer_method(self):
                    return self.value

                class Inner:
                    def __init__(self):
                        self.data = []

                    def inner_method(self):
                        return self.data
        """)
        names = [c["name"] for c in result["classes"]]
        assert "Outer" in names, "Outer должен быть в результатах"
        assert "Inner" in names, "Inner должен быть в результатах"

    def test_inner_class_has_independent_lcom4(self, tmp_path):
        # Inner не знает об атрибутах Outer — его LCOM4 считается независимо
        result = _run(tmp_path, """
            class Outer:
                def __init__(self):
                    self.x = 0
                    self.y = 0

                def get_x(self):
                    return self.x

                def get_y(self):
                    return self.y

                class Inner:
                    def __init__(self):
                        self.z = 0

                    def get_z(self):
                        return self.z
        """)
        outer = _class_by_name(result, "Outer")
        inner = _class_by_name(result, "Inner")

        # Outer: get_x использует x, get_y использует y — нет пересечения → LCOM4 = 2
        assert outer["cohesion_score"] == 2.0, "Outer: два несвязных метода"
        # Inner: один метод → LCOM4 = 1
        assert inner["cohesion_score"] == 1.0, "Inner: один метод → LCOM4 = 1"

    def test_inner_class_does_not_inherit_outer_attributes(self, tmp_path):
        # Inner не должен видеть self.x из Outer даже при одинаковых именах атрибутов
        # Это проверяет что MRO-обогащение не смешивает вложенные классы
        result = _run(tmp_path, """
            class Outer:
                def __init__(self):
                    self.shared = 0

                def outer_use(self):
                    return self.shared

                class Inner:
                    def inner_use(self):
                        # Inner не имеет __init__ с self.shared
                        # used_attributes должно быть пустым
                        return 42
        """)
        inner = _class_by_name(result, "Inner")
        # inner_use не использует атрибуты — methods_count = 1, cohesion_score = 1.0
        assert inner["methods_count"] == 1
        assert inner["cohesion_score"] == 1.0

    def test_doubly_nested_class_collected(self, tmp_path):
        # ast.walk рекурсивен — трёхуровневое вложение тоже обходится
        result = _run(tmp_path, """
            class Level1:
                class Level2:
                    class Level3:
                        def deep_method(self):
                            return "deep"
        """)
        names = [c["name"] for c in result["classes"]]
        assert "Level3" in names, "Трёхуровневый nested класс должен быть найден"

    def test_concrete_count_includes_all_levels(self, tmp_path):
        # Все уровни вложения с concrete-классами включаются в concrete_classes_count
        result = _run(tmp_path, """
            class Outer:
                def __init__(self):
                    self.a = 1

                def method_a(self):
                    return self.a

                class Inner:
                    def __init__(self):
                        self.b = 2

                    def method_b(self):
                        return self.b
        """)
        assert result["concrete_classes_count"] == 2, (
            "Outer и Inner — оба concrete, оба должны учитываться"
        )