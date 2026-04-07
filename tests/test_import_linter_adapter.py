# ===================================================================================================
# Тесты для ImportLinterAdapter
#
# Покрывают все статические методы и ключевые пути выполнения без реального lint-imports:
# - _parse_violations         (7 тестов)  — основная логика парсинга, включая violation_details
# - _parse_contract_stats     (4 теста)   — regex + fallback
# - generate_synced_config    (4 теста)   — layer_order, multiline root_packages, тип контракта
# - _error_message            (1 тест)    — схема ответа при ошибке
# ===================================================================================================

import configparser
import os
import textwrap

import pytest

from solid_dashboard.adapters.import_linter_adapter import ImportLinterAdapter


# Общий экземпляр адаптера для всех тестов
@pytest.fixture
def adapter() -> ImportLinterAdapter:
    return ImportLinterAdapter()


# ------------------------------------------------------------------
# Блок 1: _parse_violations
# ------------------------------------------------------------------

class TestParseViolations:

    def test_empty_output_returns_empty_lists(self):
        # Пустой вывод — нет нарушений, оба списка пусты
        violations, details = ImportLinterAdapter._parse_violations("")
        assert violations == []
        assert details == []

    def test_all_kept_output_returns_empty(self):
        # Вывод с KEPT-контрактами — violations не собираются
        output = "Scopus API layered architecture KEPT\nContracts: 1 kept, 0 broken."
        violations, details = ImportLinterAdapter._parse_violations(output)
        assert violations == []
        assert details == []

    def test_single_broken_contract_with_imports(self):
        # Один нарушенный контракт с двумя broken_imports
        output = textwrap.dedent("""\
            Scopus API layered architecture BROKEN
                app.routers.search -> app.models.paper
                app.routers.auth -> app.models.user
            Contracts: 0 kept, 1 broken.
        """)
        violations, details = ImportLinterAdapter._parse_violations(output)
        assert violations == ["Scopus API layered architecture"]
        assert len(details) == 1
        assert details[0]["contract_name"] == "Scopus API layered architecture"
        assert details[0]["status"] == "BROKEN"
        assert details[0]["broken_imports"] == [
            {"importer": "app.routers.search", "imported": "app.models.paper"},
            {"importer": "app.routers.auth", "imported": "app.models.user"},
        ]

    def test_multiple_broken_contracts_are_grouped_correctly(self):
        # Несколько нарушенных контрактов — каждый получает свои broken_imports
        output = textwrap.dedent("""\
            First contract BROKEN
                app.a -> app.b
            Second contract BROKEN
                app.c -> app.d
                app.e -> app.f
        """)
        violations, details = ImportLinterAdapter._parse_violations(output)
        assert violations == ["First contract", "Second contract"]
        assert len(details) == 2
        assert details[0]["broken_imports"] == [{"importer": "app.a", "imported": "app.b"}]
        assert details[1]["broken_imports"] == [
            {"importer": "app.c", "imported": "app.d"},
            {"importer": "app.e", "imported": "app.f"},
        ]

    def test_broken_contract_without_import_lines(self):
        # Контракт помечен BROKEN, но строк с '->' нет — broken_imports пуст,
        # факт нарушения зафиксирован returncode; это корректное состояние
        output = "Some contract BROKEN\nContracts: 0 kept, 1 broken."
        violations, details = ImportLinterAdapter._parse_violations(output)
        assert violations == ["Some contract"]
        assert details[0]["broken_imports"] == []

    def test_broken_import_line_not_attributed_to_wrong_contract(self):
        # Строки с '->' после KEPT-контракта не должны попасть ни в один detail
        output = textwrap.dedent("""\
            Clean contract KEPT
                app.x -> app.y
        """)
        _, details = ImportLinterAdapter._parse_violations(output)
        assert details == []

    def test_violations_and_details_are_parallel(self):
        # violations и violation_details всегда синхронны по индексу
        output = textwrap.dedent("""\
            Contract Alpha BROKEN
                app.a -> app.b
            Contract Beta BROKEN
                app.c -> app.d
        """)
        violations, details = ImportLinterAdapter._parse_violations(output)
        assert len(violations) == len(details)
        for i, name in enumerate(violations):
            assert details[i]["contract_name"] == name


# ------------------------------------------------------------------
# Блок 2: _parse_contract_stats
# ------------------------------------------------------------------

