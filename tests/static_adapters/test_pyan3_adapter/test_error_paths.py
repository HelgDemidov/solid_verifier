# test_error_paths.py — юнит-тесты для всех _error()-путей метода run().
#
# Ответственность: проверить, что каждый из 4 путей, завершающихся вызовом _error(),
# возвращает результат, соответствующий полному schema-контракту _error()-ответа.
#
# Покрытые пути:
#   1. no_py_files          — target_dir без .py-файлов → _error("No python files found...")
#   2. pyan3_not_found      — subprocess.run бросает FileNotFoundError → _error("pyan3 executable not found...")
#   3. nonzero_returncode   — pyan3 завершается с returncode=1 → _error("pyan3 failed with exit code 1...")
#   4. abort_on_high_collision — collision_rate > threshold, abort=True → _error("Aborted: collision_rate...")
#
# Что НЕ тестируется здесь:
#   - warning-пути (sanity check, collision_rate без abort) — это test_parser.py и test_confidence.py
#   - логика парсинга, confidence, дедупликации — остальные тест-файлы пакета
import os
import pytest
from unittest.mock import patch, MagicMock

from tests.static_adapters.test_pyan3_adapter.helpers import (
    assert_error_schema,
    make_raw_output,
)


class TestErrorPaths:

    # -----------------------------------------------------------------------
    # Кейс 1: нет .py-файлов в target_dir
    # -----------------------------------------------------------------------

    def test_no_py_files_returns_error(self, adapter, tmp_path, base_config):
        # tmp_path без __init__.py → сначала RuntimeWarning о пакете,
        # затем _error() из-за отсутствия .py-файлов
        with pytest.warns(RuntimeWarning, match="has no __init__.py"):
            result = adapter.run(str(tmp_path), {}, base_config)
        assert_error_schema(result)
        assert "No python files found" in result["error"]

    # -----------------------------------------------------------------------
    # Кейс 2: pyan3 не установлен — FileNotFoundError из subprocess.run
    # -----------------------------------------------------------------------

    def test_pyan3_not_found_returns_error(self, adapter, tmp_py_project, base_config):
        # tmp_py_project содержит .py-файлы, поэтому subprocess.run будет вызван;
        # патчим его чтобы симулировать отсутствие pyan3 в PATH
        with patch(
            "solid_dashboard.adapters.pyan3_adapter.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            result = adapter.run(str(tmp_py_project), {}, base_config)
        assert_error_schema(result)
        assert "pyan3 executable not found" in result["error"]

    # -----------------------------------------------------------------------
    # Кейс 3: pyan3 завершается с ненулевым кодом возврата
    # -----------------------------------------------------------------------

    def test_nonzero_returncode_returns_error(self, adapter, tmp_py_project, base_config):
        # returncode=1 и непустой stderr — адаптер передает stderr в сообщение об ошибке
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "SyntaxError: invalid syntax"
        with patch(
            "solid_dashboard.adapters.pyan3_adapter.subprocess.run",
            return_value=mock_result,
        ):
            result = adapter.run(str(tmp_py_project), {}, base_config)
        assert_error_schema(result)
        assert "pyan3 failed with exit code 1" in result["error"]
        assert "SyntaxError" in result["error"]

    # -----------------------------------------------------------------------
    # Кейс 4: abort_on_high_collision — collision_rate превышает порог, abort включен
    # -----------------------------------------------------------------------

    def test_abort_on_high_collision_returns_error(self, adapter, tmp_py_project, base_config):
        # Строим raw_output с высоким collision_rate:
        # блок "login" получает дублирующее [U]-имя "handler" → suspicious_blocks = {"login"}
        # При 2 узлах (login + handler) collision_rate = 0.5 > threshold=0.35
        raw = make_raw_output(
            edges=[("login", "handler")],
            extra_used={"login": ["handler"]},  # второй [U] handler создает коллизию
        )
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = raw
        mock_result.stderr = ""

        # Включаем abort_on_high_collision в конфиге; threshold оставляем дефолтным (0.35)
        abort_config = {
            **base_config,
            "pyan3": {
                "abort_on_high_collision": True,
                "collision_rate_threshold": 0.35,
            },
        }
        with patch(
            "solid_dashboard.adapters.pyan3_adapter.subprocess.run",
            return_value=mock_result,
        ):
            # Предупреждение о высоком collision_rate ожидается до вызова _error() —
            # перехватываем его чтобы тест не «протек» в stderr
            with pytest.warns(RuntimeWarning, match="high collision rate"):
                result = adapter.run(str(tmp_py_project), {}, abort_config)

        assert_error_schema(result)
        assert "Aborted" in result["error"]
        assert "collision_rate" in result["error"]
