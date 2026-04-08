# ==============================================================================
# Блок A: unit-тесты для CohesionAdapter._is_empty_method
#
# Проверяем, что метод корректно определяет тривиальные тела:
#   pass / ... / raise NotImplementedError / docstring -> True
#   любая реальная логика -> False
# ==============================================================================

import ast
import textwrap
import pytest


def _parse_func(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """Вспомогательная функция: парсим исходник, возвращаем первый FunctionDef или AsyncFunctionDef."""
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    raise ValueError("No FunctionDef found")


class TestIsEmptyMethod:
    # тело из одного pass — тривиальный метод-заглушка
    def test_pass_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                pass
        """)
        assert adapter._is_empty_method(func) is True

    # тело из одного Ellipsis — типичная заглушка в Protocol/ABC
    def test_ellipsis_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                ...
        """)
        assert adapter._is_empty_method(func) is True

    # raise NotImplementedError() со скобками
    def test_raise_nie_call_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                raise NotImplementedError()
        """)
        assert adapter._is_empty_method(func) is True

    # raise NotImplementedError без скобок
    def test_raise_nie_name_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                raise NotImplementedError
        """)
        assert adapter._is_empty_method(func) is True

    # raise NotImplementedError с сообщением — тоже тривиальный
    def test_raise_nie_with_message_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                raise NotImplementedError("not implemented")
        """)
        assert adapter._is_empty_method(func) is True

    # только строка-докстринг — тривиальный
    def test_docstring_only_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                \"\"\"Документация.\"\"\"
        """)
        assert adapter._is_empty_method(func) is True

    # докстринг + pass — тоже тривиальный (комбинация)
    def test_docstring_and_pass_is_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                \"\"\"Документация.\"\"\"
                pass
        """)
        assert adapter._is_empty_method(func) is True

    # присваивание — реальная логика, не тривиальный
    def test_assignment_not_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                self.x = 1
        """)
        assert adapter._is_empty_method(func) is False

    # вызов функции — реальная логика
    def test_call_not_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                self.bar()
        """)
        assert adapter._is_empty_method(func) is False

    # raise другого исключения — реальная логика (не NIE)
    def test_raise_other_not_empty(self, adapter):
        func = _parse_func("""
            def foo(self):
                raise ValueError("bad input")
        """)
        assert adapter._is_empty_method(func) is False

    # async def с pass — тоже тривиальный
    def test_async_pass_is_empty(self, adapter):
        func = _parse_func("""
            async def foo(self):
                pass
        """)
        assert adapter._is_empty_method(func) is True
