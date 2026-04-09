# ===================================================================================================
# Интеграционные тесты для report_aggregator.aggregate_results()
#
# Стратегия:
# - каждый тест передает синтетический (но реалистичный) context dict в aggregate_results()
# - проверяется только публичный контракт выходного словаря — устойчивость к рефакторингу
# - adapter-специфичные детали не тестируются здесь (покрыты в tests/static_adapters/)
#
# Покрываемые сценарии (T1–T8 из SOLID_audit.md §5.4):
#   T1 — LAYER_VIOLATION: ImportLinter + ImportGraph на одной паре -> 1 событие, 2 evidence, strength=strong
#   T2 — Кросс-метрики: class_lcom4 на FunctionMetrics, OVERLOADED_CLASS
#   T3 — Graceful degradation: pyan3 отсутствует -> dead_code=[], adapters_failed содержит "pyan3"
#   T4 — HIGH_CC_METHOD severity: CC=16 -> error, CC=11 -> warning, CC=CC_THRESHOLD -> no event
#   T5 — DEAD_CODE_NODE confidence: collision_rate<0.35 -> error; >=0.35 -> warning
#   T6 — IMPORT_CYCLE: двунаправленное ребро -> событие IMPORT_CYCLE
#   T7 — IMPORT_CYCLE false negative (xfail): 3-узловой цикл не обнаруживается (Phase 1)
#   T8 — Empty config: нет исключений, meta.config_defaults_used=True
# ===================================================================================================

import pytest

from solid_dashboard.report_aggregator import aggregate_results
from solid_dashboard.defaults import CC_THRESHOLD


# ---------------------------------------------------------------------------
# Вспомогательные фабрики синтетического context
# ---------------------------------------------------------------------------

_FP = "app/services/search_service.py"  # файл, используемый в большинстве тестов


def _radon_context(complexity: int = 5, filepath: str = _FP, lineno: int = 47,
                   name: str = "run", fn_type: str = "method") -> dict:
    """Минимальный валидный RadonAdapter output с одним элементом."""
    rank = "F" if complexity > 15 else ("C" if complexity > 10 else "A")
    return {
        "total_items": 1,
        "mean_cc": float(complexity),
        "high_complexity_count": int(complexity > 10),
        "lizard_used": False,
        "items": [{
            "name": name, "type": fn_type, "complexity": complexity,
            "rank": rank, "lineno": lineno, "filepath": filepath,
        }],
        "maintainability": {
            "total_files": 1, "mean_mi": 62.0, "low_mi_count": 0,
            "files": [{"filepath": filepath, "mi": 62.0, "rank": "A"}],
        },
    }


def _cohesion_context(lcom4: float = 2.0, filepath: str = _FP,
                      class_name: str = "SearchService", lineno: int = 12,
                      methods_count: int = 3) -> dict:
    """Минимальный валидный CohesionAdapter output с одним классом."""
    return {
        "total_classes_analyzed": 1, "concrete_classes_count": 1,
        "mean_cohesion_all": lcom4, "mean_cohesion_multi_method": lcom4,
        "analyzed_classes_count": 1, "low_cohesion_count": int(lcom4 > 1),
        "low_cohesion_excluded_count": 0, "low_cohesion_excluded_classes": [],
        "low_cohesion_threshold": 1,
        "classes": [{
            "name": class_name,          # raw field is "name" — D1 correction
            "methods_count": methods_count,
            "cohesion_score": lcom4,
            "cohesion_score_norm": round(1.0 / lcom4, 4) if lcom4 > 1.0 else 1.0,
            "filepath": filepath, "lineno": lineno,
            "class_kind": "concrete", "excluded_from_aggregation": False,
        }],
    }


def _graph_context(edges=None, violations=None,
                   nodes=None) -> dict:
    """Минимальный валидный ImportGraphAdapter output."""
    if nodes is None:
        nodes = [
            {"id": "routers",  "label": "routers",  "ca": 0, "ce": 1, "instability": 1.0},
            {"id": "models",   "label": "models",   "ca": 1, "ce": 0, "instability": 0.0},
            {"id": "services", "label": "services", "ca": 1, "ce": 1, "instability": 0.5},
            {"id": "infrastructure", "label": "infrastructure", "ca": 1, "ce": 1, "instability": 0.5},
        ]
    return {
        "nodes": nodes,
        "edges": edges or [],
        "violations": violations or [],
    }


