# test_error_paths.py — тесты трех error-путей в RadonAdapter.run().
# Каждый тест изолирован: subprocess.run патчится, реальнюю утилиту radon не запускаем.
import subprocess
from unittest.mock import patch
import pytest

from tests.static_adapters.test_radon_adapter.helpers import assert_error_schema

# путь патча subprocess.run внутри модуля — единственная точка патчинга для всех 3 тестов
_PATCH_TARGET = "solid_dashboard.adapters.radon_adapter.subprocess.run"


class TestErrorPaths:
    """Error-пути run(): FileNotFoundError, CalledProcessError, JSONDecodeError."""

    def test_file_not_found_returns_error(self, adapter, base_config, tmp_py_project):
        # radon не установлен: subprocess.run бросает FileNotFoundError
        with patch(_PATCH_TARGET, side_effect=FileNotFoundError):
            result = adapter.run(
                target_dir=str(tmp_py_project),
                context={},
                config=base_config,
            )

        assert_error_schema(result)
        # точное сообщение ошибки фиксируем как контракт
        assert result["error"] == "Radon executable not found. Please install radon."

    def test_called_process_error_returns_error(self, adapter, base_config, tmp_py_project):
        # radon завершается с ненулевым returncode
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["radon", "cc", "--json"],
            stderr="fatal: analysis failed",
        )
        with patch(_PATCH_TARGET, side_effect=exc):
            result = adapter.run(
                target_dir=str(tmp_py_project),
                context={},
                config=base_config,
            )

        assert_error_schema(result)
        # сообщение должно содержать префикс и stderr от исключения
        assert "Radon execution failed" in result["error"]
        assert "fatal: analysis failed" in result["error"]

    def test_json_decode_error_returns_error(self, adapter, base_config, tmp_py_project):
        # radon выводит невалидный JSON (returncode=0, но stdout — мусор)
        mock_result = type("CP", (), {"stdout": "not valid json {{{\n", "returncode": 0})()
        with patch(_PATCH_TARGET, return_value=mock_result):
            result = adapter.run(
                target_dir=str(tmp_py_project),
                context={},
                config=base_config,
            )

        assert_error_schema(result)
        assert result["error"] == "Failed to parse Radon JSON output"
