# conftest.py — общие fixture для тестов cohesion_adapter
import ast
import textwrap

import pytest

from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter


@pytest.fixture
def parse_class():
    """Парсит Python-код и возвращает первый ClassDef в AST."""
    def _inner(source: str):
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                return node
        raise AssertionError("ClassDef не найден в AST")
    return _inner


@pytest.fixture
def adapter():
    """Экземпляр CohesionAdapter — используется в интеграционных тестах."""
    return CohesionAdapter()