def _linter_context(broken_imports: list | None = None,
                    contract_name: str = "Scopus API layered architecture") -> dict:
    """Минимальный валидный ImportLinterAdapter output."""
    broken = broken_imports or []
    return {
        "is_success": len(broken) == 0,
        "contracts_checked": 1,
        "broken_contracts": int(bool(broken)),
        "kept_contracts": int(not broken),
        "violations": [contract_name] if broken else [],
        "violation_details": [{"contract_name": contract_name, "status": "BROKEN",
                                "broken_imports": broken}] if broken else [],
        "raw_output": "",
    }


def _pyan3_context(dead_nodes: list | None = None,
                   collision_rate: float = 0.0) -> dict:
    """Минимальный валидный Pyan3Adapter output."""
    dead = dead_nodes or []
    return {
        "is_success": True,
        "node_count": 10, "edge_count": 0,
        "edge_count_high": 0, "edge_count_low": 0,
        "nodes": [], "edges": [],
        "dead_node_count": len(dead), "dead_nodes": dead,
        "root_node_count": 0, "root_nodes": [],
        "suspicious_blocks": [], "collision_rate": collision_rate,
        "raw_output": "",
    }


def _base_config() -> dict:
    """Конфигурация, соответствующая solid_config.json (минимальная версия)."""
    return {
        "package_root": "app",
        "cohesion_threshold": 1,
        "layers": {
            "routers": ["routers"],
            "services": ["services"],
            "infrastructure": ["infrastructure"],
            "interfaces": ["interfaces"],
            "models": ["models"],
        },
        "utility_layers": {"core": ["core"]},
        "layer_order": ["routers", "services", "infrastructure", "interfaces", "models"],
    }


# ---------------------------------------------------------------------------
# T1 — LAYER_VIOLATION: обе адаптеры -> 1 событие, 2 evidence, strength=strong
# ---------------------------------------------------------------------------

def test_t1_layer_violation_dedup():
    """
    ImportGraph SDP-001 + ImportLinter BROKEN на одной паре (routers, models)
    должны слиться в единое LAYER_VIOLATION с evidence от обоих адаптеров.
    """
    context = {
        "import_graph": _graph_context(
            edges=[{"source": "routers", "target": "models"}],
            violations=[{
                "rule": "SDP-001", "layer": "routers", "instability": 1.0,
                "dependency": "models", "dep_instability": 0.0,
                "severity": "error", "message": "SDP violation", "evidence": [],
            }],
        ),
        "import_linter": _linter_context(
            broken_imports=[{"importer": "app.routers.search", "imported": "app.models.paper"}]
        ),
    }

    result = aggregate_results(context, _base_config())
    violations = result["violations"]

    layer_violations = [v for v in violations if v["type"] == "LAYER_VIOLATION"]
    assert len(layer_violations) == 1, (
        f"Expected exactly 1 LAYER_VIOLATION, got {len(layer_violations)}")

    ev = layer_violations[0]
    assert ev["strength"] == "strong", "Expected strength=strong when both adapters fire"
    assert len(ev["evidence"]) == 2, (
        f"Expected 2 evidence entries, got {len(ev['evidence'])}")
    sources = {e["source"] for e in ev["evidence"]}
    assert sources == {"import_linter", "import_graph"}, (
        f"Expected sources import_linter + import_graph, got {sources}")
    assert ev["severity"] == "error"


# ---------------------------------------------------------------------------
# T2 — Кросс-метрики: class_lcom4 на FunctionMetrics + OVERLOADED_CLASS
# ---------------------------------------------------------------------------