class TestParseContractStats:

    def test_standard_output_format(self):
        # Стандартная строка «Contracts: 1 kept, 0 broken.»
        output = "Contracts: 1 kept, 0 broken."
        kept, broken = ImportLinterAdapter._parse_contract_stats(output, linting_passed=True)
        assert kept == 1
        assert broken == 0

    def test_broken_contracts_parsed(self):
        # Есть нарушения
        output = "Contracts: 0 kept, 2 broken."
        kept, broken = ImportLinterAdapter._parse_contract_stats(output, linting_passed=False)
        assert kept == 0
        assert broken == 2

    def test_fallback_on_no_match_passed(self):
        # Формат вывода не распознан, returncode=0 — fallback: kept=1
        kept, broken = ImportLinterAdapter._parse_contract_stats("unexpected output", linting_passed=True)
        assert kept == 1
        assert broken == 0

    def test_fallback_on_no_match_failed(self):
        # Формат вывода не распознан, returncode=1 — fallback: broken=1
        kept, broken = ImportLinterAdapter._parse_contract_stats("unexpected output", linting_passed=False)
        assert kept == 0
        assert broken == 1


# ------------------------------------------------------------------
# Блок 3: generate_synced_config
# ------------------------------------------------------------------

class TestGenerateSyncedConfig:

    # Минимальный базовый .importlinter для тестов
    _BASE_INI = textwrap.dedent("""\
        [importlinter]
        root_packages = old_package

        [importlinter:contract:layers]
        name = Test contract
        type = layers
        layers =
            old_layer_a
            old_layer_b
        containers = old_package
    """)

    @pytest.fixture
    def base_config_file(self, tmp_path) -> str:
        # Записываем базовый .importlinter во временную директорию
        path = tmp_path / ".importlinter"
        path.write_text(self._BASE_INI, encoding="utf-8")
        return str(path)

    @pytest.fixture
    def out_path(self, tmp_path) -> str:
        return str(tmp_path / ".importlinter_auto_app")

    def test_root_packages_updated_to_package_name(self, adapter, base_config_file, out_path):
        # root_packages должен содержать package_name, а не старое значение
        adapter.generate_synced_config(
            base_config_path=base_config_file,
            solid_config={"layer_order": ["routers", "services"]},
            outpath=out_path,
            package_name="app",
        )
        cfg = configparser.RawConfigParser()
        cfg.read(out_path, encoding="utf-8")
        raw = cfg.get("importlinter", "root_packages")
        # multiline INI-значение содержит имя пакета
        assert "app" in raw
        assert "old_package" not in raw

    def test_layer_order_respected_in_output(self, adapter, base_config_file, out_path):
        # Порядок слоёв в сгенерированном конфиге строго совпадает с layer_order
        order = ["routers", "services", "infrastructure", "models"]
        adapter.generate_synced_config(
            base_config_path=base_config_file,
            solid_config={"layer_order": order},
            outpath=out_path,
            package_name="app",
        )
        cfg = configparser.RawConfigParser()
        cfg.optionxform = str  # type: ignore[assignment]
        cfg.read(out_path, encoding="utf-8")
        layers_raw = cfg.get("importlinter:contract:layers", "layers")
        # Извлекаем непустые строки в том же порядке
        parsed_layers = [ln.strip() for ln in layers_raw.splitlines() if ln.strip()]
        assert parsed_layers == order

    def test_non_layers_contract_type_not_modified(self, tmp_path, adapter, out_path):
        # Контракты типа forbidden/independence не должны затрагиваться
        ini = textwrap.dedent("""\
            [importlinter]
            root_packages = app

            [importlinter:contract:forbidden]
            name = No direct DB access
            type = forbidden
            source_modules = app.routers
            forbidden_modules = app.models
        """)
        base_path = tmp_path / ".importlinter"
        base_path.write_text(ini, encoding="utf-8")

        adapter.generate_synced_config(
            base_config_path=str(base_path),
            solid_config={"layer_order": ["routers", "models"]},
            outpath=out_path,
            package_name="app",
        )
        cfg = configparser.RawConfigParser()
        cfg.read(out_path, encoding="utf-8")
        # forbidden-контракт не должен содержать поле layers
        assert not cfg.has_option("importlinter:contract:forbidden", "layers")

    def test_unmatched_ignore_imports_set_to_warn(self, adapter, base_config_file, out_path):
        # unmatched_ignore_imports должно быть установлено в warn
        adapter.generate_synced_config(
            base_config_path=base_config_file,
            solid_config={"layer_order": ["routers"]},
            outpath=out_path,
            package_name="app",
        )
        cfg = configparser.RawConfigParser()
        cfg.read(out_path, encoding="utf-8")
        assert cfg.get("importlinter", "unmatched_ignore_imports") == "warn"


# ------------------------------------------------------------------
# Блок 4: _error_message
# ------------------------------------------------------------------

class TestErrorMessage:

    def test_error_message_schema_complete(self):
        # Схема ошибки содержит все 7 обязательных полей с корректными типами
        result = ImportLinterAdapter._error_message("test error")
        assert result["is_success"] is False
        assert result["error"] == "test error"
        assert result["contracts_checked"] == 0
        assert result["broken_contracts"] == 0
        assert result["kept_contracts"] == 0
        assert result["violations"] == []
        assert result["violation_details"] == []
        assert result["raw_output"] == ""
        # Итого 8 полей — violations + violation_details оба присутствуют
        assert len(result) == 8