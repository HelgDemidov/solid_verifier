# ===================================================================================================
# Report Aggregator (report_aggregator.py)
#
# Роль: агрегация и нормализация результатов всех статических адаптеров в единый отчет.
# Входные данные: context dict из pipeline.py + config dict.
# Выходные данные: AggregatedReport-совместимый dict (валидируется через schema.AggregatedReport).
#
# Этапы реализации:
#   Commit B — Шаги 1–2: нормализация + построение индексов
#   Commit C — Шаги 3–4: кросс-резолюция и денормализация метрик (текущий файл)
#   Commit D — Шаг 5:    одиночные события нарушений
#   Commit E — Шаг 6:    многоисточниковые события (LAYER_VIOLATION, OVERLOADED_CLASS)
#   Commit F — Шаги 7–9: дедупликация, сводка, финальная сборка
# ===================================================================================================

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from solid_dashboard.schema import (
    AggregatedReport,
    AggregatedSummary,
    ClassMetrics,
    DeadCodeEntry,
    EntitiesSection,
    FileMetrics,
    FunctionMetrics,
    LayerMetrics,
    ReportMeta,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# CC threshold mirrors the hardcoded value in radon_adapter.py: `if complexity > 10`
# NOT read from config — RadonAdapter has no cc_threshold config key.
CC_THRESHOLD: int = 10

# Adapter keys as they appear in the context dict populated by pipeline.py
_ADAPTER_KEYS: Tuple[str, ...] = ("radon", "cohesion", "import_graph", "import_linter", "pyan3")


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

    # 3a. Map function_id -> class_id via lineno-range matching
    fn_to_class: Dict[str, str] = _resolve_function_to_class(radon_fns, cohesion_classes)

    # 3b. Attach tier values + is_utility_layer flag to LayerMetrics
    _attach_tier_to_layers(layer_index, config)

    # 3c. Build module -> layer lookup (used by Commits E-F for import resolution)
    module_to_layer_map: Dict[str, str] = _build_module_to_layer_map(config)

    # -----------------------------------------------------------------------
    # Step 4 — Denormalize cross-metrics
    # -----------------------------------------------------------------------
    _attach_cross_metrics(fn_index, class_index, file_index, fn_to_class)

    # -----------------------------------------------------------------------
    # Steps 5–9 implemented in Commits D–F.
    # Expose unused variables to avoid linter warnings; consumed in later commits.
    # -----------------------------------------------------------------------
    _ = (lcom4_threshold, graph_edges, graph_violations, contract_violations,
         module_to_layer_map)

    # -----------------------------------------------------------------------
    # Assemble report (entities + meta; violations populated in Commits D–F)
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
        violations=[],                 # populated in Commits D–F
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
    Normalizes RadonAdapter output into:
      - FunctionMetrics list (one per function/method item)
      - raw MI file dicts (passed to _build_file_index; shape: {filepath, mi, rank})

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
            parameter_count=item.get("parameter_count"),  # None if Lizard not used
        ))

    mi_raw = raw.get("maintainability") or {}
    mi_files: List[Dict[str, Any]] = (
        mi_raw.get("files", []) if isinstance(mi_raw, dict) else []
    )

    return fns, mi_files


