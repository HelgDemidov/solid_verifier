# conftest.py — общие fixtures для пакета тестов test_radon_adapter.
# Вспомогательные assert-хелперы живут в helpers.py.
import json
import textwrap
import pytest
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from solid_dashboard.adapters.radon_adapter import RadonAdapter


@pytest.fixture
def adapter() -> RadonAdapter:
    # cast нужен по той же причине, что и в других тестовых пакетах:
    # RadonAdapter реализует IAnalyzer (Protocol), Pylance не видит
    # конкретные методы через Protocol-линзу без явного приведения
    return cast(RadonAdapter, RadonAdapter())


@pytest.fixture
def base_config() -> dict:
    # минимальный конфиг: игнорируемые директории не заданы
    return {"ignore_dirs": []}


@pytest.fixture
def config_with_ignore() -> dict:
    # конфиг с типовым набором игнорируемых директорий
    return {"ignore_dirs": ["tests", ".venv", "__pycache__"]}


@pytest.fixture
def tmp_py_project(tmp_path: Path) -> Path:
    # минимальный временный Python-проект: один .py-файл с функцией,
    # репрезентативен как валидный target_dir для run()
    (tmp_path / "module_a.py").write_text(
        textwrap.dedent("""
            def simple_function(x, y):
                if x > y:
                    return x
                return y
        """),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def make_radon_output():
    """Фабрика-фикстура: строит JSON-строку в формате radon cc --json.

    Принимает список dict-записей вида:
        [
            {
                "filepath": "path/to/file.py",
                "blocks": [
                    {
                        "name": "my_func",
                        "type": "function",
                        "complexity": 5,
                        "rank": "A",
                        "lineno": 10,
                    },
                    ...
                ]
            },
            ...
        ]

    Возвращает строку — валидный JSON в формате {filepath: [blocks...]}.
    Также принимает специальное значение blocks="SyntaxError: ..." (строка)
    для имитации radon-ответа на синтаксически невалидный файл.
    """
    def _make(entries: list[dict]) -> str:
        # собираем {filepath: blocks} — точный формат radon cc --json
        raw: dict = {}
        for entry in entries:
            raw[entry["filepath"]] = entry["blocks"]
        return json.dumps(raw)
    return _make


@pytest.fixture
def mock_subprocess_run():
    """Фабрика-фикстура: строит mock CompletedProcess с заданными полями.

    Используется для изоляции subprocess.run во всех тестовых файлах пакета.
    """
    def _make(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
        # имитируем subprocess.CompletedProcess без реального вызова
        mock = MagicMock()
        mock.returncode = returncode
        mock.stdout = stdout
        mock.stderr = stderr
        return mock
    return _make
