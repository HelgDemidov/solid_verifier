# test_lizard_enrichment.py — тесты lizard-обогащения parameter_count в RadonAdapter.run().
#
# Стратегия изоляции: патчим три модульных переменных radon_adapter:
#   - subprocess.run   → не запускаем реальный radon
#   - lizard           → переменная модуля (объект с .analyze), контролирует вход в ветку
#   - LIZARD_AVAILABLE → булева переменная (влияет на поле lizard_used в результате)
#
# Важно: lizard-ветка входит при `lizard is not None and items`, поэтому
# для активации ветки нужны: lizard != None И items непустой.
import json
import warnings
from pathlib import Path
from unittest.mock import patch, MagicMock

from tests.static_adapters.test_radon_adapter.helpers import assert_success_schema

_PATCH_SUBPROCESS = "solid_dashboard.adapters.radon_adapter.subprocess.run"
_PATCH_LIZARD_MOD = "solid_dashboard.adapters.radon_adapter.lizard"
_PATCH_LIZARD_FLAG = "solid_dashboard.adapters.radon_adapter.LIZARD_AVAILABLE"


def _make_proc_mock(stdout: str) -> MagicMock:
    # имитирует subprocess.CompletedProcess с заданным stdout
    m = MagicMock()
    m.stdout = stdout
    m.returncode = 0
    return m


def _make_radon_json(filepath: str, lineno: int = 10) -> str:
    # фабрика: минимальный JSON с одним function-блоком
    return json.dumps({filepath: [{
        "name": "target_func",
        "type": "function",
        "complexity": 3,
        "rank": "A",
        "lineno": lineno,
    }]})


def _make_lizard_func(parameter_count: int, start_line: int) -> MagicMock:
    # имитирует lizard FunctionInfo: нужны только parameter_count и start_line
    func = MagicMock()
    func.parameter_count = parameter_count
    func.start_line = start_line
    return func


def _make_lizard_fileinfo(filename: str, funcs: list) -> MagicMock:
    # имитирует lizard FileInformation: filename и function_list
    fi = MagicMock()
    fi.filename = filename
    fi.function_list = funcs
    return fi


class TestLizardDoublePatch:
    """Проверка работоспособности двойного патча: lizard + subprocess.

    Этот тест — рекомендуемый санити-чек: убедиться, что оба патча lizard
    и subprocess работают корректно в одном with-блоке — перед написанием
    остальных 7 тестов.
    """

    def test_double_patch_both_work(
        self, adapter, tmp_py_project, base_config
    ):
        # проверяем: lizard-патч (объект) и subprocess-патч работают одновременно
        filepath = "app/module.py"
        abspath = str(Path(filepath).resolve())
        liz_func = _make_lizard_func(parameter_count=3, start_line=10)
        fileinfo = _make_lizard_fileinfo(filename=filepath, funcs=[liz_func])

        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [fileinfo]

        radon_json = _make_radon_json(filepath=filepath, lineno=10)

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(radon_json)), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            result = adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        # оба патча сработали: lizard.analyze вызван и результат корректен
        mock_lizard.analyze.assert_called_once()
        assert_success_schema(result)
        assert result["items"][0].get("parameter_count") == 3


class TestParameterCountEnrichment:
    """Покрытие hit/miss в lizard_index и варианты недоступности lizard."""

    def test_parameter_count_added_on_hit(
        self, adapter, tmp_py_project, base_config
    ):
        # lizard_index содержит (abspath, lineno) — parameter_count добавляется в item
        filepath = "app/module.py"
        liz_func = _make_lizard_func(parameter_count=5, start_line=10)
        fileinfo = _make_lizard_fileinfo(filename=filepath, funcs=[liz_func])
        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [fileinfo]

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json(filepath, lineno=10))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            result = adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        assert result["items"][0].get("parameter_count") == 5

    def test_parameter_count_not_added_on_miss(
        self, adapter, tmp_py_project, base_config
    ):
        # lineno в radon не совпадает с start_line в lizard — item без parameter_count
        filepath = "app/module.py"
        liz_func = _make_lizard_func(parameter_count=4, start_line=99)  # не 10
        fileinfo = _make_lizard_fileinfo(filename=filepath, funcs=[liz_func])
        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [fileinfo]

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json(filepath, lineno=10))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            result = adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        assert "parameter_count" not in result["items"][0]

    def test_parameter_count_not_added_when_lizard_unavailable(
        self, adapter, tmp_py_project, base_config
    ):
        # lizard=None — ветка не входит, items не обогащаются
        filepath = "app/module.py"

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json(filepath, lineno=10))), \
             patch(_PATCH_LIZARD_MOD, None), \
             patch(_PATCH_LIZARD_FLAG, False):
            result = adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        assert "parameter_count" not in result["items"][0]
        assert result["lizard_used"] is False

    def test_lizard_used_true_when_available_and_items(
        self, adapter, tmp_py_project, base_config
    ):
        # items > 0, LIZARD_AVAILABLE=True — lizard_used=True в результате
        filepath = "app/module.py"
        fileinfo = _make_lizard_fileinfo(filename=filepath, funcs=[])
        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [fileinfo]

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json(filepath))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            result = adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        assert result["lizard_used"] is True

    def test_lizard_not_called_when_no_items(
        self, adapter, tmp_py_project, base_config
    ):
        # items=[] — lizard.analyze не вызывается даже при lizard != None
        mock_lizard = MagicMock()

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(json.dumps({}))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            adapter.run(
                target_dir=str(tmp_py_project), context={}, config=base_config
            )

        mock_lizard.analyze.assert_not_called()


