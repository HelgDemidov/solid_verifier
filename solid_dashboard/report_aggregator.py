# ===================================================================================================
# Report Aggregator (report_aggregator.py)
#
# Роль: агрегация и нормализация результатов всех статических адаптеров в единый отчет.
# Входные данные: context dict из pipeline.py + config dict.
# Выходные данные: AggregatedReport-совместимый dict (валидируется через schema.AggregatedReport).
#
# Этапы реализации:
#   Commit B — Шаги 1–2: нормализация + построение индексов
#   Commit C — Шаги 3–4: кросс-резолюция и денормализация метрик
#   Commit D — Шаг 5:    одиночные события нарушений (текущий файл)
#   Commit E — Шаг 6:    многоисточниковые события (LAYER_VIOLATION, OVERLOADED_CLASS)
#   Commit F — Шаги 7–9: дедупликация, сводка, финальная сборка
# ===================================================================================================

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from solid_dashboard.schema import (
    AggregatedReport,
    AggregatedSummary,
    ClassMetrics,
    DeadCodeEntry,
    EntitiesSection,
    EvidenceItem,
    FileMetrics,
    FunctionMetrics,
    LayerMetrics,
    ReportMeta,
    ViolationEvent,
    ViolationLocation,
    ViolationMetrics,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# CC threshold mirrors the hardcoded value in radon_adapter.py: `if complexity > 10`
# NOT read from config — RadonAdapter has no cc_threshold config key.
CC_THRESHOLD: int = 10

# Adapter keys as they appear in the context dict populated by pipeline.py
_ADAPTER_KEYS: Tuple[str, ...] = ("radon", "cohesion", "import_graph", "import_linter", "pyan3")

# Severity rank for sorting (higher = more severe)
_SEVERITY_RANK: Dict[str, int] = {"error": 2, "warning": 1, "info": 0}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def aggregate_results(context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aggregates raw adapter results into a single structured report.

    Parameters
    ----------
    context : Dict[str, Any]
        Pipeline context dict populated by run_pipeline(). Expected keys:
        "radon", "cohesion", "import_graph", "import_linter", "pyan3".
        All keys are optional — absent or error-containing values degrade gracefully.
    config : Dict[str, Any]
        The same config dict passed to run_pipeline() and each adapter.
        Config keys consumed by the aggregator:
          config.get("cohesion_threshold", 1)  -> lcom4_threshold (int)
          config.get("layers", {})             -> layer prefix map for module->layer resolution
          config.get("utility_layers", {})     -> crosscutting layer names (no tier)
          config.get("layer_order", [])        -> tier ordering for LayerMetrics.tier
          config.get("package_root", "")       -> package name for prefix normalization
        Note: CC threshold (10) is a module constant (CC_THRESHOLD), not read from config.

    Returns
    -------
    Dict[str, Any]
        AggregatedReport-shaped dict. Always valid — missing adapter data produces
        empty lists/zeroes, never absent keys.
        Validate with: AggregatedReport.model_validate(result)
    """
    if config is None:
        config = {}

    config_defaults_used: bool = not bool(config)

    # -----------------------------------------------------------------------
    # Step 1 — Guard and normalize raw adapter outputs
    # -----------------------------------------------------------------------
    lcom4_threshold: int = int(config.get("cohesion_threshold", 1))

    adapters_succeeded: List[str] = []
    adapters_failed: List[str] = []

    radon_fns, mi_files = _safe_normalize(
        "radon", context, _normalize_radon,
        adapters_succeeded, adapters_failed,
        default=([], []),
    )

    cohesion_classes: List[ClassMetrics] = _safe_normalize(
        "cohesion", context, _normalize_cohesion,
        adapters_succeeded, adapters_failed,
        default=[],
    )

    graph_layers, graph_edges, graph_violations = _safe_normalize(
        "import_graph", context, _normalize_import_graph,
        adapters_succeeded, adapters_failed,
        default=([], [], []),
    )

    contract_violations: List[Dict[str, Any]] = _safe_normalize(
        "import_linter", context, _normalize_import_linter,
        adapters_succeeded, adapters_failed,
        default=[],
    )

    _pyan3_nodes, dead_entries = _safe_normalize(
        "pyan3", context, _normalize_pyan3,
        adapters_succeeded, adapters_failed,
        default=([], []),
    )

    lizard_used: bool = bool(
        isinstance(context.get("radon"), dict) and context["radon"].get("lizard_used", False)
    )

    # -----------------------------------------------------------------------
    # Step 2 — Build entity indexes
    # -----------------------------------------------------------------------
    file_index: Dict[str, FileMetrics] = _build_file_index(radon_fns, mi_files, cohesion_classes)
    class_index: Dict[str, ClassMetrics] = _build_class_index(cohesion_classes)
    fn_index: Dict[str, FunctionMetrics] = _build_function_index(radon_fns)
    layer_index: Dict[str, LayerMetrics] = _build_layer_index(graph_layers)

    # -----------------------------------------------------------------------
    # Step 3 — Cross-adapter resolution
    # -----------------------------------------------------------------------
    fn_to_class: Dict[str, str] = _resolve_function_to_class(radon_fns, cohesion_classes)
    _attach_tier_to_layers(layer_index, config)
    module_to_layer_map: Dict[str, str] = _build_module_to_layer_map(config)

    # -----------------------------------------------------------------------
    # Step 4 — Denormalize cross-metrics
    # -----------------------------------------------------------------------
    _attach_cross_metrics(fn_index, class_index, file_index, fn_to_class)

    # -----------------------------------------------------------------------
    # Step 5 — Emit single-source ViolationEvent protos
    # -----------------------------------------------------------------------
    cc_events = _emit_cc_events(list(fn_index.values()), CC_THRESHOLD)
    mi_events = _emit_mi_events(list(file_index.values()))
    cohesion_events = _emit_cohesion_events(list(class_index.values()), lcom4_threshold)
    dead_events = _emit_dead_code_events(dead_entries)
    cycle_events = _detect_import_cycles(graph_edges)

    all_violations: List[ViolationEvent] = (
        cc_events + mi_events + cohesion_events + dead_events + cycle_events
        # multi-source events added in Commit E
    )

    # -----------------------------------------------------------------------
    # Steps 6–9 implemented in Commits E–F.
    # -----------------------------------------------------------------------
    _ = (graph_violations, contract_violations, module_to_layer_map)

    # -----------------------------------------------------------------------
    # Assemble report
    # -----------------------------------------------------------------------
    meta = ReportMeta(
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        adapter_versions_available=list(_ADAPTER_KEYS),
        adapters_succeeded=adapters_succeeded,
        adapters_failed=adapters_failed,
        lizard_used=lizard_used,
        config_defaults_used=config_defaults_used,
    )

    entities = EntitiesSection(
        files=sorted(file_index.values(), key=lambda f: f.filepath),
        classes=sorted(class_index.values(), key=lambda c: c.class_id),
        functions=sorted(fn_index.values(), key=lambda fn: fn.function_id),
        layers=sorted(layer_index.values(), key=lambda la: la.layer_name),
    )

    report = AggregatedReport(
        meta=meta,
        summary=AggregatedSummary(),  # populated in Commit F (_compute_summary)
        entities=entities,
        violations=all_violations,
        dead_code=dead_entries,
    )

    return report.model_dump()


# ---------------------------------------------------------------------------
# Internal helper: safe normalization with per-adapter error isolation
# ---------------------------------------------------------------------------

def _safe_normalize(
    key: str,
    context: Dict[str, Any],
    normalize_fn,
    succeeded: List[str],
    failed: List[str],
    default: Any,
) -> Any:
    """
    Calls normalize_fn(raw) for context[key], isolating failures per adapter.

    On missing key, error result, or exception:
      marks adapter as failed, returns default.
    On success:
      marks adapter as succeeded, returns result.
    """
    raw = context.get(key)
    if raw is None or _is_error_result(raw):
        failed.append(key)
        return default
    try:
        result = normalize_fn(raw)
        succeeded.append(key)
        return result
    except Exception:
        failed.append(key)
        return default


def _is_error_result(raw: Any) -> bool:
    """
    Returns True if an adapter result signals failure.

    Covers two error patterns used across adapters:
      {"error": "..."}           — radon_adapter, import_graph_adapter, import_linter_adapter
      {"is_success": False, ...} — import_linter_adapter, pyan3_adapter
    """
    if not isinstance(raw, dict):
        return True
    if "error" in raw:
        return True
    if raw.get("is_success") is False:
        return True
    return False


# ---------------------------------------------------------------------------
# Normalizers — one per adapter
# ---------------------------------------------------------------------------

def _normalize_radon(
    raw: Dict[str, Any],
) -> Tuple[List[FunctionMetrics], List[Dict[str, Any]]]:
    """
    Normalizes RadonAdapter output into FunctionMetrics list + raw MI file dicts.
    function_id format: "<filepath>::<lineno>::<name>"
    """
    fns: List[FunctionMetrics] = []

    for item in raw.get("items", []):
        fp: str = item.get("filepath", "")
        lineno: int = item.get("lineno", 0)
        name: str = item.get("name", "")

        fns.append(FunctionMetrics(
            function_id=f"{fp}::{lineno}::{name}",
            filepath=fp,
            name=name,
            type=item.get("type", "function"),
            lineno=lineno,
            cc=item.get("complexity", 0),
            rank=item.get("rank", "A"),
            parameter_count=item.get("parameter_count"),
        ))

    mi_raw = raw.get("maintainability") or {}
    mi_files: List[Dict[str, Any]] = (
        mi_raw.get("files", []) if isinstance(mi_raw, dict) else []
    )
    return fns, mi_files


def _normalize_cohesion(raw: Dict[str, Any]) -> List[ClassMetrics]:
    """
    Normalizes CohesionAdapter output into ClassMetrics list.
    D1 correction: raw field is "name", mapped to class_name.
    class_id format: "<filepath>::<class_name>"
    """
    classes: List[ClassMetrics] = []

    for record in raw.get("classes", []):
        raw_name: str = record.get("name", "")
        fp: str = record.get("filepath", "")
        lcom4_val = record.get("cohesion_score")

        classes.append(ClassMetrics(
            class_id=f"{fp}::{raw_name}",
            filepath=fp,
            class_name=raw_name,
            lineno=record.get("lineno", 0),
            class_kind=record.get("class_kind", "concrete"),
            lcom4=float(lcom4_val) if lcom4_val is not None else None,
            lcom4_norm=record.get("cohesion_score_norm"),
            methods_count=record.get("methods_count", 0),
            excluded_from_aggregation=record.get("excluded_from_aggregation", False),
            label=raw_name,
        ))
    return classes


def _normalize_import_graph(
    raw: Dict[str, Any],
) -> Tuple[List[LayerMetrics], List[Dict[str, str]], List[Dict[str, Any]]]:
    """
    Normalizes ImportGraphAdapter output.
    D2 correction: preserves raw node["label"] in LayerMetrics.label.
    """
    layers: List[LayerMetrics] = []

    for node in raw.get("nodes", []):
        layer_name: str = node.get("id", "")
        layers.append(LayerMetrics(
            layer_id=layer_name,
            layer_name=layer_name,
            label=node.get("label", layer_name),
            tier=None,
            ca=node.get("ca", 0),
            ce=node.get("ce", 0),
            instability=node.get("instability", 0.0),
        ))

    return layers, raw.get("edges", []), raw.get("violations", [])


def _normalize_import_linter(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Returns violation_details verbatim."""
    return raw.get("violation_details", [])


def _normalize_pyan3(
    raw: Dict[str, Any],
) -> Tuple[List[str], List[DeadCodeEntry]]:
    """
    Normalizes Pyan3Adapter output.
    Derives global confidence from collision_rate vs 0.35 threshold
    (matches solid_config.json pyan3.collision_rate_threshold).
    """
    collision_rate: float = float(raw.get("collision_rate", 0.0))
    global_confidence: str = "low" if collision_rate >= 0.35 else "high"

    dead_entries: List[DeadCodeEntry] = [
        DeadCodeEntry(
            dead_id=qname,
            qualified_name=qname,
            confidence=global_confidence,
        )
        for qname in raw.get("dead_nodes", [])
    ]
    return raw.get("nodes", []), dead_entries


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------

def _build_file_index(
    fns: List[FunctionMetrics],
    mi_files: List[Dict[str, Any]],
    cohesion_classes: List[ClassMetrics],
) -> Dict[str, FileMetrics]:
    """Builds filepath -> FileMetrics, aggregating CC, MI, and class count."""
    cc_by_file: Dict[str, List[int]] = defaultdict(list)
    for fn in fns:
        cc_by_file[fn.filepath].append(fn.cc)

    class_count_by_file: Dict[str, int] = defaultdict(int)
    for cls in cohesion_classes:
        class_count_by_file[cls.filepath] += 1

    mi_lookup: Dict[str, Dict[str, Any]] = {
        rec["filepath"]: rec
        for rec in mi_files
        if isinstance(rec, dict) and "filepath" in rec
    }

    all_fps = set(cc_by_file.keys()) | set(mi_lookup.keys()) | set(class_count_by_file.keys())
    index: Dict[str, FileMetrics] = {}

    for fp in sorted(all_fps):
        cc_list = cc_by_file.get(fp, [])
        mi_rec = mi_lookup.get(fp)
        index[fp] = FileMetrics(
            file_id=fp,
            filepath=fp,
            mi=float(mi_rec["mi"]) if mi_rec else None,
            mi_rank=mi_rec.get("rank") if mi_rec else None,
            function_count=len(cc_list),
            mean_cc=round(sum(cc_list) / len(cc_list), 2) if cc_list else 0.0,
            max_cc=max(cc_list) if cc_list else 0,
            high_cc_count=sum(1 for cc in cc_list if cc > CC_THRESHOLD),
            class_count=class_count_by_file.get(fp, 0),
        )
    return index


def _build_class_index(classes: List[ClassMetrics]) -> Dict[str, ClassMetrics]:
    return {cls.class_id: cls for cls in classes}


def _build_function_index(fns: List[FunctionMetrics]) -> Dict[str, FunctionMetrics]:
    return {fn.function_id: fn for fn in fns}


def _build_layer_index(layers: List[LayerMetrics]) -> Dict[str, LayerMetrics]:
    return {layer.layer_name: layer for layer in layers}


# ---------------------------------------------------------------------------
# Step 3: cross-adapter resolution
# ---------------------------------------------------------------------------

def _resolve_function_to_class(
    fns: List[FunctionMetrics],
    classes: List[ClassMetrics],
) -> Dict[str, str]:
    """
    Returns {function_id: class_id} for type="method" items via lineno-range matching.
    class.lineno <= method.lineno < next_class.lineno (or end of file).
    """
    classes_by_file: Dict[str, List[ClassMetrics]] = defaultdict(list)
    for cls in classes:
        classes_by_file[cls.filepath].append(cls)
    for fp in classes_by_file:
        classes_by_file[fp].sort(key=lambda c: c.lineno)

    fn_to_class: Dict[str, str] = {}

    for fn in fns:
        if fn.type != "method":
            continue
        file_classes = classes_by_file.get(fn.filepath, [])
        if not file_classes:
            continue

        matched_class: Optional[ClassMetrics] = None
        for i, cls in enumerate(file_classes):
            if cls.lineno > fn.lineno:
                break
            next_lineno = (
                file_classes[i + 1].lineno if i + 1 < len(file_classes) else float("inf")
            )
            if cls.lineno <= fn.lineno < next_lineno:
                matched_class = cls

        if matched_class is not None:
            fn_to_class[fn.function_id] = matched_class.class_id

    return fn_to_class


def _resolve_tier_map(config: Dict[str, Any]) -> Optional[Dict[str, int]]:
    """
    Builds layer_name -> tier_index from config["layer_order"].
    Supports flat-list (Format A) and grouped-list (Format B).
    Returns None if layer_order is absent or empty.
    """
    raw_order = config.get("layer_order")
    if not raw_order or not isinstance(raw_order, list):
        return None

    tier_map: Dict[str, int] = {}
    first = raw_order[0] if raw_order else None

    if isinstance(first, str):
        for i, name in enumerate(raw_order):
            if isinstance(name, str) and name.strip():
                tier_map[name.strip()] = i
    elif isinstance(first, list):
        for i, group in enumerate(raw_order):
            if not isinstance(group, list):
                continue
            for name in group:
                if isinstance(name, str) and name.strip():
                    tier_map[name.strip()] = i
    else:
        return None

    return tier_map if tier_map else None


def _attach_tier_to_layers(
    layer_index: Dict[str, LayerMetrics],
    config: Dict[str, Any],
) -> None:
    """
    Mutates LayerMetrics in place: sets tier from layer_order and
    marks is_utility_layer=True for layers in config["utility_layers"].
    """
    tier_map = _resolve_tier_map(config)
    utility_names: Set[str] = set((config.get("utility_layers") or {}).keys())

    for layer_name, layer_m in layer_index.items():
        layer_m.is_utility_layer = layer_name in utility_names
        if tier_map is not None and layer_name not in utility_names:
            layer_m.tier = tier_map.get(layer_name)


def _build_module_to_layer_map(config: Dict[str, Any]) -> Dict[str, str]:
    """
    Builds fully-qualified module prefix -> layer_name lookup.
    Normalizes config["layers"] + config["utility_layers"] with package_root.
    """
    package_root: str = config.get("package_root", "")
    package_prefix: str = f"{package_root}." if package_root else ""
    result: Dict[str, str] = {}

    for section_key in ("layers", "utility_layers"):
        for layer_name, raw_value in (config.get(section_key) or {}).items():
            paths = [raw_value] if isinstance(raw_value, str) else (
                [p for p in raw_value if isinstance(p, str)]
                if isinstance(raw_value, list) else []
            )
            for path in paths:
                cleaned = path.strip()
                if not cleaned:
                    continue
                normalized = (
                    cleaned if (not package_root or cleaned == package_root
                                or cleaned.startswith(package_prefix))
                    else f"{package_root}.{cleaned}"
                )
                result[normalized] = layer_name

    return result


def _resolve_module_to_layer(
    module: str,
    module_to_layer_map: Dict[str, str],
) -> Optional[str]:
    """Longest-prefix match: "app.routers.search" -> "routers"."""
    best_len = 0
    best_layer: Optional[str] = None
    for prefix, layer_name in module_to_layer_map.items():
        if module == prefix or module.startswith(prefix + "."):
            if len(prefix) > best_len:
                best_len = len(prefix)
                best_layer = layer_name
    return best_layer


# ---------------------------------------------------------------------------
# Step 4: cross-metric denormalization
# ---------------------------------------------------------------------------

def _attach_cross_metrics(
    fn_index: Dict[str, FunctionMetrics],
    class_index: Dict[str, ClassMetrics],
    file_index: Dict[str, FileMetrics],
    fn_to_class: Dict[str, str],
) -> None:
    """
    Denormalizes cross-adapter metrics into entity objects (mutates in place).
      FunctionMetrics.file_mi      <- FileMetrics.mi
      FunctionMetrics.class_id     <- fn_to_class[fn.function_id]
      FunctionMetrics.class_lcom4  <- ClassMetrics.lcom4
      ClassMetrics.file_mi         <- FileMetrics.mi
      ClassMetrics.max_method_cc   <- max CC across class methods
      ClassMetrics.mean_method_cc  <- mean CC across class methods
    """
    for fn in fn_index.values():
        file_m = file_index.get(fn.filepath)
        if file_m is not None:
            fn.file_mi = file_m.mi

    for fn_id, class_id in fn_to_class.items():
        fn = fn_index.get(fn_id)
        cls = class_index.get(class_id)
        if fn is not None:
            fn.class_id = class_id
        if fn is not None and cls is not None:
            fn.class_lcom4 = cls.lcom4

    cc_per_class: Dict[str, List[int]] = defaultdict(list)
    for fn_id, class_id in fn_to_class.items():
        fn = fn_index.get(fn_id)
        if fn is not None:
            cc_per_class[class_id].append(fn.cc)

    for cls in class_index.values():
        file_m = file_index.get(cls.filepath)
        if file_m is not None:
            cls.file_mi = file_m.mi
        cc_list = cc_per_class.get(cls.class_id, [])
        if cc_list:
            cls.max_method_cc = max(cc_list)
            cls.mean_method_cc = round(sum(cc_list) / len(cc_list), 2)


# ---------------------------------------------------------------------------
# Step 5: single-source violation event emitters
# ---------------------------------------------------------------------------

def _make_event_id(event_type: str, *key_parts: str) -> str:
    """Builds a stable, human-readable violation ID: "<TYPE>::<part1>::<part2>::..."."""
    return "::".join([event_type] + list(key_parts))


def _emit_cc_events(
    fns: List[FunctionMetrics],
    cc_threshold: int,
) -> List[ViolationEvent]:
    """
    Emits HIGH_CC_METHOD events for functions/methods with CC > cc_threshold.

    Severity:
      CC > 15  -> "error"
      CC > 10  -> "warning"  (i.e., cc_threshold < CC <= 15)
    """
    events: List[ViolationEvent] = []

    for fn in fns:
        if fn.cc <= cc_threshold:
            continue

        severity = "error" if fn.cc > 15 else "warning"

        events.append(ViolationEvent(
            id=_make_event_id("HIGH_CC_METHOD", fn.filepath, str(fn.lineno), fn.name),
            type="HIGH_CC_METHOD",
            severity=severity,
            location=ViolationLocation(
                filepath=fn.filepath,
                lineno=fn.lineno,
                name=fn.name,
                class_name=fn.class_id.split("::")[-1] if fn.class_id else None,
            ),
            metrics=ViolationMetrics(
                cc=fn.cc,
                rank=fn.rank,
                parameter_count=fn.parameter_count,
            ),
            evidence=[EvidenceItem(
                source="radon",
                details={
                    "complexity": fn.cc,
                    "rank": fn.rank,
                    "type": fn.type,
                    "lineno": fn.lineno,
                },
            )],
            strength="weak",
        ))

    return events


def _emit_mi_events(files: List[FileMetrics]) -> List[ViolationEvent]:
    """
    Emits LOW_MI_FILE events for files with mi_rank "B" or "C".

    Severity:
      rank "C" (MI < 10)  -> "error"
      rank "B" (MI < 20)  -> "warning"
    """
    events: List[ViolationEvent] = []

    for f in files:
        if f.mi_rank not in ("B", "C"):
            continue

        severity = "error" if f.mi_rank == "C" else "warning"

        events.append(ViolationEvent(
            id=_make_event_id("LOW_MI_FILE", f.filepath),
            type="LOW_MI_FILE",
            severity=severity,
            location=ViolationLocation(filepath=f.filepath),
            metrics=ViolationMetrics(
                mi=f.mi,
                rank=f.mi_rank,
            ),
            evidence=[EvidenceItem(
                source="radon",
                details={
                    "mi": f.mi,
                    "rank": f.mi_rank,
                    "filepath": f.filepath,
                },
            )],
            strength="weak",
        ))

    return events


def _emit_cohesion_events(
    classes: List[ClassMetrics],
    lcom4_threshold: int,
) -> List[ViolationEvent]:
    """
    Emits LOW_COHESION_CLASS events for concrete classes with LCOM4 > lcom4_threshold.

    Severity:
      LCOM4 >= 3  -> "error"
      LCOM4 == 2  -> "warning"   (threshold < LCOM4 < 3)
    """
    events: List[ViolationEvent] = []

    for cls in classes:
        if cls.excluded_from_aggregation:
            continue
        if cls.lcom4 is None or cls.lcom4 <= lcom4_threshold:
            continue

        severity = "error" if cls.lcom4 >= 3 else "warning"

        events.append(ViolationEvent(
            id=_make_event_id("LOW_COHESION_CLASS", cls.filepath, cls.class_name),
            type="LOW_COHESION_CLASS",
            severity=severity,
            location=ViolationLocation(
                filepath=cls.filepath,
                lineno=cls.lineno,
                class_name=cls.class_name,
            ),
            metrics=ViolationMetrics(lcom4=cls.lcom4),
            evidence=[EvidenceItem(
                source="cohesion",
                details={
                    "cohesion_score": cls.lcom4,
                    "cohesion_score_norm": cls.lcom4_norm,
                    "methods_count": cls.methods_count,
                    "class_kind": cls.class_kind,
                },
            )],
            strength="weak",
        ))

    return events


def _emit_dead_code_events(dead_entries: List[DeadCodeEntry]) -> List[ViolationEvent]:
    """
    Emits DEAD_CODE_NODE events for each dead node from Pyan3Adapter.

    Severity:
      confidence "high" -> "error"
      confidence "low"  -> "warning"
    """
    events: List[ViolationEvent] = []

    for entry in dead_entries:
        severity = "error" if entry.confidence == "high" else "warning"

        events.append(ViolationEvent(
            id=_make_event_id("DEAD_CODE_NODE", entry.qualified_name),
            type="DEAD_CODE_NODE",
            severity=severity,
            location=ViolationLocation(
                filepath=entry.filepath,
                name=entry.qualified_name,
                layer=entry.layer,
            ),
            metrics=ViolationMetrics(),
            evidence=[EvidenceItem(
                source="pyan3",
                details={
                    "qualified_name": entry.qualified_name,
                    "confidence": entry.confidence,
                },
            )],
            strength="weak",
        ))

    return events


def _detect_import_cycles(edges: List[Dict[str, str]]) -> List[ViolationEvent]:
    """
    Emits IMPORT_CYCLE events by scanning ImportGraph edges for bidirectional pairs.

    Phase 1 — known limitation (SOLID_audit.md D3):
      Only 2-node cycles (A->B and B->A) are detected.
      Cycles of length >= 3 (A->B->C->A with no direct reversal) are silently missed.
      Phase 2 (future): replace with full Tarjan SCC over the layer graph.

    One event is emitted per unique unordered pair {A, B}.
    """
    edge_set: Set[Tuple[str, str]] = {
        (e.get("source", ""), e.get("target", ""))
        for e in edges
        if isinstance(e, dict)
    }

    seen_pairs: Set[FrozenSet[str]] = set()
    events: List[ViolationEvent] = []

    for source, target in sorted(edge_set):
        if not source or not target:
            continue
        pair: FrozenSet[str] = frozenset({source, target})
        if pair in seen_pairs:
            continue
        if (target, source) in edge_set:
            seen_pairs.add(pair)
            a, b = sorted(pair)  # deterministic ordering for stable ID
            events.append(ViolationEvent(
                id=_make_event_id("IMPORT_CYCLE", a, b),
                type="IMPORT_CYCLE",
                severity="error",
                location=ViolationLocation(from_layer=a, to_layer=b),
                metrics=ViolationMetrics(),
                evidence=[EvidenceItem(
                    source="import_graph",
                    details={
                        "edges": [[source, target], [target, source]],
                        "note": "Phase 1: bidirectional pair only. "
                                "n-node cycles require Tarjan SCC (Phase 2).",
                    },
                )],
                strength="weak",
            ))

    return events
