"""
Тесты Шага 2: build_project_map.
Покрывают все тест-кейсы и edge cases из плана реализации.
"""

import textwrap
import tempfile
from pathlib import Path

import pytest

from tools.solid_verifier.solid_dashboard.llm.ast_parser import build_project_map


# ---------------------------------------------------------------------------
# Вспомогательная фикстура: записывает несколько файлов во временную директорию
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_project(tmp_path):
    """Возвращает фабрику, которая создаёт .py-файлы во временной директории."""
    def _write(filename: str, source: str) -> str:
        path = tmp_path / filename
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return str(path)
    return _write


# ---------------------------------------------------------------------------
# 1. Одиночный класс без наследования
# ---------------------------------------------------------------------------

class TestSimpleClass:
    def test_class_in_map(self, tmp_project):
        f = tmp_project("animal.py", """
            class Animal:
                def speak(self) -> str:
                    return "sound"
        """)
        pm = build_project_map([f])
        assert "Animal" in pm.classes

    def test_empty_parent_classes(self, tmp_project):
        f = tmp_project("animal.py", """
            class Animal:
                pass
        """)
        pm = build_project_map([f])
        assert pm.classes["Animal"].parent_classes == []

    def test_source_code_captured(self, tmp_project):
        f = tmp_project("animal.py", """
            class Animal:
                def speak(self) -> str:
                    return "sound"
        """)
        pm = build_project_map([f])
        assert "def speak" in pm.classes["Animal"].source_code
        assert "class Animal" in pm.classes["Animal"].source_code

    def test_file_path_stored(self, tmp_project):
        f = tmp_project("animal.py", "class Animal: pass")
        pm = build_project_map([f])
        assert pm.classes["Animal"].file_path == f


# ---------------------------------------------------------------------------
# 2. Наследование: parent_classes и is_override
# ---------------------------------------------------------------------------

class TestInheritance:
    def test_single_parent(self, tmp_project):
        f = tmp_project("dog.py", """
            class Animal:
                def speak(self) -> str: ...

            class Dog(Animal):
                def speak(self) -> str:
                    return "woof"
        """)
        pm = build_project_map([f])
        assert pm.classes["Dog"].parent_classes == ["Animal"]

    def test_multiple_parents(self, tmp_project):
        f = tmp_project("multi.py", """
            class A:
                def foo(self): ...

            class B:
                def bar(self): ...

            class C(A, B):
                pass
        """)
        pm = build_project_map([f])
        parents = pm.classes["C"].parent_classes
        assert "A" in parents
        assert "B" in parents

    def test_is_override_true(self, tmp_project):
        f = tmp_project("override.py", """
            class Base:
                def run(self) -> None: ...

            class Child(Base):
                def run(self) -> None:
                    pass
        """)
        pm = build_project_map([f])
        child_methods = {m.name: m for m in pm.classes["Child"].methods}
        assert child_methods["run"].is_override is True

    def test_is_override_false_for_new_method(self, tmp_project):
        f = tmp_project("new_method.py", """
            class Base:
                def run(self) -> None: ...

            class Child(Base):
                def run(self) -> None: ...
                def new_method(self) -> None: ...
        """)
        pm = build_project_map([f])
        child_methods = {m.name: m for m in pm.classes["Child"].methods}
        assert child_methods["new_method"].is_override is False

    def test_dynamic_base_marked(self, tmp_project):
        f = tmp_project("dynamic.py", """
            def get_base(): ...

            class Foo(get_base()):
                pass
        """)
        pm = build_project_map([f])
        assert "<dynamic>" in pm.classes["Foo"].parent_classes


# ---------------------------------------------------------------------------
# 3. Методы и сигнатуры
# ---------------------------------------------------------------------------

class TestMethodSignatures:
    def test_multiple_methods(self, tmp_project):
        f = tmp_project("service.py", """
            class UserService:
                def create(self, name: str) -> int: ...
                def delete(self, user_id: int) -> None: ...
                def find(self, user_id: int) -> str: ...
        """)
        pm = build_project_map([f])
        names = [m.name for m in pm.classes["UserService"].methods]
        assert "create" in names
        assert "delete" in names
        assert "find" in names

    def test_method_parameters(self, tmp_project):
        f = tmp_project("params.py", """
            class Foo:
                def bar(self, x: int, y: str) -> bool: ...
        """)
        pm = build_project_map([f])
        method = pm.classes["Foo"].methods[0]
        assert "x: int" in method.parameters
        assert "y: str" in method.parameters

    def test_method_return_type(self, tmp_project):
        f = tmp_project("returns.py", """
            class Foo:
                def compute(self) -> int: ...
        """)
        pm = build_project_map([f])
        assert pm.classes["Foo"].methods[0].return_type == "int"

    def test_async_method_captured(self, tmp_project):
        f = tmp_project("async_class.py", """
            class AsyncRepo:
                async def fetch(self) -> list: ...
        """)
        pm = build_project_map([f])
        names = [m.name for m in pm.classes["AsyncRepo"].methods]
        assert "fetch" in names