def test_t2_cross_metric_denormalization():
    """
    Cohesion-класс SearchService (lineno=12, lcom4=2.0) и Radon-метод run
    (lineno=47, CC=16) в одном файле.
    Ожидается: fn.class_lcom4=2.0, class.max_method_cc=16, одно OVERLOADED_CLASS событие.
    """
    context = {
        "radon": _radon_context(complexity=16, lineno=47, fn_type="method"),
        "cohesion": _cohesion_context(lcom4=2.0, lineno=12),
    }

    result = aggregate_results(context, _base_config())

    # Проверяем денормализацию class_lcom4 на FunctionMetrics
    functions = result["entities"]["functions"]
    assert len(functions) == 1
    fn = functions[0]
    assert fn["class_lcom4"] == 2.0, (
        f"Expected class_lcom4=2.0 on FunctionMetrics, got {fn['class_lcom4']}")

    # Проверяем max_method_cc на ClassMetrics
    classes = result["entities"]["classes"]
    assert len(classes) == 1
    cls = classes[0]
    assert cls["max_method_cc"] == 16, (
        f"Expected max_method_cc=16 on ClassMetrics, got {cls['max_method_cc']}")

    # Проверяем OVERLOADED_CLASS
    overloaded = [v for v in result["violations"] if v["type"] == "OVERLOADED_CLASS"]
    assert len(overloaded) == 1, f"Expected 1 OVERLOADED_CLASS, got {len(overloaded)}"
    assert overloaded[0]["strength"] == "strong"
    sources = {e["source"] for e in overloaded[0]["evidence"]}
    assert sources == {"cohesion", "radon"}


# ---------------------------------------------------------------------------
# T3 — Graceful degradation: pyan3 отсутствует
# ---------------------------------------------------------------------------

def test_t3_pyan3_absent_graceful_degradation():
    """
    Контекст без ключа "pyan3". Агрегатор не должен падать.
    dead_code должен быть пустым, meta.adapters_failed содержит "pyan3".
    Все остальные секции отчета остаются корректными.
    """
    context = {
        "radon": _radon_context(complexity=5),
        "cohesion": _cohesion_context(lcom4=1.0),
        # "pyan3" намеренно отсутствует
    }

    result = aggregate_results(context, _base_config())

    assert result["dead_code"] == [], (
        "Expected empty dead_code when pyan3 is absent")
    assert "pyan3" in result["meta"]["adapters_failed"], (
        "Expected 'pyan3' in adapters_failed")
    assert result["summary"]["dead_code"]["dead_node_count"] == 0

    # Остальные секции должны быть заполнены корректно
    assert "meta" in result
    assert "entities" in result
    assert "violations" in result
    assert "summary" in result


# ---------------------------------------------------------------------------
# T4 — HIGH_CC_METHOD severity: CC=16 -> error, CC=11 -> warning, CC=threshold -> no event
# ---------------------------------------------------------------------------

def test_t4_high_cc_method_severity():
    """
    Radon item с CC=16 должен породить HIGH_CC_METHOD с severity=error.
    CC=11 (первое значение выше порога) -> severity=warning.
    CC=CC_THRESHOLD (ровно 10) -> событие не создается (граница: равенство порогу не нарушение).
    """
    # CC=16 -> error
    context_error = {"radon": _radon_context(complexity=16)}
    result_error = aggregate_results(context_error, _base_config())
    cc_events = [v for v in result_error["violations"] if v["type"] == "HIGH_CC_METHOD"]
    assert len(cc_events) == 1
    assert cc_events[0]["severity"] == "error", (
        f"CC=16 should produce severity=error, got {cc_events[0]['severity']}")
    assert cc_events[0]["evidence"][0]["source"] == "radon"
    assert cc_events[0]["strength"] == "weak"

    # CC=11 -> warning (первое значение строго выше CC_THRESHOLD=10)
    context_warning = {"radon": _radon_context(complexity=11)}
    result_warning = aggregate_results(context_warning, _base_config())
    cc_events_w = [v for v in result_warning["violations"] if v["type"] == "HIGH_CC_METHOD"]
    assert len(cc_events_w) == 1
    assert cc_events_w[0]["severity"] == "warning", (
        f"CC=11 should produce severity=warning, got {cc_events_w[0]['severity']}")

    # CC=CC_THRESHOLD (ровно 10) -> нет события: граница не является нарушением
    context_boundary = {"radon": _radon_context(complexity=CC_THRESHOLD)}
    result_boundary = aggregate_results(context_boundary, _base_config())
    cc_events_b = [v for v in result_boundary["violations"] if v["type"] == "HIGH_CC_METHOD"]
    assert len(cc_events_b) == 0, (
        f"CC={CC_THRESHOLD} (exact threshold) must NOT produce a HIGH_CC_METHOD event, "
        f"got {len(cc_events_b)} event(s). Invariant: only cc > CC_THRESHOLD triggers a violation.")


