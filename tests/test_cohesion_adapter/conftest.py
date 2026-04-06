# conftest.py — общие fixtures и реэкспорт вспомогательных типов
# для пакета тестов test_cohesion_adapter
import ast
import textwrap
import pytest
from pathlib import Path
from typing import cast

from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter, ClassInfo

from solid_dashboard.adapters.class_classifier import classify_class

# ClassInfo и classify_class реэкспортируются отсюда как единственная точка импорта —
# тестовые файлы делают `from .conftest import ClassInfo` без дублирования type: ignore
__all__ = ["ClassInfo", "classify_class"]


# парсим фрагмент кода и возвращаем первый ClassDef — удобно для unit-тестов
@pytest.fixture
def parse_class():
    def _inner(source: str) -> ast.ClassDef:
        tree = ast.parse(textwrap.dedent(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                return node
        raise ValueError("No ClassDef found in source")
    return _inner


# создает временную директорию с Python-файлами по словарю {filename: source}
@pytest.fixture
def tmp_code_dir(tmp_path: Path):
    def _inner(files: dict) -> Path:
        for name, src in files.items():
            (tmp_path / name).write_text(textwrap.dedent(src), encoding="utf-8")
        return tmp_path
    return _inner


@pytest.fixture
def adapter() -> CohesionAdapter:
    # cast нужен: CohesionAdapter реализует IAnalyzer (Protocol),
    # Pylance не видит приватные методы через Protocol-линзу без явного приведения
    return cast(CohesionAdapter, CohesionAdapter())
