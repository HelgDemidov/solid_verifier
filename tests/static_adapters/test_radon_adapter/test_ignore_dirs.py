# test_ignore_dirs.py — тесты сборки CLI-команды в RadonAdapter.run().
# Стратегия: subprocess.run патчится через call_args, проверяем cmd, а не результат.
# JSON-вывод минимальный (пустой объект {}) — нам важна только команда.
import json
from unittest.mock import patch, MagicMock

_PATCH_SUBPROCESS = "solid_dashboard.adapters.radon_adapter.subprocess.run"
_PATCH_LIZARD = "solid_dashboard.adapters.radon_adapter.LIZARD_AVAILABLE"

# пустой JSON-ответ radon — не влияет на тесты игнорирования директорий
_EMPTY_OUTPUT = json.dumps({})


def _make_mock(stdout: str = _EMPTY_OUTPUT) -> MagicMock:
    # имитируем успешный subprocess.CompletedProcess
    m = MagicMock()
    m.stdout = stdout
    m.returncode = 0
    return m


def _get_cmd(mock_run: MagicMock) -> list:
    # извлекаем первый позиционный аргумент (cmd) из call_args
    return mock_run.call_args.args[0]


class TestIgnoreDirsCmdConstruction:
    """subprocess получает правильную команду в зависимости от ignore_dirs."""

    def test_no_ignore_dirs_no_flag(
        self, adapter, tmp_py_project, base_config
    ):
        # пустой ignore_dirs — флаг -i не должен появляться в команде
        mock = _make_mock()
        with patch(_PATCH_SUBPROCESS, return_value=mock), \
             patch(_PATCH_LIZARD, False):
            adapter.run(target_dir=str(tmp_py_project), context={}, config=base_config)

        cmd = _get_cmd(mock)
        assert "-i" not in cmd

    def test_single_ignore_dir_produces_flag(
        self, adapter, tmp_py_project
    ):
        # один ignore_dir — cmd содержит "-i" и его значение
        config = {"ignore_dirs": ["tests"]}
        mock = _make_mock()
        with patch(_PATCH_SUBPROCESS, return_value=mock), \
             patch(_PATCH_LIZARD, False):
            adapter.run(target_dir=str(tmp_py_project), context={}, config=config)

        cmd = _get_cmd(mock)
        assert "-i" in cmd
        assert cmd[cmd.index("-i") + 1] == "tests"

    def test_multiple_ignore_dirs_joined_with_comma(
        self, adapter, tmp_py_project
    ):
        # несколько ignore_dirs — значение должно быть соединено через запятую
        config = {"ignore_dirs": ["tests", ".venv", "migrations"]}
        mock = _make_mock()
        with patch(_PATCH_SUBPROCESS, return_value=mock), \
             patch(_PATCH_LIZARD, False):
            adapter.run(target_dir=str(tmp_py_project), context={}, config=config)

        cmd = _get_cmd(mock)
        assert "-i" in cmd
        assert cmd[cmd.index("-i") + 1] == "tests,.venv,migrations"

    def test_empty_strings_in_ignore_dirs_filtered(
        self, adapter, tmp_py_project
    ):
        # пустые строки и пробелы фильтруются, остается только "tests"
        config = {"ignore_dirs": ["tests", "", "  "]}
        mock = _make_mock()
        with patch(_PATCH_SUBPROCESS, return_value=mock), \
             patch(_PATCH_LIZARD, False):
            adapter.run(target_dir=str(tmp_py_project), context={}, config=config)

        cmd = _get_cmd(mock)
        assert "-i" in cmd
        assert cmd[cmd.index("-i") + 1] == "tests"

    def test_target_dir_present_in_cmd(
        self, adapter, tmp_py_project, base_config
    ):
        # target_dir должен всегда присутствовать в команде
        mock = _make_mock()
        with patch(_PATCH_SUBPROCESS, return_value=mock), \
             patch(_PATCH_LIZARD, False):
            adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        cmd = _get_cmd(mock)
        assert str(tmp_py_project) in cmd
