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
#   Commit D — Шаг 5:    одиночные события нарушений
#   Commit E — Шаг 6:    многоисточниковые события
#   Commit F — Шаги 7–9: дедупликация, сводка, финальная сборка (текущий файл)
# ===================================================================================================

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from solid_dashboard.defaults import CC_THRESHOLD, LCOM4_THRESHOLD, DEAD_CODE_CONFIDENCE_CUTOFF
from solid_dashboard.schema import (
    AggregatedReport,
    AggregatedSummary,
    ClassMetrics,
    CohesionSummary,
    ComplexitySummary,
    DeadCodeEntry,
    DeadCodeSummary,
    EntitiesSection,
    EvidenceItem,
    FileMetrics,
    FunctionMetrics,
    ImportsSummary,
    LayerMetrics,
    MaintainabilitySummary,
    ReportMeta,
    ViolationEvent,
    ViolationLocation,
    ViolationMetrics,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# CC_THRESHOLD и DEAD_CODE_CONFIDENCE_CUTOFF импортированы из defaults.py
_ADAPTER_KEYS: Tuple[str, ...] = ("radon", "cohesion", "import_graph", "import_linter", "pyan3")
_SEVERITY_RANK: Dict[str, int] = {"error": 2, "warning": 1, "info": 0}


# ---------------------------------------------------------------------------
# Public entry point — fully wired (Steps 1–9)
# ---------------------------------------------------------------------------

def aggregate_results(context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Aggregates raw adapter results into a single structured report.

    Config keys consumed:
      cohesion_threshold (default 1), layers, utility_layers, layer_order, package_root.
    CC threshold = CC_THRESHOLD (from defaults.py = 10, NOT from config).
    Returns AggregatedReport-shaped dict; always valid regardless of missing adapters.
    Validate with: AggregatedReport.model_validate(result)
    """
    if config is None:
        config = {}

    config_defaults_used: bool = not bool(config)
    lcom4_threshold: int = int(config.get("cohesion_threshold", LCOM4_THRESHOLD))

    adapters_succeeded: List[str] = []
    adapters_failed: List[str] = []

    # Step 1 — normalize
    radon_fns, mi_files = _safe_normalize(
        "radon", context, _normalize_radon, adapters_succeeded, adapters_failed, default=([], []))
    cohesion_classes: List[ClassMetrics] = _safe_normalize(
        "cohesion", context, _normalize_cohesion, adapters_succeeded, adapters_failed, default=[])
    graph_layers, graph_edges, graph_violations = _safe_normalize(
        "import_graph", context, _normalize_import_graph, adapters_succeeded, adapters_failed,
        default=([], [], []))
    contract_violations: List[Dict[str, Any]] = _safe_normalize(
        "import_linter", context, _normalize_import_linter, adapters_succeeded, adapters_failed,
        default=[])
    _pyan3_nodes, dead_entries = _safe_normalize(
        "pyan3", context, _normalize_pyan3, adapters_succeeded, adapters_failed, default=([], []))

    lizard_used: bool = bool(
        isinstance(context.get("radon"), dict) and context["radon"].get("lizard_used", False))

    # Step 2 — indexes
    file_index: Dict[str, FileMetrics] = _build_file_index(radon_fns, mi_files, cohesion_classes)
    class_index: Dict[str, ClassMetrics] = _build_class_index(cohesion_classes)
    fn_index: Dict[str, FunctionMetrics] = _build_function_index(radon_fns)
    layer_index: Dict[str, LayerMetrics] = _build_layer_index(graph_layers)

    # Step 3 — cross-resolution
    fn_to_class: Dict[str, str] = _resolve_function_to_class(radon_fns, cohesion_classes)
    _attach_tier_to_layers(layer_index, config)
    module_to_layer_map: Dict[str, str] = _build_module_to_layer_map(config)

    # Step 4 — denormalize cross-metrics
    _attach_cross_metrics(fn_index, class_index, file_index, fn_to_class)

    # Step 5 — single-source events
    cc_events = _emit_cc_events(list(fn_index.values()), CC_THRESHOLD)
    mi_events = _emit_mi_events(list(file_index.values()))
    cohesion_events = _emit_cohesion_events(list(class_index.values()), lcom4_threshold)
    dead_events = _emit_dead_code_events(dead_entries)
    cycle_events = _detect_import_cycles(graph_edges)

    # Step 6 — multi-source merged events
    layer_events = _merge_layer_violations(
        graph_violations, contract_violations, layer_index, module_to_layer_map)
    overloaded_events = _emit_overloaded_class_events(
        class_index, fn_to_class, fn_index, CC_THRESHOLD, lcom4_threshold)

    raw_violations: List[ViolationEvent] = (
        cc_events + mi_events + cohesion_events + dead_events
        + cycle_events + layer_events + overloaded_events
    )

    # Step 7 — deduplicate and sort
    all_violations: List[ViolationEvent] = _deduplicate_violations(raw_violations)

    # Step 8 — compute summary
    # Pylance cannot narrow Optional types through ternary expressions in argument position.
    # Pre-compute typed Dict[str, Any] locals using isinstance guards on separate lines.
    _linter_val = context.get("import_linter")
    _pyan3_val = context.get("pyan3")
    linter_raw_summary: Dict[str, Any] = _linter_val if isinstance(_linter_val, dict) else {}
    pyan3_raw_summary: Dict[str, Any] = _pyan3_val if isinstance(_pyan3_val, dict) else {}

    summary = _compute_summary(
        fn_index=fn_index,
        file_index=file_index,
        class_index=class_index,
        layer_index=layer_index,
        violations=all_violations,
        dead_entries=dead_entries,
        lcom4_threshold=lcom4_threshold,
        linter_raw=linter_raw_summary,
        pyan3_raw=pyan3_raw_summary,
    )

    # Step 9 — assemble final report
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
        summary=summary,
        entities=entities,
        violations=all_violations,
        dead_code=sorted(dead_entries, key=lambda e: e.qualified_name),
    )
    return report.model_dump()


# ---------------------------------------------------------------------------
# Safe normalization
# ---------------------------------------------------------------------------

def _safe_normalize(key, context, normalize_fn, succeeded, failed, default):
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
    Returns True only when an adapter result signals a genuine crash.

    The sole discriminator is the presence of an "error" key.
    Both Pyan3Adapter._error() and ImportLinterAdapter._error_message()
    always include "error" when they crash, and never include it in their
    normal operating returns.

    IMPORTANT — is_success=False is intentionally NOT checked here:
      ImportLinterAdapter sets is_success=False when contracts are broken.
      This is a normal, expected state that carries populated violation_details
      — the very data the aggregator exists to consume. Treating it as an error
      would silently discard all LAYER_VIOLATION evidence (confirmed bug: T1).
      For Pyan3Adapter, is_success=False only ever appears together with an
      "error" key, so the "error" check alone is sufficient for both adapters.
    """
    if not isinstance(raw, dict):
        return True
    if "error" in raw:
        return True
    return False


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _normalize_radon(raw):
    fns = []
    for item in raw.get("items", []):
        fp, lineno, name = item.get("filepath", ""), item.get("lineno", 0), item.get("name", "")
        fns.append(FunctionMetrics(
            function_id=f"{fp}::{lineno}::{name}", filepath=fp, name=name,
            type=item.get("type", "function"), lineno=lineno,
            cc=item.get("complexity", 0), rank=item.get("rank", "A"),
            parameter_count=item.get("parameter_count"),
        ))
    mi_raw = raw.get("maintainability") or {}
    return fns, (mi_raw.get("files", []) if isinstance(mi_raw, dict) else [])


def _normalize_cohesion(raw):
    classes = []
    for record in raw.get("classes", []):
        raw_name, fp = record.get("name", ""), record.get("filepath", "")
        lcom4_val = record.get("cohesion_score")
        classes.append(ClassMetrics(
            class_id=f"{fp}::{raw_name}", filepath=fp, class_name=raw_name,
            lineno=record.get("lineno", 0), class_kind=record.get("class_kind", "concrete"),
            lcom4=float(lcom4_val) if lcom4_val is not None else None,
            lcom4_norm=record.get("cohesion_score_norm"),
            methods_count=record.get("methods_count", 0),
            excluded_from_aggregation=record.get("excluded_from_aggregation", False),
            label=raw_name,
        ))
    return classes


def _normalize_import_graph(raw):
    layers = []
    for node in raw.get("nodes", []):
        ln = node.get("id", "")
        layers.append(LayerMetrics(
            layer_id=ln, layer_name=ln, label=node.get("label", ln), tier=None,
            ca=node.get("ca", 0), ce=node.get("ce", 0),
            instability=node.get("instability", 0.0),
        ))
    return layers, raw.get("edges", []), raw.get("violations", [])


def _normalize_import_linter(raw):
    return raw.get("violation_details", [])


def _normalize_pyan3(raw):
    collision_rate = float(raw.get("collision_rate", 0.0))
    # используем DEAD_CODE_CONFIDENCE_CUTOFF из defaults.py вместо магического числа 0.35
    confidence = "low" if collision_rate >= DEAD_CODE_CONFIDENCE_CUTOFF else "high"
    dead = [DeadCodeEntry(dead_id=q, qualified_name=q, confidence=confidence)
            for q in raw.get("dead_nodes", [])]
    return raw.get("nodes", []), dead


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------

def _build_file_index(fns, mi_files, cohesion_classes):
    cc_by_file: Dict[str, List[int]] = defaultdict(list)
    for fn in fns:
        cc_by_file[fn.filepath].append(fn.cc)
    class_count: Dict[str, int] = defaultdict(int)
    for cls in cohesion_classes:
        class_count[cls.filepath] += 1
    mi_lookup = {r["filepath"]: r for r in mi_files if isinstance(r, dict) and "filepath" in r}
    index = {}
    for fp in sorted(set(cc_by_file) | set(mi_lookup) | set(class_count)):
        cc_list = cc_by_file.get(fp, [])
        mi_rec = mi_lookup.get(fp)
        index[fp] = FileMetrics(
            file_id=fp, filepath=fp,
            mi=float(mi_rec["mi"]) if mi_rec else None,
            mi_rank=mi_rec.get("rank") if mi_rec else None,
            function_count=len(cc_list),
            mean_cc=round(sum(cc_list) / len(cc_list), 2) if cc_list else 0.0,
            max_cc=max(cc_list) if cc_list else 0,
            high_cc_count=sum(1 for cc in cc_list if cc > CC_THRESHOLD),
            class_count=class_count.get(fp, 0),
        )
    return index


def _build_class_index(classes): return {c.class_id: c for c in classes}
def _build_function_index(fns): return {f.function_id: f for f in fns}
def _build_layer_index(layers): return {l.layer_name: l for l in layers}


# ---------------------------------------------------------------------------
# Step 3: cross-adapter resolution
# ---------------------------------------------------------------------------

def _resolve_function_to_class(fns, classes):
    by_file: Dict[str, List[ClassMetrics]] = defaultdict(list)
    for cls in classes:
        by_file[cls.filepath].append(cls)
    for fp in by_file:
        by_file[fp].sort(key=lambda c: c.lineno)
    result = {}
    for fn in fns:
        if fn.type != "method":
            continue
        file_cls = by_file.get(fn.filepath, [])
        if not file_cls:
            continue
        matched = None
        for i, cls in enumerate(file_cls):
            if cls.lineno > fn.lineno:
                break
            nxt = file_cls[i + 1].lineno if i + 1 < len(file_cls) else float("inf")
            if cls.lineno <= fn.lineno < nxt:
                matched = cls
        if matched:
            result[fn.function_id] = matched.class_id
    return result


def _resolve_tier_map(config):
    raw = config.get("layer_order")
    if not raw or not isinstance(raw, list):
        return None
    tier_map: Dict[str, int] = {}
    first = raw[0] if raw else None
    if isinstance(first, str):
        for i, n in enumerate(raw):
            if isinstance(n, str) and n.strip():
                tier_map[n.strip()] = i
    elif isinstance(first, list):
        for i, grp in enumerate(raw):
            if isinstance(grp, list):
                for n in grp:
                    if isinstance(n, str) and n.strip():
                        tier_map[n.strip()] = i
    else:
        return None
    return tier_map or None


def _attach_tier_to_layers(layer_index, config):
    tier_map = _resolve_tier_map(config)
    utility: Set[str] = set((config.get("utility_layers") or {}).keys())
    for ln, lm in layer_index.items():
        lm.is_utility_layer = ln in utility
        if tier_map and ln not in utility:
            lm.tier = tier_map.get(ln)


def _build_module_to_layer_map(config):
    root = config.get("package_root", "")
    prefix = f"{root}." if root else ""
    result: Dict[str, str] = {}
    for sec in ("layers", "utility_layers"):
        for ln, rv in (config.get(sec) or {}).items():
            paths = [rv] if isinstance(rv, str) else (
                [p for p in rv if isinstance(p, str)] if isinstance(rv, list) else [])
            for p in paths:
                p = p.strip()
                if not p:
                    continue
                norm = p if (not root or p == root or p.startswith(prefix)) else f"{root}.{p}"
                result[norm] = ln
    return result


def _resolve_module_to_layer(module, module_to_layer_map):
    best_len, best = 0, None
    for pfx, ln in module_to_layer_map.items():
        if (module == pfx or module.startswith(pfx + ".")) and len(pfx) > best_len:
            best_len, best = len(pfx), ln
    return best


# ---------------------------------------------------------------------------
# Step 4: cross-metric denormalization
# ---------------------------------------------------------------------------

def _attach_cross_metrics(fn_index, class_index, file_index, fn_to_class):
    for fn in fn_index.values():
        fm = file_index.get(fn.filepath)
        if fm:
            fn.file_mi = fm.mi
    cc_per_class: Dict[str, List[int]] = defaultdict(list)
    for fn_id, cid in fn_to_class.items():
        fn = fn_index.get(fn_id)
        cls = class_index.get(cid)
        if fn:
            fn.class_id = cid
        if fn and cls:
            fn.class_lcom4 = cls.lcom4
        if fn:
            cc_per_class[cid].append(fn.cc)
    for cls in class_index.values():
        fm = file_index.get(cls.filepath)
        if fm:
            cls.file_mi = fm.mi
        cc_list = cc_per_class.get(cls.class_id, [])
        if cc_list:
            cls.max_method_cc = max(cc_list)
            cls.mean_method_cc = round(sum(cc_list) / len(cc_list), 2)


# ---------------------------------------------------------------------------
# Step 5: single-source violation emitters
# ---------------------------------------------------------------------------

def _make_event_id(event_type: str, *key_parts: str) -> str:
    return "::".join([event_type] + list(key_parts))


def _emit_cc_events(fns: List[FunctionMetrics], cc_threshold: int) -> List[ViolationEvent]:
    """
    Emits HIGH_CC_METHOD events for functions/methods where CC strictly exceeds cc_threshold.

    Boundary invariant: cc == cc_threshold does NOT produce an event.
    Only cc > cc_threshold triggers a violation.

    Severity scale:
      cc > 15  -> error   (high cognitive load, refactoring required)
      cc > threshold (and <= 15) -> warning  (elevated complexity, worth reviewing)
    """
    events = []
    for fn in fns:
        # строго больше порога — равенство порогу нарушением не является
        if fn.cc <= cc_threshold:
            continue
        events.append(ViolationEvent(
            id=_make_event_id("HIGH_CC_METHOD", fn.filepath, str(fn.lineno), fn.name),
            type="HIGH_CC_METHOD",
            severity="error" if fn.cc > 15 else "warning",
            location=ViolationLocation(
                filepath=fn.filepath, lineno=fn.lineno, name=fn.name,
                class_name=fn.class_id.split("::")[-1] if fn.class_id else None,
            ),
            metrics=ViolationMetrics(cc=fn.cc, rank=fn.rank, parameter_count=fn.parameter_count),
            evidence=[EvidenceItem(source="radon",
                details={"complexity": fn.cc, "rank": fn.rank, "type": fn.type, "lineno": fn.lineno})],
            strength="weak",
        ))
    return events


def _emit_mi_events(files):
    events = []
    for f in files:
        if f.mi_rank not in ("B", "C"):
            continue
        events.append(ViolationEvent(
            id=_make_event_id("LOW_MI_FILE", f.filepath),
            type="LOW_MI_FILE",
            severity="error" if f.mi_rank == "C" else "warning",
            location=ViolationLocation(filepath=f.filepath),
            metrics=ViolationMetrics(mi=f.mi, rank=f.mi_rank),
            evidence=[EvidenceItem(source="radon",
                details={"mi": f.mi, "rank": f.mi_rank, "filepath": f.filepath})],
            strength="weak",
        ))
    return events


def _emit_cohesion_events(classes, lcom4_threshold):
    events = []
    for cls in classes:
        if cls.excluded_from_aggregation or cls.lcom4 is None or cls.lcom4 <= lcom4_threshold:
            continue
        events.append(ViolationEvent(
            id=_make_event_id("LOW_COHESION_CLASS", cls.filepath, cls.class_name),
            type="LOW_COHESION_CLASS",
            severity="error" if cls.lcom4 >= 3 else "warning",
            location=ViolationLocation(filepath=cls.filepath, lineno=cls.lineno, class_name=cls.class_name),
            metrics=ViolationMetrics(lcom4=cls.lcom4),
            evidence=[EvidenceItem(source="cohesion",
                details={"cohesion_score": cls.lcom4, "cohesion_score_norm": cls.lcom4_norm,
                         "methods_count": cls.methods_count, "class_kind": cls.class_kind})],
            strength="weak",
        ))
    return events


def _emit_dead_code_events(dead_entries):
    return [ViolationEvent(
        id=_make_event_id("DEAD_CODE_NODE", e.qualified_name),
        type="DEAD_CODE_NODE",
        severity="error" if e.confidence == "high" else "warning",
        location=ViolationLocation(filepath=e.filepath, name=e.qualified_name, layer=e.layer),
        metrics=ViolationMetrics(),
        evidence=[EvidenceItem(source="pyan3",
            details={"qualified_name": e.qualified_name, "confidence": e.confidence})],
        strength="weak",
    ) for e in dead_entries]


def _detect_import_cycles(edges):
    """
    Phase 1: bidirectional pair scan — SOLID_audit.md D3 known limitation.
    n-node cycles (A->B->C->A with no direct reversal) are silently missed.
    Phase 2 (future): Tarjan SCC over layer graph.
    """
    edge_set: Set[Tuple[str, str]] = {
        (e.get("source", ""), e.get("target", ""))
        for e in edges if isinstance(e, dict)
    }
    seen: Set[FrozenSet[str]] = set()
    events = []
    for source, target in sorted(edge_set):
        if not source or not target:
            continue
        pair: FrozenSet[str] = frozenset({source, target})
        if pair in seen:
            continue
        if (target, source) in edge_set:
            seen.add(pair)
            a, b = sorted(pair)
            events.append(ViolationEvent(
                id=_make_event_id("IMPORT_CYCLE", a, b),
                type="IMPORT_CYCLE", severity="error",
                location=ViolationLocation(from_layer=a, to_layer=b),
                metrics=ViolationMetrics(),
                evidence=[EvidenceItem(source="import_graph",
                    details={"edges": [[source, target], [target, source]],
                             "note": "Phase 1: bidirectional pair only. n-node cycles require Tarjan SCC (Phase 2)."})],
                strength="weak",
            ))
    return events


# ---------------------------------------------------------------------------
# Step 6: multi-source merged events
# ---------------------------------------------------------------------------

def _merge_layer_violations(graph_violations, contract_violations, layer_index, module_to_layer_map):
    """
    Merges ImportGraph + ImportLinter violations per (from_layer, to_layer) key.
    Updates LayerMetrics counters for _compute_summary.
    See SOLID_audit.md §3.3 Rule D1 for full merge semantics.
    """
    linter_bucket: Dict[Tuple[str, str], Dict[str, Any]] = {}
    graph_bucket: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for contract in contract_violations:
        contract_name = contract.get("contract_name", "")
        for imp in contract.get("broken_imports", []):
            importer, imported = imp.get("importer", ""), imp.get("imported", "")
            from_layer = _resolve_module_to_layer(importer, module_to_layer_map)
            to_layer = _resolve_module_to_layer(imported, module_to_layer_map)
            if not from_layer or not to_layer or from_layer == to_layer:
                continue
            key = (from_layer, to_layer)
            if key not in linter_bucket:
                linter_bucket[key] = {"contract_name": contract_name, "broken_imports": []}
            linter_bucket[key]["broken_imports"].append({"importer": importer, "imported": imported})
            if from_layer in layer_index:
                layer_index[from_layer].linter_broken_imports += 1

    for v in graph_violations:
        rule = v.get("rule", "")
        from_layer, to_layer = v.get("layer", ""), v.get("dependency", "")
        if not from_layer or not to_layer:
            continue
        key = (from_layer, to_layer)
        if key not in graph_bucket or rule == "SDP-001":
            graph_bucket[key] = {
                "rule": rule, "severity": v.get("severity", "error"),
                "instability": v.get("instability"), "dep_instability": v.get("dep_instability"),
                "skip_distance": v.get("skip_distance"), "tier": v.get("tier"), "dep_tier": v.get("dep_tier"),
            }
        if from_layer in layer_index:
            lm = layer_index[from_layer]
            if rule == "SDP-001":
                lm.sdp_violation_count += 1
            elif rule == "SLP-001":
                lm.slp_violation_count += 1

    events: List[ViolationEvent] = []
    for key in sorted(set(linter_bucket) | set(graph_bucket)):
        from_layer, to_layer = key
        linter_ev = linter_bucket.get(key)
        graph_ev = graph_bucket.get(key)

        evidence: List[EvidenceItem] = []
        if linter_ev:
            evidence.append(EvidenceItem(source="import_linter", details={
                "contract_name": linter_ev["contract_name"],
                "broken_imports_count": len(linter_ev["broken_imports"]),
                "broken_imports": linter_ev["broken_imports"],
            }))
        if graph_ev:
            evidence.append(EvidenceItem(source="import_graph",
                details={k: v for k, v in graph_ev.items() if v is not None}))

        both = linter_ev is not None and graph_ev is not None
        if linter_ev is not None:
            event_type = "LAYER_VIOLATION"
            graph_sev = graph_ev["severity"] if graph_ev else "error"
            severity = "error" if _SEVERITY_RANK.get(graph_sev, 0) >= _SEVERITY_RANK["warning"] else "warning"
        elif graph_ev is not None:
            event_type = "SDP_VIOLATION" if graph_ev["rule"] == "SDP-001" else "SLP_VIOLATION"
            severity = graph_ev["severity"]
        else:
            continue

        events.append(ViolationEvent(
            id=_make_event_id(event_type, from_layer, to_layer),
            type=event_type, severity=severity,
            location=ViolationLocation(from_layer=from_layer, to_layer=to_layer),
            metrics=ViolationMetrics(
                instability=graph_ev.get("instability") if graph_ev else None,
                dep_instability=graph_ev.get("dep_instability") if graph_ev else None,
                skip_distance=graph_ev.get("skip_distance") if graph_ev else None,
            ),
            evidence=evidence, strength="strong" if both else "weak",
        ))
    return events


def _emit_overloaded_class_events(class_index, fn_to_class, fn_index, cc_threshold, lcom4_threshold):
    """
    OVERLOADED_CLASS: Cohesion lcom4>threshold AND any method CC>threshold.
    strength=strong (both adapters). See SOLID_audit.md §3.3 Rule D2.
    """
    methods_by_class: Dict[str, List[FunctionMetrics]] = defaultdict(list)
    for fn_id, cid in fn_to_class.items():
        fn = fn_index.get(fn_id)
        if fn:
            methods_by_class[cid].append(fn)

    events = []
    for cid, cls in class_index.items():
        if cls.excluded_from_aggregation or cls.lcom4 is None or cls.lcom4 <= lcom4_threshold:
            continue
        high_cc = [m for m in methods_by_class.get(cid, []) if m.cc > cc_threshold]
        if not high_cc:
            continue
        top = max(high_cc, key=lambda m: m.cc)
        events.append(ViolationEvent(
            id=_make_event_id("OVERLOADED_CLASS", cls.filepath, cls.class_name),
            type="OVERLOADED_CLASS",
            severity="error" if cls.lcom4 >= 3 and top.cc > 15 else "warning",
            location=ViolationLocation(filepath=cls.filepath, lineno=cls.lineno, class_name=cls.class_name),
            metrics=ViolationMetrics(lcom4=cls.lcom4, cc=top.cc),
            evidence=[
                EvidenceItem(source="cohesion", details={
                    "cohesion_score": cls.lcom4, "cohesion_score_norm": cls.lcom4_norm,
                    "methods_count": cls.methods_count, "class_kind": cls.class_kind,
                }),
                EvidenceItem(source="radon", details={
                    "max_cc_method_name": top.name, "max_complexity": top.cc,
                    "max_cc_method_lineno": top.lineno, "mean_cc_in_class": cls.mean_method_cc,
                    "high_cc_methods_count": len(high_cc),
                }),
            ],
            strength="strong",
        ))
    return events


# ---------------------------------------------------------------------------
# Step 7: deduplication and sorting
# ---------------------------------------------------------------------------

def _deduplicate_violations(violations: List[ViolationEvent]) -> List[ViolationEvent]:
    """
    Deduplicates ViolationEvent list by event id.

    If two paths produce the same id (same type + same location key):
      - Evidence lists are merged (union by source)
      - Severity is set to the maximum of both
      - strength is set to "strong" if merged evidence has >=2 distinct sources

    Final list is sorted: severity DESC (error > warning > info), then type ASC.
    """
    merged: Dict[str, ViolationEvent] = {}

    for event in violations:
        existing = merged.get(event.id)
        if existing is None:
            merged[event.id] = event
        else:
            # объединяем evidence — добавляем источники, которых ещё нет
            existing_sources = {e.source for e in existing.evidence}
            for ev in event.evidence:
                if ev.source not in existing_sources:
                    existing.evidence.append(ev)
                    existing_sources.add(ev.source)

            # повышаем severity до максимального из двух событий
            if _SEVERITY_RANK.get(event.severity, 0) > _SEVERITY_RANK.get(existing.severity, 0):
                existing.severity = event.severity

            # если теперь несколько источников — strength становится strong
            if len(existing.evidence) >= 2:
                existing.strength = "strong"

    return sorted(
        merged.values(),
        key=lambda e: (-_SEVERITY_RANK.get(e.severity, 0), e.type),
    )


# ---------------------------------------------------------------------------
# Step 8: summary computation
# ---------------------------------------------------------------------------

def _compute_summary(
    fn_index: Dict[str, FunctionMetrics],
    file_index: Dict[str, FileMetrics],
    class_index: Dict[str, ClassMetrics],
    layer_index: Dict[str, LayerMetrics],
    violations: List[ViolationEvent],
    dead_entries: List[DeadCodeEntry],
    lcom4_threshold: int,
    linter_raw: Dict[str, Any],
    pyan3_raw: Dict[str, Any],
) -> AggregatedSummary:
    """
    Computes all summary sub-sections from entity indexes and violation list.
    linter_raw and pyan3_raw are used for adapter-level aggregates not captured in entities.
    """
    # --- Complexity ---
    all_cc = [fn.cc for fn in fn_index.values()]
    rank_dist_cc: Dict[str, int] = defaultdict(int)
    for fn in fn_index.values():
        rank_dist_cc[fn.rank] += 1
    complexity = ComplexitySummary(
        total_items=len(all_cc),
        mean_cc=round(sum(all_cc) / len(all_cc), 2) if all_cc else 0.0,
        # CC_THRESHOLD импортирован из defaults.py
        high_complexity_count=sum(1 for cc in all_cc if cc > CC_THRESHOLD),
        rank_distribution=dict(rank_dist_cc),
    )

    # --- Maintainability ---
    # Use a typed List[float] comprehension so Pylance can narrow Optional[float] -> float.
    # Pre-filtered lists of ClassMetrics/FileMetrics retain Optional types for Pylance
    # even after `if f.mi is not None`; a fresh comprehension with the same guard narrows.
    mi_values: List[float] = [f.mi for f in file_index.values() if f.mi is not None]
    mi_files = [f for f in file_index.values() if f.mi is not None]
    rank_dist_mi: Dict[str, int] = defaultdict(int)
    for f in mi_files:
        if f.mi_rank:
            rank_dist_mi[f.mi_rank] += 1
    maintainability = MaintainabilitySummary(
        total_files=len(mi_values),
        mean_mi=round(sum(mi_values) / len(mi_values), 2) if mi_values else 0.0,
        low_mi_count=sum(1 for f in mi_files if f.mi_rank == "C"),
        rank_distribution=dict(rank_dist_mi),
    )

    # --- Cohesion ---
    concrete = [c for c in class_index.values() if not c.excluded_from_aggregation]
    # Extract float LCOM4 values via typed comprehensions — same Pylance narrowing rule
    # as MI above: Optional[float] is only narrowed to float inside the same comprehension
    # that contains the `if c.lcom4 is not None` guard, not in a downstream generator.
    lcom4_all_vals: List[float] = [
        c.lcom4 for c in class_index.values()
        if not c.excluded_from_aggregation and c.lcom4 is not None
    ]
    lcom4_multi_vals: List[float] = [
        c.lcom4 for c in class_index.values()
        if not c.excluded_from_aggregation and c.lcom4 is not None and c.methods_count >= 2
    ]
    cohesion = CohesionSummary(
        total_classes_analyzed=len(class_index),
        concrete_classes_count=len(concrete),
        mean_lcom4_all=round(
            sum(lcom4_all_vals) / len(lcom4_all_vals), 2
        ) if lcom4_all_vals else 0.0,
        mean_lcom4_multi_method=round(
            sum(lcom4_multi_vals) / len(lcom4_multi_vals), 2
        ) if lcom4_multi_vals else 0.0,
        low_cohesion_count=sum(1 for v in lcom4_all_vals if v > lcom4_threshold),
        low_cohesion_threshold=lcom4_threshold,
    )

    # --- Imports ---
    sdp_count = sum(lm.sdp_violation_count for lm in layer_index.values())
    slp_count = sum(lm.slp_violation_count for lm in layer_index.values())
    cycle_count = sum(1 for v in violations if v.type == "IMPORT_CYCLE")
    imports = ImportsSummary(
        contracts_checked=int(linter_raw.get("contracts_checked", 0)),
        broken_contracts=int(linter_raw.get("broken_contracts", 0)),
        sdp_violations=sdp_count,
        slp_violations=slp_count,
        import_cycles=cycle_count,
    )

    # --- Dead code ---
    dead_code_summary = DeadCodeSummary(
        dead_node_count=len(dead_entries),
        high_confidence_dead=sum(1 for e in dead_entries if e.confidence == "high"),
        collision_rate=float(pyan3_raw.get("collision_rate", 0.0)),
    )

    # --- Violation counts ---
    strong_count = sum(1 for v in violations if v.strength == "strong")
    weak_count = len(violations) - strong_count

    return AggregatedSummary(
        complexity=complexity,
        maintainability=maintainability,
        cohesion=cohesion,
        imports=imports,
        dead_code=dead_code_summary,
        violations_total=len(violations),
        strong_violations=strong_count,
        weak_violations=weak_count,
    )
