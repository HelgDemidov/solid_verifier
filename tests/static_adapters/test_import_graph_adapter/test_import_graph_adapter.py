# ===================================================================================================
# Тесты для ImportGraphAdapter (коммит 7)
#
# Покрывают все публичные методы адаптера без mock-ов grimp:
# - _resolve_tier_map     (5 тестов)
# - _get_interface_layer_names (3 теста)
# - _detect_sdp_violations    (4 теста)
# - _detect_skip_layer_violations (10 тестов)
# ===================================================================================================

import pytest
from solid_dashboard.adapters.import_graph_adapter import ImportGraphAdapter


# Общий экземпляр адаптера для всех тестов
@pytest.fixture
def adapter() -> ImportGraphAdapter:
    return ImportGraphAdapter()


# ------------------------------------------------------------------
# Блок 1: _resolve_tier_map
# ------------------------------------------------------------------

class TestResolveTierMap:

    def test_format_a_flat_list(self, adapter):
        # Формат A: плоский список строк — каждая получает свой tier
        config = {"layer_order": ["routers", "services", "infrastructure", "interfaces", "models"]}
        result = adapter._resolve_tier_map(config)
        assert result == {
            "routers": 0,
            "services": 1,
            "infrastructure": 2,
            "interfaces": 3,
            "models": 4,
        }

    def test_format_b_nested_groups(self, adapter):
        # Формат B: внешний индекс = tier, внутренние элементы делят один тир
        config = {
            "layer_order": [
                ["routers"],
                ["services", "infrastructure"],
                ["interfaces"],
                ["models"],
            ]
        }
        result = adapter._resolve_tier_map(config)
        assert result == {
            "routers": 0,
            "services": 1,
            "infrastructure": 1,  # тот же тир, что и services
            "interfaces": 2,
            "models": 3,
        }

    def test_external_layers_get_max_tier_plus_one(self, adapter):
        # external_layers автоматически получают max_tier + 1
        config = {
            "layer_order": ["routers", "services", "models"],
            "external_layers": {"db_libs": ["sqlalchemy"], "web_libs": ["fastapi"]},
        }
        result = adapter._resolve_tier_map(config)
        assert result["db_libs"] == 3  # max_tier=2, external = 3
        assert result["web_libs"] == 3

    def test_no_layer_order_returns_none(self, adapter):
        # Файл-сайлент: отсутствие layer_order — None
        assert adapter._resolve_tier_map({}) is None
        assert adapter._resolve_tier_map({"layer_order": []}) is None

    def test_utility_layers_not_in_tier_map(self, adapter):
        # utility_layers намеренно не попадают в tier_map
        config = {
            "layer_order": ["routers", "services", "models"],
            "utility_layers": {"core": ["app.core"], "schemas": ["app.schemas"]},
        }
        result = adapter._resolve_tier_map(config)
        assert "core" not in result
        assert "schemas" not in result
        assert "routers" in result


# ------------------------------------------------------------------
# Блок 2: _get_interface_layer_names
# ------------------------------------------------------------------

class TestGetInterfaceLayerNames:

    def test_returns_interface_names(self, adapter):
        # Базовый кейс: поле interface_layers содержит имена слоев
        config = {"interface_layers": ["interfaces", "ports"]}
        result = adapter._get_interface_layer_names(config)
        assert result == ["interfaces", "ports"]

    def test_empty_list_returns_empty(self, adapter):
        # Пустой список — возвращает пустой
        assert adapter._get_interface_layer_names({"interface_layers": []}) == []

    def test_missing_field_returns_empty(self, adapter):
        # Отсутствует поле — fail-silent, возвращает пустой
        assert adapter._get_interface_layer_names({}) == []


# ------------------------------------------------------------------
# Блок 3: _detect_sdp_violations
# ------------------------------------------------------------------

class TestDetectSdpViolations:

    # Общий tier_map для блока
    _TIER_MAP = {"routers": 0, "services": 1, "infrastructure": 2, "models": 3}

    def test_violation_when_source_more_unstable(self, adapter):
        # routers (I=1.0) → services (I=0.8): 1.0 > 0.8 + 0.0 → violation
        result = adapter._detect_sdp_violations(
            layer_edges={("routers", "services")},
            instability_map={"routers": 1.0, "services": 0.8},
            tier_map=self._TIER_MAP,
            tolerance=0.0,
            exceptions=[],
        )
        assert len(result) == 1
        v = result[0]
        assert v["rule"] == "SDP-001"
        assert v["layer"] == "routers"
        assert v["dependency"] == "services"
        assert v["severity"] == "error"
        # Проверяем что evidence = [] (не None), баг коммита 6.1
        assert v["evidence"] == []

    def test_no_violation_when_source_less_unstable(self, adapter):
        # services (I=0.5) → models (I=0.2): 0.5 <= 0.2 + 0.0? NO, тест на отсутствие нарушения
        # Исправленный кейс: равные значения (I=0.5, I=0.5)
        result = adapter._detect_sdp_violations(
            layer_edges={("services", "models")},
            instability_map={"services": 0.5, "models": 0.5},
            tier_map=self._TIER_MAP,
            tolerance=0.0,
            exceptions=[],
        )
        assert len(result) == 0

    def test_tolerance_absorbs_minor_violation(self, adapter):
        # services (I=0.8) → models (I=0.75): margin=0.05, tolerance=0.1 → нет нарушения
        result = adapter._detect_sdp_violations(
            layer_edges={("services", "models")},
            instability_map={"services": 0.8, "models": 0.75},
            tier_map=self._TIER_MAP,
            tolerance=0.1,
            exceptions=[],
        )
        assert len(result) == 0

    def test_exception_skips_violation(self, adapter):
        # models → db_libs — в allowed_dependency_exceptions, должно пропускаться
        tier_map = {**self._TIER_MAP, "db_libs": 4}
        result = adapter._detect_sdp_violations(
            layer_edges={("models", "db_libs")},
            instability_map={"models": 0.2, "db_libs": 0.0},
            tier_map=tier_map,
            tolerance=0.0,
            exceptions=[{"source": "models", "target": "db_libs"}],
        )
        assert len(result) == 0