# ---------------------------------------------------------------------------
# 4. Файл без классов
# ---------------------------------------------------------------------------

class TestFileWithoutClasses:
    def test_empty_file(self, tmp_project):
        f = tmp_project("empty.py", "")
        pm = build_project_map([f])
        assert pm.classes == {}
        assert pm.interfaces == {}

    def test_only_functions(self, tmp_project):
        f = tmp_project("utils.py", """
            def helper(x: int) -> int:
                return x * 2
        """)
        pm = build_project_map([f])
        assert pm.classes == {}

    def test_only_constants(self, tmp_project):
        f = tmp_project("const.py", """
            MAX_RETRIES = 3
            DEFAULT_TIMEOUT = 30
        """)
        pm = build_project_map([f])
        assert pm.classes == {}


# ---------------------------------------------------------------------------
# 5. ABC и Protocol → попадают в interfaces
# ---------------------------------------------------------------------------

class TestInterfaces:
    def test_abc_base(self, tmp_project):
        f = tmp_project("repo.py", """
            from abc import ABC, abstractmethod

            class IRepository(ABC):
                @abstractmethod
                def find(self, id: int): ...
        """)
        pm = build_project_map([f])
        assert "IRepository" in pm.interfaces

    def test_protocol_base(self, tmp_project):
        f = tmp_project("protocol.py", """
            from typing import Protocol

            class Serializable(Protocol):
                def serialize(self) -> str: ...
        """)
        pm = build_project_map([f])
        assert "Serializable" in pm.interfaces

    def test_interface_implementations_backlink(self, tmp_project):
        f = tmp_project("payment.py", """
            from abc import ABC

            class IPayment(ABC):
                def pay(self) -> bool: ...

            class CreditCard(IPayment):
                def pay(self) -> bool:
                    return True

            class PayPal(IPayment):
                def pay(self) -> bool:
                    return True
        """)
        pm = build_project_map([f])
        impls = pm.interfaces["IPayment"].implementations
        assert "CreditCard" in impls
        assert "PayPal" in impls

    def test_implemented_interfaces_in_class_info(self, tmp_project):
        f = tmp_project("impl.py", """
            from abc import ABC

            class IBase(ABC):
                def run(self): ...

            class Concrete(IBase):
                def run(self): pass
        """)
        pm = build_project_map([f])
        assert "IBase" in pm.classes["Concrete"].implemented_interfaces


# ---------------------------------------------------------------------------
# 6. Ошибка парсинга — файл пропускается, остальные обрабатываются
# ---------------------------------------------------------------------------

class TestParseErrorHandling:
    def test_syntax_error_skipped(self, tmp_project):
        good = tmp_project("good.py", """
            class Good:
                pass
        """)
        bad = tmp_project("bad.py", "class Broken\n    def foo(\n")

        pm = build_project_map([good, bad])
        assert "Good" in pm.classes
        assert "Broken" not in pm.classes

    def test_nonexistent_file_skipped(self, tmp_project):
        good = tmp_project("good.py", "class Good: pass")
        pm = build_project_map([good, "/nonexistent/path/fake.py"])
        assert "Good" in pm.classes

    def test_non_python_file_skipped(self, tmp_project):
        txt = tmp_project("notes.txt", "class Fake: pass")
        # Переименуем в .txt
        txt_path = Path(txt).with_suffix(".txt")
        Path(txt).rename(txt_path)
        pm = build_project_map([str(txt_path)])
        assert pm.classes == {}


