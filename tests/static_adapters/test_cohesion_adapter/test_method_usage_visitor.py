# ==============================================================================
# Блок D: unit-тесты для _MethodUsageVisitor
#
# Проверяем:
#   D1. Обнаружение self.attr в used_attributes
#   D2. Обнаружение self.method() в called_methods
#   D3. Атрибуты/методы не из класса не регистрируются
#   D4. @staticmethod: первый параметр не трактуется как self
#   D5. Регрессия 4fa8cd7: super().method() регистрируется в called_methods
#   D6. Регрессия 4fa8cd7: super(ClassName, self).method() регистрируется
#   D7. super().unknown_method() не регистрируется (чужой метод предка)
#   D8. Вложенный def не загрязняет граф (защита от замыканий)
#   D9. cls.method() регистрируется через @classmethod
#   D10. Прямой вызов method() без self регистрируется в called_methods
# ==============================================================================

import ast
import textwrap
from typing import cast

import pytest

# _MethodUsageVisitor — приватный класс модуля, импортируем напрямую
# (не часть публичного API адаптера, но необходим для регрессионных тестов)
from solid_dashboard.adapters.cohesion_adapter import _MethodUsageVisitor  # type: ignore[reportPrivateImportUsage]


def _make_visitor(
    source: str,
    class_attributes: set[str],
    method_names: set[str],
    is_static: bool = False,
) -> _MethodUsageVisitor:
    """
    Вспомогательная функция: парсит исходник, находит первый FunctionDef/AsyncFunctionDef,
    запускает Visitor и возвращает его для проверки результатов.
    """
    tree = ast.parse(textwrap.dedent(source))
    func_node = next(
        cast(
            ast.FunctionDef | ast.AsyncFunctionDef,
            node,
        )
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    )
    visitor = _MethodUsageVisitor(
    class_attributes=class_attributes,
    method_names=method_names,
    is_static=is_static,
)
    visitor.visit(func_node)
    return visitor


class TestMethodUsageVisitor:

    # --- D1: self.attr регистрируется в used_attributes ---

    def test_self_attribute_registered(self):
        v = _make_visitor(
            """
            def process(self):
                return self.data
            """,
            class_attributes={"data"},
            method_names={"process"},
        )
        assert "data" in v.used_attributes

    # --- D2: self.method() регистрируется в called_methods ---

    def test_self_method_call_registered(self):
        v = _make_visitor(
            """
            def run(self):
                self.validate()
            """,
            class_attributes=set(),
            method_names={"run", "validate"},
        )
        assert "validate" in v.called_methods
        assert "run" not in v.called_methods  # себя не регистрируем

    # --- D3: атрибуты/методы не из класса не попадают в наборы ---

    def test_unknown_attr_not_registered(self):
        # self.external_attr не в class_attributes — игнорируется
        v = _make_visitor(
            """
            def process(self):
                return self.external_attr
            """,
            class_attributes={"data"},  # external_attr отсутствует
            method_names={"process"},
        )
        assert v.used_attributes == set()

    def test_unknown_method_call_not_registered(self):
        # self.external_call() не в method_names — игнорируется
        v = _make_visitor(
            """
            def run(self):
                self.external_call()
            """,
            class_attributes=set(),
            method_names={"run"},  # external_call отсутствует
        )
        assert v.called_methods == set()

    # --- D4: @staticmethod — первый параметр не принимается за self ---

    def test_staticmethod_first_arg_not_self(self):
        # helper — первый аргумент @staticmethod не является self;
        # helper.data не должно регистрироваться в used_attributes
        v = _make_visitor(
            """
            def compute(helper):
                return helper.data
            """,
            class_attributes={"data"},
            method_names={"compute"},
            is_static=True,  # симулируем @staticmethod
        )
        assert v.used_attributes == set()

    # --- D5 (регрессия 4fa8cd7): super().method() должно регистрироваться ---
    #
    # До фикса 4fa8cd7 super().method() игнорировалось:
    # visit_Call проверял только self.method() / cls.method(),
    # но не super().method() — а super() является ast.Call, не ast.Name.
    # Данный тест закрепляет фикс.

    def test_super_call_no_args_registered(self):
        v = _make_visitor(
            """
            def save(self):
                super().save()
            """,
            class_attributes=set(),
            method_names={"save"},
        )
        assert "save" in v.called_methods

    # --- D6 (регрессия 4fa8cd7): super(ClassName, self).method() должно регистрироваться ---

    def test_super_call_with_args_registered(self):
        v = _make_visitor(
            """
            def save(self):
                super(MyClass, self).save()
            """,
            class_attributes=set(),
            method_names={"save"},
        )
        assert "save" in v.called_methods

    # --- D7: super().unknown_method() не регистрируется (чужой метод предка) ---

    def test_super_unknown_method_not_registered(self):
        # ancestor_only() не объявлен в текущем классе — не в method_names
        v = _make_visitor(
            """
            def run(self):
                super().ancestor_only()
            """,
            class_attributes=set(),
            method_names={"run"},  # ancestor_only отсутствует
        )
        assert v.called_methods == set()

    # --- D8: вложенный def не загрязняет граф (защита от замыканий) ---
    #
    # Если self внутренней функции бы обрабатывался, visitor зарегистрировал бы
    # self.data внутренней функции как атрибут внешнего метода — ложный результат.
    # Через пропуск вложенных FunctionDef такой спуриоус исключается.

    def test_nested_def_does_not_pollute_graph(self):
        v = _make_visitor(
            """
            def outer(self):
                def inner(self):
                    self.data   # self внутренней функции — не self класса
                    self.helper()
            """,
            class_attributes={"data"},
            method_names={"outer", "helper"},
        )
        # outer не использует атрибуты и не вызывает методы напрямую—
        # всё это внутри inner, который должен быть игнорирован
        assert v.used_attributes == set()
        assert v.called_methods == set()

    # --- D9: cls.method() через @classmethod ---

    def test_classmethod_cls_call_registered(self):
        # первый параметр @classmethod называется cls, а не self —
        # но visitor отслеживает is_static=False и регистрирует первый аргумент
        v = _make_visitor(
            """
            def create(cls):
                cls.validate()
            """,
            class_attributes=set(),
            method_names={"create", "validate"},
            is_static=False,
        )
        assert "validate" in v.called_methods

    # --- D10: прямой вызов method() без self ---

    def test_bare_method_call_registered(self):
        v = _make_visitor(
            """
            def run(self):
                helper()
            """,
            class_attributes=set(),
            method_names={"run", "helper"},
        )
        assert "helper" in v.called_methods
