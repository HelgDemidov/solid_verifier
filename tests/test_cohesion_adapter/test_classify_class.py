# ==============================================================================
# Блок B: unit-тесты для _classify_class (модульная функция)
#
# Проверяем корректную семантическую классификацию классов:
#   "dataclass"  — @dataclass / BaseModel / Base
#   "interface"  — ABC/Protocol, все non-dunder методы абстрактны (или методов нет)
#   "abstract"   — ABC/Protocol, но есть хотя бы один конкретный метод
#   "concrete"   — всё остальное
#
# После рефакторинга (a4a9e5e) _classify_class является модульной функцией,
# а не методом CohesionAdapter. Тесты вызывают её напрямую без экземпляра адаптера.
# ==============================================================================

import pytest

from solid_dashboard.adapters.cohesion_adapter import _classify_class


class TestClassifyClass:
    # @dataclass декоратор -> "dataclass"
    def test_dataclass_decorator(self, parse_class):
        node = parse_class("""
            from dataclasses import dataclass

            @dataclass
            class Foo:
                x: int
                y: str
        """)
        assert _classify_class(node) == "dataclass"

    # наследование от BaseModel (Pydantic) -> "dataclass"
    def test_basemodel_base(self, parse_class):
        node = parse_class("""
            class Foo(BaseModel):
                name: str
                age: int
        """)
        assert _classify_class(node) == "dataclass"

    # наследование от Base (SQLAlchemy DeclarativeBase) -> "dataclass"
    def test_declarative_base(self, parse_class):
        node = parse_class("""
            class Article(Base):
                __tablename__ = "articles"
                id: int
        """)
        assert _classify_class(node) == "dataclass"

    # ABC, все non-dunder методы с @abstractmethod -> "interface"
    def test_abc_all_abstract(self, parse_class):
        node = parse_class("""
            from abc import ABC, abstractmethod

            class IRepo(ABC):
                @abstractmethod
                def get(self, id: int): ...

                @abstractmethod
                def save(self, entity): ...
        """)
        assert _classify_class(node) == "interface"

    # ABC без non-dunder методов вообще -> "interface"
    def test_abc_no_methods(self, parse_class):
        node = parse_class("""
            from abc import ABC

            class IBase(ABC):
                pass
        """)
        assert _classify_class(node) == "interface"

    # ABC с одним конкретным методом -> "abstract"
    def test_abc_mixed_methods(self, parse_class):
        node = parse_class("""
            from abc import ABC, abstractmethod

            class BaseService(ABC):
                @abstractmethod
                def process(self): ...

                def validate(self):
                    return True
        """)
        assert _classify_class(node) == "abstract"

    # Protocol, все non-dunder @abstractmethod -> "interface"
    def test_protocol_all_abstract(self, parse_class):
        node = parse_class("""
            from typing import Protocol

            class IAnalyzer(Protocol):
                @abstractmethod
                def run(self, path: str): ...
        """)
        assert _classify_class(node) == "interface"

    # Protocol с конкретным методом -> "abstract"
    def test_protocol_with_concrete(self, parse_class):
        node = parse_class("""
            from typing import Protocol

            class IAnalyzer(Protocol):
                @abstractmethod
                def run(self): ...

                def name(self) -> str:
                    return "analyzer"
        """)
        assert _classify_class(node) == "abstract"

    # обычный класс без ABC/dataclass -> "concrete"
    def test_plain_class(self, parse_class):
        node = parse_class("""
            class UserService:
                def __init__(self, repo):
                    self.repo = repo

                def get_user(self, id: int):
                    return self.repo.get(id)
        """)
        assert _classify_class(node) == "concrete"

    # dunder-методы не считаются non-dunder, ABC без non-dunder -> "interface"
    def test_abc_only_dunder(self, parse_class):
        node = parse_class("""
            from abc import ABC

            class IBase(ABC):
                def __init__(self): ...
                def __str__(self): ...
        """)
        assert _classify_class(node) == "interface"