# ---------------------------------------------------------------------------
# T5 — DEAD_CODE_NODE confidence -> severity
# ---------------------------------------------------------------------------

def test_t5_dead_node_confidence_maps_to_severity():
    """
    collision_rate < 0.35  -> confidence=high -> severity=error
    collision_rate >= 0.35 -> confidence=low  -> severity=warning
    """
    # High confidence (low collision rate)
    ctx_high = {"pyan3": _pyan3_context(dead_nodes=["app.utils.legacy.old_fn"], collision_rate=0.0)}
    result_high = aggregate_results(ctx_high, _base_config())
    dead_events_high = [v for v in result_high["violations"] if v["type"] == "DEAD_CODE_NODE"]
    assert len(dead_events_high) == 1
    assert dead_events_high[0]["severity"] == "error", (
        "High-confidence dead node should produce severity=error")

    # Also verify in dead_code section
    assert len(result_high["dead_code"]) == 1
    assert result_high["dead_code"][0]["confidence"] == "high"

    # Low confidence (high collision rate)
    ctx_low = {"pyan3": _pyan3_context(dead_nodes=["app.utils.legacy.old_fn"], collision_rate=0.5)}
    result_low = aggregate_results(ctx_low, _base_config())
    dead_events_low = [v for v in result_low["violations"] if v["type"] == "DEAD_CODE_NODE"]
    assert len(dead_events_low) == 1
    assert dead_events_low[0]["severity"] == "warning", (
        "Low-confidence dead node should produce severity=warning")


# ---------------------------------------------------------------------------
# T6 — IMPORT_CYCLE: двунаправленное ребро
# ---------------------------------------------------------------------------

def test_t6_import_cycle_bidirectional():
    """
    services->infrastructure И infrastructure->services -> одно событие IMPORT_CYCLE,
    severity=error. Только одна пара, несмотря на два обратных ребра.
    """
    context = {
        "import_graph": _graph_context(
            edges=[
                {"source": "services",        "target": "infrastructure"},
                {"source": "infrastructure",   "target": "services"},
            ],
        ),
    }

    result = aggregate_results(context, _base_config())
    cycle_events = [v for v in result["violations"] if v["type"] == "IMPORT_CYCLE"]

    assert len(cycle_events) == 1, (
        f"Expected exactly 1 IMPORT_CYCLE event, got {len(cycle_events)}")
    assert cycle_events[0]["severity"] == "error"
    loc = cycle_events[0]["location"]
    pair = {loc["from_layer"], loc["to_layer"]}
    assert pair == {"services", "infrastructure"}


