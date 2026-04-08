# test_edge_cases_extra.py — три дополнительных граничных сценария RadonAdapter.run().
#
# 1. test_lizard_analyze_receives_exclude_pattern
#    Верифицирует, что lizard.analyze вызывается с правильным exclude_pattern
#    при непустом ignore_dirs. Проверяет glob-трансляцию */{d}/*.
#
# 2. test_non_empty_context_does_not_affect_result
#    Верифицирует, что context полностью игнорируется адаптером:
#    результат при context={"x": 1} идентичен результату при context={}.
#
# 3. test_called_process_error_without_stderr
#    Верифицирует корректный fallback когда CalledProcessError.stderr is None:
#    сообщение не содержит строку "None", содержит понятный плейсхолдер.
import json
import subprocess
from unittest.mock import patch, MagicMock

from tests.static_adapters.test_radon_adapter.helpers import assert_success_schema, assert_error_schema

# точки патчинга — единственный источник истины для трёх тестов
_PATCH_SUBPROCESS = "solid_dashboard.adapters.radon_adapter.subprocess.run"
_PATCH_LIZARD_MOD = "solid_dashboard.adapters.radon_adapter.lizard"
_PATCH_LIZARD_FLAG = "solid_dashboard.adapters.radon_adapter.LIZARD_AVAILABLE"


def _make_proc_mock(stdout: str) -> MagicMock:
    # имитирует subprocess.CompletedProcess с заданным stdout и returncode=0
    m = MagicMock()
    m.stdout = stdout
    m.returncode = 0
    return m


def _make_radon_json_one_func(filepath: str = "app/module.py", lineno: int = 10) -> str:
    # минимальный валидный radon cc --json с одним function-блоком
    return json.dumps({filepath: [{
        "name": "target_func",
        "type": "function",
        "complexity": 3,
        "rank": "A",
        "lineno": lineno,
    }]})


class TestEdgeCasesExtra:
    """Граничные сценарии: lizard exclude_pattern, context passthrough, stderr=None."""

    def test_lizard_analyze_receives_exclude_pattern(
        self, adapter, tmp_py_project
    ):
        # ignore_dirs=["tests", ".venv"] -> lizard.analyze должен получить
        # exclude_pattern=["*/tests/*", "*/.venv/*"] — точная glob-трансляция
        config = {"ignore_dirs": ["tests", ".venv"]}
        filepath = "app/module.py"

        # fileinfo без функций — нам важен только сам вызов analyze
        fileinfo = MagicMock()
        fileinfo.filename = filepath
        fileinfo.function_list = []

        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [fileinfo]

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json_one_func(filepath))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            adapter.run(
                target_dir=str(tmp_py_project),
                context={},
                config=config,
            )

        # проверяем, что lizard.analyze вызван с правильным exclude_pattern
        mock_lizard.analyze.assert_called_once()
        call_kwargs = mock_lizard.analyze.call_args.kwargs
        assert "exclude_pattern" in call_kwargs, (
            "lizard.analyze должен получить exclude_pattern как keyword argument"
        )
        assert sorted(call_kwargs["exclude_pattern"]) == sorted(["*/tests/*", "*/.venv/*"]), (
            f"Ожидался glob-паттерн ['*/tests/*', '*/.venv/*'], "
            f"получен: {call_kwargs['exclude_pattern']}"
        )

    def test_non_empty_context_does_not_affect_result(
        self, adapter, tmp_py_project, make_radon_output
    ):
        # context={"x": 1, "y": [1, 2, 3]} не должен изменять результат run():
        # total_items, mean_cc и items идентичны результату при context={}
        radon_stdout = make_radon_output([{
            "filepath": "app/svc.py",
            "blocks": [
                {"name": "do_work", "type": "function", "complexity": 4,
                 "rank": "A", "lineno": 5},
            ],
        }])

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(radon_stdout)), \
             patch(_PATCH_LIZARD_MOD, None), \
             patch(_PATCH_LIZARD_FLAG, False):
            result_empty_ctx = adapter.run(
                target_dir=str(tmp_py_project),
                context={},
                config={"ignore_dirs": []},
            )

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(radon_stdout)), \
             patch(_PATCH_LIZARD_MOD, None), \
             patch(_PATCH_LIZARD_FLAG, False):
            result_nonempty_ctx = adapter.run(
                target_dir=str(tmp_py_project),
                context={"x": 1, "y": [1, 2, 3]},
                config={"ignore_dirs": []},
            )

        # оба вызова должны вернуть корректную схему
        assert_success_schema(result_empty_ctx)
        assert_success_schema(result_nonempty_ctx)

        # результаты идентичны по всем значимым полям
        assert result_nonempty_ctx["total_items"] == result_empty_ctx["total_items"]
        assert result_nonempty_ctx["mean_cc"] == result_empty_ctx["mean_cc"]
        assert result_nonempty_ctx["items"] == result_empty_ctx["items"]

    def test_called_process_error_without_stderr(
        self, adapter, base_config, tmp_py_project
    ):
        # CalledProcessError с stderr=None (дефолт subprocess) не должен
        # приводить к строке "None" в сообщении ошибки — ожидается fallback
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["radon", "cc", "--json"],
            stderr=None,  # явно передаём None — воспроизводим реальный кейс
        )
        with patch(_PATCH_SUBPROCESS, side_effect=exc):
            result = adapter.run(
                target_dir=str(tmp_py_project),
                context={},
                config=base_config,
            )

        assert_error_schema(result)
        # "None" как строка не должна появляться в сообщении
        assert "None" not in result["error"], (
            f"Сообщение ошибки не должно содержать строку 'None', "
            f"получено: {result['error']!r}"
        )
        # сообщение должно содержать читаемый fallback
        assert "(no stderr)" in result["error"], (
            f"Ожидался fallback '(no stderr)' в сообщении, "
            f"получено: {result['error']!r}"
        )