def _normalize_cohesion(raw: Dict[str, Any]) -> List[ClassMetrics]:
    """
    Normalizes CohesionAdapter output into ClassMetrics list.

    IMPORTANT — D1 correction (SOLID_audit.md):
      The raw CohesionAdapter field is "name", NOT "class_name".
      This normalizer reads record["name"] and maps it to class_name in ClassMetrics.

    class_id format: "<filepath>::<class_name>"
    """
    classes: List[ClassMetrics] = []

    for record in raw.get("classes", []):
        raw_name: str = record.get("name", "")      # raw field is "name" — see D1 correction
        fp: str = record.get("filepath", "")
        class_id: str = f"{fp}::{raw_name}"

        lcom4_val = record.get("cohesion_score")

        classes.append(ClassMetrics(
            class_id=class_id,
            filepath=fp,
            class_name=raw_name,                    # normalized: "name" -> class_name
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
    Normalizes ImportGraphAdapter output into:
      - LayerMetrics list (nodes with Ca/Ce/Instability)
      - raw edge list  [{source, target}] — passed through verbatim
      - raw violation list (SDP-001 / SLP-001 dicts) — passed through verbatim

    D2 correction (SOLID_audit.md):
      Raw node dict includes "label" field (always equals "id").
      LayerMetrics.label is populated from node["label"].

    tier is not set here; resolved in Step 3 via _attach_tier_to_layers().
    """
    layers: List[LayerMetrics] = []

    for node in raw.get("nodes", []):
        layer_name: str = node.get("id", "")

        layers.append(LayerMetrics(
            layer_id=layer_name,
            layer_name=layer_name,
            label=node.get("label", layer_name),    # "label" always equals id — see D2 correction
            tier=None,
            ca=node.get("ca", 0),
            ce=node.get("ce", 0),
            instability=node.get("instability", 0.0),
        ))

    edges: List[Dict[str, str]] = raw.get("edges", [])
    violations: List[Dict[str, Any]] = raw.get("violations", [])

    return layers, edges, violations


def _normalize_import_linter(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Normalizes ImportLinterAdapter output.

    Returns the violation_details list verbatim; each element has shape:
      {
        "contract_name": str,
        "status": "BROKEN",
        "broken_imports": [{"importer": str, "imported": str}, ...]
      }
    """
    return raw.get("violation_details", [])


def _normalize_pyan3(
    raw: Dict[str, Any],
) -> Tuple[List[str], List[DeadCodeEntry]]:
    """
    Normalizes Pyan3Adapter output into:
      - node list (qualified name strings, for future use)
      - DeadCodeEntry list

    Confidence assignment:
      Pyan3Adapter stores dead_nodes as a flat list (no per-node confidence field).
      Global confidence is derived from collision_rate:
        collision_rate >= 0.35  -> "low"  (parse quality suspect)
        collision_rate <  0.35  -> "high" (parse quality acceptable)
      Threshold 0.35 matches solid_config.json pyan3.collision_rate_threshold.
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

    nodes: List[str] = raw.get("nodes", [])
    return nodes, dead_entries


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------

def _build_file_index(
    fns: List[FunctionMetrics],
    mi_files: List[Dict[str, Any]],
    cohesion_classes: List[ClassMetrics],
) -> Dict[str, FileMetrics]:
    """
    Builds filepath -> FileMetrics index by aggregating:
      - per-file CC metrics from FunctionMetrics list
      - MI data from raw MI file dicts (shape: {filepath, mi, rank})
      - class count from ClassMetrics list
    """
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
    """
    Builds class_id -> ClassMetrics index.
    class_id format: "<filepath>::<class_name>"
    """
    return {cls.class_id: cls for cls in classes}


def _build_function_index(fns: List[FunctionMetrics]) -> Dict[str, FunctionMetrics]:
    """
    Builds function_id -> FunctionMetrics index.
    function_id format: "<filepath>::<lineno>::<name>"
    """
    return {fn.function_id: fn for fn in fns}


def _build_layer_index(layers: List[LayerMetrics]) -> Dict[str, LayerMetrics]:
    """
    Builds layer_name -> LayerMetrics index.
    """
    return {layer.layer_name: layer for layer in layers}


# ---------------------------------------------------------------------------
# Step 3 helpers: cross-adapter resolution
# ---------------------------------------------------------------------------

def _resolve_function_to_class(
    fns: List[FunctionMetrics],
    classes: List[ClassMetrics],
) -> Dict[str, str]:
    """
    Returns {function_id: class_id} for method-type functions, using lineno-range matching.

    Algorithm:
      For each file, sort classes by lineno ASC.
      A method at lineno L belongs to class C if:
        C.lineno <= L < next_class.lineno   (or C is the last class in the file)

    Only processes items with type="method"; standalone functions are skipped.
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
                # This class starts after the method — stop scanning
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
    Builds layer_name -> tier_index map from config["layer_order"].

    Supports two formats (identical to ImportGraphAdapter._resolve_tier_map):
      Format A — flat list:    ["routers", "services", "infrastructure", ...]
      Format B — grouped list: [["routers"], ["services", "infrastructure"], ...]

    Returns None if layer_order is absent or empty (fail-silent).
    """
    raw_order = config.get("layer_order")
    if not raw_order or not isinstance(raw_order, list):
        return None

    tier_map: Dict[str, int] = {}
    first = raw_order[0] if raw_order else None

    if isinstance(first, str):
        for tier_index, layer_name in enumerate(raw_order):
            if isinstance(layer_name, str) and layer_name.strip():
                tier_map[layer_name.strip()] = tier_index
    elif isinstance(first, list):
        for tier_index, group in enumerate(raw_order):
            if not isinstance(group, list):
                continue
            for layer_name in group:
                if isinstance(layer_name, str) and layer_name.strip():
                    tier_map[layer_name.strip()] = tier_index
    else:
        return None

    return tier_map if tier_map else None


def _attach_tier_to_layers(
    layer_index: Dict[str, LayerMetrics],
    config: Dict[str, Any],
) -> None:
    """
    Mutates LayerMetrics objects in place:
      - Sets tier from config layer_order (None if layer not in tier_map)
      - Sets is_utility_layer=True for layers listed in config utility_layers

    utility_layers intentionally have no tier (crosscutting; no SDP/SLP checks).
    """
    tier_map = _resolve_tier_map(config)

    utility_names: Set[str] = set(
        (config.get("utility_layers") or {}).keys()
    )

    for layer_name, layer_m in layer_index.items():
        layer_m.is_utility_layer = layer_name in utility_names
        if tier_map is not None and layer_name not in utility_names:
            layer_m.tier = tier_map.get(layer_name)  # None if not in ordered set


def _build_module_to_layer_map(config: Dict[str, Any]) -> Dict[str, str]:
    """
    Builds a fully-qualified module prefix -> layer_name lookup dict.

    Normalizes both config["layers"] and config["utility_layers"] prefixes
    using config["package_root"] (same logic as ImportGraphAdapter._normalize_layer_config).

    Example with package_root="app":
      {"layers": {"routers": ["routers"]}} -> {"app.routers": "routers"}

    Used in Commits E–F to resolve fully-qualified module names
    (e.g. "app.routers.search") to their layer names (e.g. "routers").
    """
    package_root: str = config.get("package_root", "")
    package_prefix: str = f"{package_root}." if package_root else ""

    result: Dict[str, str] = {}

    for section_key in ("layers", "utility_layers"):
        layer_section: Dict[str, Any] = config.get(section_key) or {}

        for layer_name, raw_value in layer_section.items():
            if isinstance(raw_value, str):
                paths = [raw_value]
            elif isinstance(raw_value, list):
                paths = [p for p in raw_value if isinstance(p, str)]
            else:
                continue

            for path in paths:
                cleaned = path.strip()
                if not cleaned:
                    continue
                # Normalize to full module path (same as ImportGraphAdapter)
                if package_root and not (
                    cleaned == package_root or cleaned.startswith(package_prefix)
                ):
                    normalized = f"{package_root}.{cleaned}"
                else:
                    normalized = cleaned
                result[normalized] = layer_name

    return result


def _resolve_module_to_layer(
    module: str,
    module_to_layer_map: Dict[str, str],
) -> Optional[str]:
    """
    Resolves a fully-qualified module name to its layer name using longest-prefix match.

    Example:
      "app.routers.search" with map {"app.routers": "routers"} -> "routers"
      "app.routers"        with map {"app.routers": "routers"} -> "routers"
      "external.lib"       with map {"app.routers": "routers"} -> None
    """
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

    Populated fields:
      FunctionMetrics.file_mi    <- FileMetrics.mi for same filepath
      FunctionMetrics.class_lcom4 <- ClassMetrics.lcom4 for owning class
      FunctionMetrics.class_id   <- set from fn_to_class resolution
      ClassMetrics.file_mi       <- FileMetrics.mi for same filepath
      ClassMetrics.max_method_cc <- max CC across all methods in the class
      ClassMetrics.mean_method_cc <- mean CC across all methods in the class
    """
    # Attach file_mi to functions
    for fn in fn_index.values():
        file_m = file_index.get(fn.filepath)
        if file_m is not None:
            fn.file_mi = file_m.mi

    # Attach class_id and class_lcom4 to methods
    for fn_id, class_id in fn_to_class.items():
        fn = fn_index.get(fn_id)
        cls = class_index.get(class_id)
        if fn is not None:
            fn.class_id = class_id
        if fn is not None and cls is not None:
            fn.class_lcom4 = cls.lcom4

    # Attach file_mi to classes
    for cls in class_index.values():
        file_m = file_index.get(cls.filepath)
        if file_m is not None:
            cls.file_mi = file_m.mi

    # Compute max/mean method CC per class from fn_to_class mapping
    cc_per_class: Dict[str, List[int]] = defaultdict(list)
    for fn_id, class_id in fn_to_class.items():
        fn = fn_index.get(fn_id)
        if fn is not None:
            cc_per_class[class_id].append(fn.cc)

    for class_id, cc_list in cc_per_class.items():
        cls = class_index.get(class_id)
        if cls is not None and cc_list:
            cls.max_method_cc = max(cc_list)
            cls.mean_method_cc = round(sum(cc_list) / len(cc_list), 2)
