# test_mi.py — тесты метода _run_mi() адаптера RadonAdapter.
#
# Стратегия: subprocess.run патчится отдельно для mi-вызова.
# Тесты делятся на два класса:
#   - TestRunMiCmdConstruction: верифицирует сборку команды `radon mi --json`
#   - TestRunMiParsing: верифицирует парсинг и агрегацию JSON-вывода radon mi
#   - TestRunMiIsolation: верифицирует изоляцию сбоя MI от CC-результата
import json
from unittest.mock import patch, MagicMock, call

import pytest

_PATCH_SUBPROCESS = "solid_dashboard.adapters.radon_adapter.subprocess.run"
_PATCH_LIZARD = "solid_dashboard.adapters.radon_adapter.LIZARD_AVAILABLE"


def _make_proc_mock(stdout: str = "{}") -> MagicMock:
    # имитирует subprocess.CompletedProcess — объект-результат вызова
    m = MagicMock()
    m.stdout = stdout
    m.returncode = 0
    return m


def _make_mi_output(entries: list[dict]) -> str:
    # строит JSON в формате `radon mi --json`:
    # {"filepath": {"mi": float, "rank": str}, ...}
    raw = {e["filepath"]: {"mi": e["mi"], "rank": e["rank"]} for e in entries}
    return json.dumps(raw)


# ---------------------------------------------------------------------------
# Сборка команды radon mi
# ---------------------------------------------------------------------------

class TestRunMiCmdConstruction:
    """_run_mi() строит правильную команду radon mi --json."""

    def test_cmd_contains_radon_mi_json(
        self, adapter, tmp_py_project
    ):
        # базовая команда всегда содержит ["radon", "mi", "--json", target_dir]
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock()) as mock_run:
            adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ["radon", "mi", "--json"]
        assert str(tmp_py_project) in cmd

    def test_cmd_no_ignore_flag_when_empty(
        self, adapter, tmp_py_project
    ):
        # пустой ignore_dirs — флаг -i не должен появляться
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock()) as mock_run:
            adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        cmd = mock_run.call_args.args[0]
        assert "-i" not in cmd

    def test_cmd_ignore_dirs_joined_with_comma(
        self, adapter, tmp_py_project
    ):
        # несколько ignore_dirs — значение после -i через запятую
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock()) as mock_run:
            adapter._run_mi(str(tmp_py_project), ignore_dirs=["tests", ".venv"])

        cmd = mock_run.call_args.args[0]
        assert "-i" in cmd
        assert cmd[cmd.index("-i") + 1] == "tests,.venv"


# ---------------------------------------------------------------------------
# Парсинг и агрегация JSON-вывода radon mi
# ---------------------------------------------------------------------------

class TestRunMiParsing:
    """_run_mi() корректно парсит вывод radon mi и агрегирует метрики."""

    def test_returns_empty_dict_on_empty_output(
        self, adapter, tmp_py_project
    ):
        # radon mi --json вернул {} — _run_mi возвращает структуру с нулями
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock("{}")):
            result = adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        assert result["total_files"] == 0
        assert result["mean_mi"] == 0.0
        assert result["low_mi_count"] == 0
        assert result["files"] == []

    def test_parses_single_file_rank_a(
        self, adapter, tmp_py_project
    ):
        # один файл с rank A — low_mi_count == 0, mean_mi совпадает
        mi_output = _make_mi_output([
            {"filepath": "/app/main.py", "mi": 72.5, "rank": "A"}
        ])
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(mi_output)):
            result = adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        assert result["total_files"] == 1
        assert result["mean_mi"] == 72.5
        assert result["low_mi_count"] == 0
        assert len(result["files"]) == 1
        assert result["files"][0]["rank"] == "A"

    def test_low_mi_count_counts_rank_c_only(
        self, adapter, tmp_py_project
    ):
        # три файла: A, B, C — low_mi_count должен считать только rank C
        mi_output = _make_mi_output([
            {"filepath": "/app/a.py", "mi": 80.0, "rank": "A"},
            {"filepath": "/app/b.py", "mi": 15.0, "rank": "B"},
            {"filepath": "/app/c.py", "mi": 5.0,  "rank": "C"},
        ])
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(mi_output)):
            result = adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        assert result["low_mi_count"] == 1
        assert result["total_files"] == 3

    def test_files_sorted_by_mi_ascending(
        self, adapter, tmp_py_project
    ):
        # файлы должны быть отсортированы по mi ASC — худшие первыми
        mi_output = _make_mi_output([
            {"filepath": "/app/good.py",  "mi": 90.0, "rank": "A"},
            {"filepath": "/app/worse.py", "mi": 8.0,  "rank": "C"},
            {"filepath": "/app/mid.py",   "mi": 45.0, "rank": "A"},
        ])
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(mi_output)):
            result = adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        mi_values = [f["mi"] for f in result["files"]]
        assert mi_values == sorted(mi_values), "файлы должны быть отсортированы по mi ASC"
        assert result["files"][0]["filepath"] == "/app/worse.py"


# ---------------------------------------------------------------------------
# Изоляция сбоя MI от CC-результата
# ---------------------------------------------------------------------------

class TestRunMiIsolation:
    """Сбой radon mi не ломает CC-результат run()."""

    def test_mi_failure_returns_empty_dict(
        self, adapter, tmp_py_project
    ):
        # CalledProcessError при вызове radon mi — _run_mi возвращает {}
        import subprocess as _sp
        with patch(_PATCH_SUBPROCESS, side_effect=_sp.CalledProcessError(1, "radon")):
            result = adapter._run_mi(str(tmp_py_project), ignore_dirs=[])

        assert result == {}

    def test_mi_failure_does_not_break_run_cc_result(
        self, adapter, tmp_py_project, base_config
    ):
        # subprocess.run вызывается дважды: 1й раз для cc (успех),
        # 2й раз для mi (сбой FileNotFoundError) — run() должен вернуть
        # корректный CC-результат, а maintainability == {}
        import subprocess as _sp

        cc_output = json.dumps({
            str(tmp_py_project / "module_a.py"): [
                {"name": "simple_function", "type": "function",
                 "complexity": 2, "rank": "A", "lineno": 2}
            ]
        })
        cc_mock = _make_proc_mock(stdout=cc_output)

        def _side_effect(cmd, **kwargs):
            # первый вызов — radon cc (успех), второй — radon mi (сбой)
            if "cc" in cmd:
                return cc_mock
            raise FileNotFoundError("radon mi not found")

        with patch(_PATCH_SUBPROCESS, side_effect=_side_effect), \
             patch(_PATCH_LIZARD, False):
            result = adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        # CC-результат корректен
        assert "error" not in result
        assert result["total_items"] == 1
        assert result["mean_cc"] == 2.0
        # MI-сбой изолирован
        assert result["maintainability"] == {}