class TestLizardWarnings:
    """Оба RuntimeWarning и устойчивость пайплайна при сбоях."""

    def test_indexing_failure_emits_warning(
        self, adapter, tmp_py_project, base_config
    ):
        # сбой в fileinfo (например, сбой при итерации function_list) — RuntimeWarning
        filepath = "app/module.py"

        # fileinfo.файл доступен, но function_list бросает исключение при итерации
        bad_fileinfo = MagicMock()
        bad_fileinfo.filename = filepath
        bad_fileinfo.function_list = MagicMock(
            __iter__=MagicMock(side_effect=RuntimeError("iter failed"))
        )
        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [bad_fileinfo]

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json(filepath))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                adapter.run(
                    target_dir=str(tmp_py_project), context={}, config=base_config
                )

        runtime_warns = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert len(runtime_warns) >= 1
        assert any("lizard failed to index file" in str(w.message) for w in runtime_warns)

    def test_enrichment_failure_emits_warning(
        self, adapter, tmp_py_project, base_config
    ):
        # Path(filepath).resolve() бросает исключение при обогащении
        filepath = "app/module.py"
        liz_func = _make_lizard_func(parameter_count=2, start_line=10)
        fileinfo = _make_lizard_fileinfo(filename=filepath, funcs=[liz_func])
        mock_lizard = MagicMock()
        mock_lizard.analyze.return_value = [fileinfo]

        # патчим Path чтобы resolve() бросал исключение при обогащении item
        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(_make_radon_json(filepath, lineno=10))), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True), \
             patch("solid_dashboard.adapters.radon_adapter.Path",
                   side_effect=[Path(filepath), RuntimeError("path error")]):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                adapter.run(
                    target_dir=str(tmp_py_project), context={}, config=base_config
                )

        runtime_warns = [w for w in caught if issubclass(w.category, RuntimeWarning)]
        assert len(runtime_warns) >= 1
        assert any("parameter_count enrichment failed" in str(w.message) for w in runtime_warns)

    def test_indexing_failure_does_not_stop_pipeline(
        self, adapter, tmp_py_project, base_config
    ):
        # сбой индексации одного файла — остальные items обрабатываются,
        # пайплайн не останавливается и результат возвращается
        filepath_good = "app/good.py"
        filepath_bad = "app/bad.py"

        # два блока из двух файлов
        radon_json = json.dumps({
            filepath_bad: [{"name": "bad_func", "type": "function",
                            "complexity": 2, "rank": "A", "lineno": 5}],
            filepath_good: [{"name": "good_func", "type": "function",
                             "complexity": 3, "rank": "A", "lineno": 10}],
        })

        # good fileinfo нормальный, bad fileinfo бросает при итерации function_list
        good_liz_func = _make_lizard_func(parameter_count=2, start_line=10)
        good_fileinfo = _make_lizard_fileinfo(filename=filepath_good, funcs=[good_liz_func])

        bad_fileinfo = MagicMock()
        bad_fileinfo.filename = filepath_bad
        bad_fileinfo.function_list = MagicMock(
            __iter__=MagicMock(side_effect=RuntimeError("iter failed"))
        )

        mock_lizard = MagicMock()
        # bad впереди good — убеждаемся, что сбой одного не блокирует второй
        mock_lizard.analyze.return_value = [bad_fileinfo, good_fileinfo]

        with patch(_PATCH_SUBPROCESS, return_value=_make_proc_mock(radon_json)), \
             patch(_PATCH_LIZARD_MOD, mock_lizard), \
             patch(_PATCH_LIZARD_FLAG, True):
            with warnings.catch_warnings(record=True):
                warnings.simplefilter("always")
                result = adapter.run(
                    target_dir=str(tmp_py_project), context={}, config=base_config
                )

        # адаптер завершился и вернул полный результат с обоими items
        assert_success_schema(result)
        assert result["total_items"] == 2