# ---------------------------------------------------------------------------
# T7 — IMPORT_CYCLE false negative: 3-узловой цикл (известное ограничение Phase 1)
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=False,
    reason="Phase 1: bidirectional scan only. "
           "n-node cycles (A->B->C->A) are not detected. "
           "Phase 2 (Tarjan SCC) required — SOLID_audit.md §3.3 Rule D4.",
)
def test_t7_import_cycle_3node_false_negative():
    """
    Цикл A->B->C->A без обратных пар НЕ обнаруживается Phase 1 алгоритмом.
    Этот тест документирует известное ограничение и помечен как xfail.
    Если тест внезапно пройдет (Phase 2 реализован), xfail станет xpass.
    """
    context = {
        "import_graph": _graph_context(
            nodes=[
                {"id": "routers",        "label": "routers",        "ca": 0, "ce": 1, "instability": 1.0},
                {"id": "services",       "label": "services",       "ca": 1, "ce": 1, "instability": 0.5},
                {"id": "infrastructure", "label": "infrastructure", "ca": 1, "ce": 1, "instability": 0.5},
            ],
            edges=[
                {"source": "routers",        "target": "services"},
                {"source": "services",       "target": "infrastructure"},
                {"source": "infrastructure", "target": "routers"},   # замыкает 3-цикл
                # нет прямых обратных пар: services->routers, infrastructure->services, routers->infrastructure
            ],
        ),
    }

    result = aggregate_results(context, _base_config())
    cycle_events = [v for v in result["violations"] if v["type"] == "IMPORT_CYCLE"]

    # В Phase 1 цикл НЕ обнаружится — это xfail
    assert len(cycle_events) >= 1, (
        "Phase 2 not yet implemented: 3-node cycles not detected by bidirectional scan")


# ---------------------------------------------------------------------------
# T8 — Empty config: нет исключений, meta.config_defaults_used=True
# ---------------------------------------------------------------------------

def test_t8_empty_config_graceful_defaults():
    """
    aggregate_results(context, config={}) не должен падать.
    Все пороги должны использовать значения по умолчанию:
      cc_threshold=10, lcom4_threshold=1.
    meta.config_defaults_used должен быть True.
    Нарушения должны корректно вычисляться с дефолтными порогами.
    """
    context = {
        "radon": _radon_context(complexity=16),   # > CC_THRESHOLD=10, > 15 -> error
        "cohesion": _cohesion_context(lcom4=2.0), # > cohesion_threshold=1 -> LOW_COHESION
        "pyan3": _pyan3_context(dead_nodes=["app.utils.legacy.fn"]),
    }

    result = aggregate_results(context, config={})  # пустой конфиг

    assert result["meta"]["config_defaults_used"] is True, (
        "Expected config_defaults_used=True for empty config")

    # CC событие должно быть с дефолтным порогом 10
    cc_events = [v for v in result["violations"] if v["type"] == "HIGH_CC_METHOD"]
    assert len(cc_events) == 1
    assert cc_events[0]["severity"] == "error"  # CC=16 > 15

    # LCOM4 событие должно быть с дефолтным порогом 1
    lcom4_events = [v for v in result["violations"] if v["type"] == "LOW_COHESION_CLASS"]
    assert len(lcom4_events) == 1

    # dead_code заполнен
    assert len(result["dead_code"]) == 1

    # Нет KeyError / TypeError при пустом config
    assert "meta" in result
    assert "summary" in result
    assert "entities" in result
    assert "violations" in result


# ---------------------------------------------------------------------------
# Дополнительный smoke-тест: полная схема отчета
# ---------------------------------------------------------------------------

def test_report_schema_keys_always_present():
    """
    Все обязательные ключи верхнего уровня и sub-секции всегда присутствуют,
    даже если context пустой (все адаптеры провалились).
    """
    result = aggregate_results(context={}, config=_base_config())

    # Верхний уровень
    for key in ("meta", "summary", "entities", "violations", "dead_code"):
        assert key in result, f"Missing top-level key: {key}"

    # meta
    for key in ("generated_at", "adapters_succeeded", "adapters_failed",
                "lizard_used", "config_defaults_used"):
        assert key in result["meta"], f"Missing meta key: {key}"

    # summary
    for key in ("complexity", "maintainability", "cohesion", "imports",
                "dead_code", "violations_total", "strong_violations", "weak_violations"):
        assert key in result["summary"], f"Missing summary key: {key}"

    # entities
    for key in ("files", "classes", "functions", "layers"):
        assert key in result["entities"], f"Missing entities key: {key}"

    # Все адаптеры должны быть в adapters_failed при пустом context
    assert set(result["meta"]["adapters_failed"]) == {
        "radon", "cohesion", "import_graph", "import_linter", "pyan3"
    }

    # violations_total консистентен с len(violations)
    assert result["summary"]["violations_total"] == len(result["violations"])
