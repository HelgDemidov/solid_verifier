from typing import Any, List, Dict, Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Radon: метрики цикломатической сложности (CC) — пофункционально
# ---------------------------------------------------------------------------

class RadonFunctionMetrics(BaseModel):
    name: str
    type: str
    complexity: int
    rank: str
    lineno: int
    filepath: str
    # опциональное поле из Lizard — отсутствует если lizard недоступен
    # или совпадение по lineno не найдено; включает self для методов
    parameter_count: Optional[int] = None


# ---------------------------------------------------------------------------
# Radon: Maintainability Index (MI) — поуфайлово
# ---------------------------------------------------------------------------

class MaintainabilityFileMetrics(BaseModel):
    filepath: str
    mi: float        # 0.0–100.0: A >= 20, B in [10,20), C < 10
    rank: str        # A / B / C


class MaintainabilityResult(BaseModel):
    total_files: int
    mean_mi: float
    low_mi_count: int        # количество файлов с rank C (MI < 10)
    files: List[MaintainabilityFileMetrics]  # отсортировано по mi ASC


# ---------------------------------------------------------------------------
# Radon: итоговый результат адаптера — зарезервировано для Report Aggregator
# ---------------------------------------------------------------------------

class RadonResult(BaseModel):
    total_items: int
    mean_cc: float
    high_complexity_count: int
    items: List[RadonFunctionMetrics]
    # maintainability — пустой dict {} если radon mi завершился с ошибкой
    maintainability: Optional[MaintainabilityResult] = None
    # lizard_used фиксирует факт обогащения parameter_count в текущем прогоне
    lizard_used: bool = False


# ---------------------------------------------------------------------------
# Cohesion
# ---------------------------------------------------------------------------

class CohesionClassMetrics(BaseModel):
    name: str
    methods_count: int
    cohesion_score: float


class CohesionResult(BaseModel):  # Зарезервировано для Report Aggregator
    total_classes_analyzed: int
    mean_cohesion: float
    low_cohesion_count: int
    classes: List[CohesionClassMetrics]


# ===========================================================================
# Report Aggregator models
# Используются исключительно в report_aggregator.py.
# Существующие модели выше не изменяются.
# ===========================================================================

# ---------------------------------------------------------------------------
# Summary sub-sections
# ---------------------------------------------------------------------------

class ComplexitySummary(BaseModel):
    total_items: int = 0
    mean_cc: float = 0.0
    high_complexity_count: int = 0       # CC > CC_THRESHOLD (see defaults.py)
    rank_distribution: Dict[str, int] = {}


class MaintainabilitySummary(BaseModel):
    total_files: int = 0
    mean_mi: float = 0.0
    low_mi_count: int = 0                # rank C (MI < 10)
    rank_distribution: Dict[str, int] = {}


class CohesionSummary(BaseModel):
    total_classes_analyzed: int = 0
    concrete_classes_count: int = 0
    mean_lcom4_all: float = 0.0
    mean_lcom4_multi_method: float = 0.0
    low_cohesion_count: int = 0
    low_cohesion_threshold: int = 1


class ImportsSummary(BaseModel):
    contracts_checked: int = 0
    broken_contracts: int = 0
    sdp_violations: int = 0
    slp_violations: int = 0
    import_cycles: int = 0               # bidirectional pairs (Phase 1 only)


class DeadCodeSummary(BaseModel):
    dead_node_count: int = 0
    high_confidence_dead: int = 0
    collision_rate: float = 0.0


class AggregatedSummary(BaseModel):
    complexity: ComplexitySummary = ComplexitySummary()
    maintainability: MaintainabilitySummary = MaintainabilitySummary()
    cohesion: CohesionSummary = CohesionSummary()
    imports: ImportsSummary = ImportsSummary()
    dead_code: DeadCodeSummary = DeadCodeSummary()
    violations_total: int = 0
    strong_violations: int = 0
    weak_violations: int = 0


# ---------------------------------------------------------------------------
# Entity models
# ---------------------------------------------------------------------------

class FileMetrics(BaseModel):
    file_id: str                              # stable ID = normalized filepath
    filepath: str
    mi: Optional[float] = None               # null if MI sub-call failed
    mi_rank: Optional[str] = None            # "A" | "B" | "C"
    function_count: int = 0
    mean_cc: float = 0.0
    max_cc: int = 0
    high_cc_count: int = 0                   # functions with CC > CC_THRESHOLD (see defaults.py)
    class_count: int = 0                     # from Cohesion classes in same file