# ------------------------------------------------------------------
# Блок 4: _detect_skip_layer_violations
# ------------------------------------------------------------------

class TestDetectSkipLayerViolations:
    """
    Полное покрытие severity-логики коммита 6.1.

    tier_map-якорь: routers=0, services=1, infrastructure=2, interfaces=3, models=4, db_libs=5
    interface_set: {"interfaces"}
    """

    # Общий tier_map для блока
    _TIER_MAP = {
        "routers": 0, "services": 1, "infrastructure": 2,
        "interfaces": 3, "models": 4, "db_libs": 5,
    }
    _INTERFACES = ["interfaces"]

    def test_target_in_interface_set_is_warning(self, adapter):
        # services (tier=1) → interfaces (tier=3): target в interface_set → warning
        # Фактический кейс из лога проекта
        result = adapter._detect_skip_layer_violations(
            layer_edges={("services", "interfaces")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert len(result) == 1
        v = result[0]
        assert v["severity"] == "warning"
        assert v["rule"] == "SLP-001"
        assert v["skip_distance"] == 1
        assert v["evidence"] == [2]  # пропущен tier=2

    def test_target_in_interface_large_skip_is_still_warning(self, adapter):
        # routers (tier=0) → interfaces (tier=3): skip_distance=2, target в interface_set → warning
        # Решение по итогам обсуждения: зависимость от абстракции не является error
        result = adapter._detect_skip_layer_violations(
            layer_edges={("routers", "interfaces")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert len(result) == 1
        assert result[0]["severity"] == "warning"
        assert result[0]["skip_distance"] == 2

    def test_skipped_interface_layer_is_error(self, adapter):
        # infrastructure (tier=2) → models (tier=4): пропущен interfaces (tier=3) → error
        # Фактический кейс из лога проекта
        result = adapter._detect_skip_layer_violations(
            layer_edges={("infrastructure", "models")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert len(result) == 1
        v = result[0]
        assert v["severity"] == "error"
        assert v["skip_distance"] == 1
        assert 3 in v["evidence"]  # tier=3 (interfaces) в evidence

    def test_skip_ge_2_without_interface_is_error(self, adapter):
        # services (tier=1) → models (tier=4): skip=2, в пропущенных [infrastructure, interfaces]
        # interfaces в пропущенных → error (проверка по ветви 2)
        result = adapter._detect_skip_layer_violations(
            layer_edges={("services", "models")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert result[0]["severity"] == "error"

    def test_skip_ge_2_no_interface_in_skipped(self, adapter):
        # A (tier=0) → D (tier=3), пропущены B, C (tier=1, 2) — ни один не interface
        tier_map = {"A": 0, "B": 1, "C": 2, "D": 3}
        result = adapter._detect_skip_layer_violations(
            layer_edges={("A", "D")},
            tier_map=tier_map,
            interface_layer_names=[],  # нет interface-слоев
        )
        assert len(result) == 1
        assert result[0]["severity"] == "error"  # skip_distance=2, нет interface → error

    def test_skip_1_no_interface_is_warning(self, adapter):
        # routers (tier=0) → infrastructure (tier=2): skip=1, skipped=[services], нет interface → warning
        # Фактический кейс из лога проекта
        result = adapter._detect_skip_layer_violations(
            layer_edges={("routers", "infrastructure")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert len(result) == 1
        assert result[0]["severity"] == "warning"
        assert result[0]["evidence"] == [1]  # пропущен tier=1

    def test_adjacent_tiers_no_violation(self, adapter):
        # routers (tier=0) → services (tier=1): skip_distance=0 → нет нарушения
        result = adapter._detect_skip_layer_violations(
            layer_edges={("routers", "services")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert len(result) == 0

    def test_no_tier_map_returns_empty(self, adapter):
        # tier_map = None → fail-silent — нет нарушений
        result = adapter._detect_skip_layer_violations(
            layer_edges={("routers", "models")},
            tier_map=None,
            interface_layer_names=self._INTERFACES,
        )
        assert result == []

    def test_unknown_layer_skipped_silently(self, adapter):
        # Слой вне tier_map — fail-silent (например, utility_layer)
        result = adapter._detect_skip_layer_violations(
            layer_edges={("core", "models")},  # core вне tier_map
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert result == []

    def test_evidence_contains_skipped_tier_indices(self, adapter):
        # routers (tier=0) → db_libs (tier=5): evidence=[1,2,3,4]
        # Фактический кейс из лога
        result = adapter._detect_skip_layer_violations(
            layer_edges={("routers", "db_libs")},
            tier_map=self._TIER_MAP,
            interface_layer_names=self._INTERFACES,
        )
        assert len(result) == 1
        assert result[0]["evidence"] == [1, 2, 3, 4]
        assert result[0]["skip_distance"] == 4
        assert result[0]["severity"] == "error"