# ---------------------------------------------------------------------------
# 7. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_nested_class_ignored(self, tmp_project):
        """Вложенные классы не должны попадать в top-level ProjectMap."""
        f = tmp_project("nested.py", """
            class Outer:
                class Inner:
                    pass
        """)
        pm = build_project_map([f])
        assert "Outer" in pm.classes
        assert "Inner" not in pm.classes

    def test_multiple_classes_in_one_file(self, tmp_project):
        f = tmp_project("multi_class.py", """
            class Alpha:
                pass

            class Beta:
                pass

            class Gamma:
                pass
        """)
        pm = build_project_map([f])
        assert "Alpha" in pm.classes
        assert "Beta" in pm.classes
        assert "Gamma" in pm.classes

    def test_multiple_files(self, tmp_project):
        f1 = tmp_project("a.py", "class Alpha: pass")
        f2 = tmp_project("b.py", "class Beta: pass")
        pm = build_project_map([f1, f2])
        assert "Alpha" in pm.classes
        assert "Beta" in pm.classes

    def test_class_with_decorator(self, tmp_project):
        """Декораторы не должны мешать извлечению класса."""
        f = tmp_project("dataclass.py", """
            from dataclasses import dataclass

            @dataclass
            class Point:
                x: int
                y: int
        """)
        pm = build_project_map([f])
        assert "Point" in pm.classes

    def test_dependencies_extracted(self, tmp_project):
        """Импорты файла попадают в dependencies классов из этого файла."""
        f = tmp_project("service.py", """
            import os
            from pathlib import Path

            class FileService:
                pass
        """)
        pm = build_project_map([f])
        assert "os" in pm.classes["FileService"].dependencies
        assert "pathlib" in pm.classes["FileService"].dependencies

# ---------------------------------------------------------------------------
# 8. ИНТЕГРАЦИОННЫЙ ТЕСТ (Критерий готовности Шага 2)
# ---------------------------------------------------------------------------

def test_integration_5_to_10_files_project(tmp_project):
    """
    Проверка критерия готовности: тестовый набор из 6 связанных Python-файлов.
    Проверяем, что ProjectMap корректно связывает классы, интерфейсы и переопределения
    через границы файлов.
    """
    f1 = tmp_project("interfaces.py", """
        from abc import ABC, abstractmethod
        from typing import Protocol
        
        class IRepository(ABC):
            @abstractmethod
            def save(self, data: dict) -> bool: ...
            
        class PaymentStrategy(Protocol):
            def pay(self, amount: int) -> bool: ...
    """)
    
    f2 = tmp_project("base_service.py", """
        class BaseService:
            def process(self) -> None:
                pass
            def get_name(self) -> str:
                return "base"
    """)
    
    f3 = tmp_project("repo_impl.py", """
        from interfaces import IRepository
        
        class PostgresRepo(IRepository):
            def save(self, data: dict) -> bool:
                return True
    """)
    
    f4 = tmp_project("payment_impl.py", """
        from interfaces import PaymentStrategy
        
        class StripePayment(PaymentStrategy):
            def pay(self, amount: int) -> bool:
                return True
    """)
    
    f5 = tmp_project("order_service.py", """
        from base_service import BaseService
        
        class OrderService(BaseService):
            def process(self) -> None:  # Это override!
                pass
            def calculate_total(self) -> float: # А это новый метод
                return 100.0
    """)
    
    f6 = tmp_project("main.py", """
        from order_service import OrderService
        from repo_impl import PostgresRepo
        from payment_impl import StripePayment
        
        class App:
            def __init__(self):
                self.repo = PostgresRepo()
    """)
    
    # Запускаем парсер на всём проекте (6 файлов)
    pm = build_project_map([f1, f2, f3, f4, f5, f6])
    
    # 1. Проверяем интерфейсы (из interfaces.py)
    assert "IRepository" in pm.interfaces
    assert "PaymentStrategy" in pm.interfaces
    
    # 2. Проверяем обратные связи (implementations)
    repo_impls = pm.interfaces["IRepository"].implementations
    assert "PostgresRepo" in repo_impls  # Наследник из repo_impl.py
    
    payment_impls = pm.interfaces["PaymentStrategy"].implementations
    assert "StripePayment" in payment_impls # Наследник из payment_impl.py
    
    # 3. Проверяем классическое наследование и is_override (cross-file)
    order_service = pm.classes["OrderService"]
    assert "BaseService" in order_service.parent_classes
    
    # Ищем методы OrderService
    os_methods = {m.name: m for m in order_service.methods}
    assert "process" in os_methods
    assert os_methods["process"].is_override is True      # Переопределил метод из base_service.py
    assert "calculate_total" in os_methods
    assert os_methods["calculate_total"].is_override is False # Свой собственный метод
    
    # 4. Проверяем зависимости (импорты) для App
    app_class = pm.classes["App"]
    assert "order_service" in app_class.dependencies
    assert "repo_impl" in app_class.dependencies
    assert "payment_impl" in app_class.dependencies