class ClassMetrics(BaseModel):
    class_id: str                            # "<filepath>::<class_name>"
    filepath: str
    class_name: str                          # normalized from raw Cohesion field "name"
    lineno: int
    class_kind: str                          # "concrete"|"abstract"|"interface"|"dataclass"
    lcom4: Optional[float] = None            # null if Cohesion adapter failed/skipped
    lcom4_norm: Optional[float] = None       # 1/lcom4 if lcom4>1 else 1.0
    methods_count: int = 0
    excluded_from_aggregation: bool = False  # True for non-concrete kinds
    label: Optional[str] = None             # equals class_name; for rendering compatibility
    # denormalized cross-metrics (filled by _attach_cross_metrics)
    file_mi: Optional[float] = None
    max_method_cc: Optional[int] = None
    mean_method_cc: Optional[float] = None


class FunctionMetrics(BaseModel):
    function_id: str                         # "<filepath>::<lineno>::<name>"
    filepath: str
    name: str
    type: str                                # "function" | "method"
    lineno: int
    cc: int
    rank: str                                # radon rank A..F
    parameter_count: Optional[int] = None   # null if lizard_used=False
    class_id: Optional[str] = None          # set after _resolve_function_to_class()
    # denormalized cross-metrics (filled by _attach_cross_metrics)
    file_mi: Optional[float] = None
    class_lcom4: Optional[float] = None


class LayerMetrics(BaseModel):
    layer_id: str                            # equals layer_name
    layer_name: str
    label: str                               # from raw node["label"]; always == layer_name
    tier: Optional[int] = None               # null for utility_layers (crosscutting)
    ca: int = 0                              # afferent coupling
    ce: int = 0                              # efferent coupling
    instability: float = 0.0                 # ce / (ca + ce)
    sdp_violation_count: int = 0            # SDP-001 violations where this layer is source
    slp_violation_count: int = 0
    linter_broken_imports: int = 0          # ImportLinter broken_imports for this layer
    is_utility_layer: bool = False


# ---------------------------------------------------------------------------
# Violation models
# ---------------------------------------------------------------------------

class ViolationLocation(BaseModel):
    filepath: Optional[str] = None
    lineno: Optional[int] = None
    name: Optional[str] = None
    class_name: Optional[str] = None
    layer: Optional[str] = None
    from_layer: Optional[str] = None
    to_layer: Optional[str] = None


class ViolationMetrics(BaseModel):
    cc: Optional[int] = None
    rank: Optional[str] = None
    parameter_count: Optional[int] = None
    mi: Optional[float] = None
    lcom4: Optional[float] = None
    instability: Optional[float] = None
    dep_instability: Optional[float] = None
    skip_distance: Optional[int] = None


class EvidenceItem(BaseModel):
    source: str                              # "radon"|"cohesion"|"import_graph"|"import_linter"|"pyan3"
    details: Dict[str, Any]


class ViolationEvent(BaseModel):
    id: str                                  # "<TYPE>::<key_parts joined by ::>"
    type: str                                # HIGH_CC_METHOD | LOW_MI_FILE | ... (see SOLID_audit.md §3.1)
    severity: str                            # "info" | "warning" | "error"
    location: ViolationLocation
    metrics: ViolationMetrics = ViolationMetrics()
    evidence: List[EvidenceItem] = []
    strength: str                            # "strong" (>=2 adapters) | "weak" (1 adapter)


class DeadCodeEntry(BaseModel):
    dead_id: str                             # equals qualified_name
    qualified_name: str
    confidence: str                          # "high" | "low"
    filepath: Optional[str] = None          # inferred from module prefix if possible
    layer: Optional[str] = None             # resolved layer name if possible


# ---------------------------------------------------------------------------
# Top-level aggregated report
# ---------------------------------------------------------------------------

class EntitiesSection(BaseModel):
    files: List[FileMetrics] = []
    classes: List[ClassMetrics] = []
    functions: List[FunctionMetrics] = []
    layers: List[LayerMetrics] = []


class ReportMeta(BaseModel):
    generated_at: str
    adapter_versions_available: List[str] = []
    adapters_succeeded: List[str] = []
    adapters_failed: List[str] = []
    lizard_used: bool = False
    config_defaults_used: bool = False      # True when config={} or missing keys


class AggregatedReport(BaseModel):
    meta: ReportMeta
    summary: AggregatedSummary = AggregatedSummary()
    entities: EntitiesSection = EntitiesSection()
    violations: List[ViolationEvent] = []
    dead_code: List[DeadCodeEntry] = []
