# conftest.py — общие fixtures для пакета тестов test_pyan3_adapter.
# Вспомогательные утилиты и assert-хелперы живут в helpers.py.
import textwrap
import pytest
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from solid_dashboard.adapters.pyan3_adapter import Pyan3Adapter


@pytest.fixture
def adapter() -> Pyan3Adapter:
    # cast нужен по той же причине, что и в test_cohesion_adapter:
    # Pyan3Adapter реализует IAnalyzer (Protocol), Pylance не видит
    # конкретные методы через Protocol-линзу без явного приведения
    return cast(Pyan3Adapter, Pyan3Adapter())


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
    # временный проект: два минимальных .py-файла для интеграционных тестов
    (tmp_path / "module_a.py").write_text(
        textwrap.dedent("""
            def foo():
                bar()

            def bar():
                pass
        """),
        encoding="utf-8",
    )
    (tmp_path / "module_b.py").write_text(
        textwrap.dedent("""
            from module_a import foo

            def entry():
                foo()
        """),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def empty_tmp_dir(tmp_path: Path) -> Path:
    # пустая директория: ни одного .py-файла — для тестирования error path
    return tmp_path


@pytest.fixture
def mock_subprocess_run():
    # фабрика-фикстура: возвращает функцию, строящую mock CompletedProcess
    # с заданными returncode, stdout и stderr
    def _make(stdout: str = "", stderr: str = "", returncode: int = 0) -> MagicMock:
        mock = MagicMock()
        mock.returncode = returncode
        mock.stdout = stdout
        mock.stderr = stderr
        return mock
    return _make
