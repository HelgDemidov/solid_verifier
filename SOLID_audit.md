# SOLID Verifier — Static Adapter Audit & Report Aggregator Design

> Source of truth: branch `refactor/heuristics-modularization`, commit state as read.  
> Primary: `adapters/*_adapter.py` + `schema.py`. Secondary: `ARCHITECTURE.md`.  
> **No code is generated or modified.**

---

## Preliminary note: schema.py drift

`schema.py` defines Pydantic models for `RadonResult` / `CohesionResult` but they lag behind the actual `run()` return values:

| Schema.py model | Drift vs. actual `run()` output |
|---|---|
| `CohesionResult.mean_cohesion` | Adapter returns `mean_cohesion_all` + `mean_cohesion_multi_method` |
| `CohesionResult.classes[*]` items | Missing `cohesion_score_norm`, `lineno`, `filepath`, `class_kind`, `excluded_from_aggregation` |
| `PydepsResult` | Dead artifact — no adapter uses it |
| ImportGraph, ImportLinter, Pyan3 | No Pydantic schemas exist at all |

The schemas below are derived **from the actual `run()` implementation**, not from `schema.py`.

---

## 1) Adapter Schemas

### 1.1 RadonAdapter

Two independent subprocess calls: `radon cc --json` (per-function CC) and `radon mi --json` (per-file MI). Optional Lizard enrichment for `parameter_count`. Sorted: items by complexity DESC, MI files by mi ASC.

**Top-level return dict:**

```jsonc
{
  "total_items": 42,            // int — count of function+method records
  "mean_cc": 3.14,              // float — mean cyclomatic complexity
  "high_complexity_count": 5,   // int — items with CC > 10
  "lizard_used": true,          // bool — Lizard enriched parameter_count in this run
  "items": [ /* RadonFunctionMetrics */ ],
  "maintainability": { /* MaintainabilityResult | {} on MI failure */ }
}
```

**`items[]` element:**

```jsonc
{
  "name": "create_search_query",  // str — function or method name
  "type": "method",               // str — "function" | "method"
  "complexity": 12,               // int — cyclomatic complexity
  "rank": "C",                    // str — radon rank A..F
  "lineno": 47,                   // int — declaration line
  "filepath": "app/services/search_service.py",  // str — absolute or relative
  "parameter_count": 4            // Optional[int] — from Lizard; null if unavailable
}
```

**`maintainability` sub-object (or `{}` on failure):**

```jsonc
{
  "total_files": 18,
  "mean_mi": 62.4,
  "low_mi_count": 2,     // int — files with rank C (MI < 10)
  "files": [
    { "filepath": "app/services/search_service.py", "mi": 8.3, "rank": "C" }
  ]
}
```

**Primary keys for matching:**

| Scope | Key | Joins with |
|---|---|---|
| Per function/method | `(filepath, lineno, name)` | Pyan3 nodes (via qualified name), Cohesion methods |
| Per file (MI) | `filepath` | Cohesion classes, ImportGraph modules |

---

### 1.2 CohesionAdapter

Two-pass AST analysis: Pass 1 collects class structure + attributes; Pass 2 enriches with ancestor `self.xxx` via MRO traversal. LCOM4 = number of disconnected components in method-attribute graph. Aggregates only over `class_kind == "concrete"`.

**Top-level return dict:**

```jsonc
{
  "total_classes_analyzed": 24,        // int — all kinds combined
  "concrete_classes_count": 17,        // int — concrete only, used in aggregates
  "mean_cohesion_all": 1.35,           // float — mean LCOM4 for concrete classes (≥1 method)
  "mean_cohesion_multi_method": 1.58,  // float — mean LCOM4 for concrete classes (≥2 methods)
  "analyzed_classes_count": 14,        // int — concrete multi-method classes
  "low_cohesion_count": 4,             // int — concrete classes with LCOM4 > threshold
  "low_cohesion_excluded_count": 1,    // int — non-concrete classes above threshold
  "low_cohesion_excluded_classes": [ /* brief records */ ],
  "low_cohesion_threshold": 1,         // int — from config.cohesion_threshold
  "classes": [ /* CohesionClassRecord */ ]
}
```

**`classes[]` element:**

```jsonc
{
  "name": "SearchService",             // str — class name
  "methods_count": 6,                  // int — non-empty methods counted for LCOM4
  "cohesion_score": 2.0,               // float — LCOM4 value (1.0 = fully cohesive)
  "cohesion_score_norm": 0.5,          // float — 1/LCOM4 if LCOM4>1 else 1.0
  "filepath": "app/services/search_service.py",  // str
  "lineno": 12,                        // int — class declaration line
  "class_kind": "concrete",            // str — "concrete"|"abstract"|"interface"|"dataclass"
  "excluded_from_aggregation": false   // bool — true for non-concrete
}
```

**Note:** `schema.py`'s `CohesionResult` is outdated — it lacks `filepath`, `lineno`, `class_kind`, `excluded_from_aggregation`, `cohesion_score_norm`, and the two separate mean fields.

**Primary keys for matching:**

| Scope | Key | Joins with |
|---|---|---|
| Per class | `(filepath, name)` | Radon items (methods in same file), heuristics findings |
| Per file | `filepath` | Radon MI files |

---

### 1.3 ImportGraphAdapter

Uses `grimp` to build a full import graph, maps physical modules → logical layers, calculates Martin stability metrics (Ca, Ce, I). Detects SDP-001 (Stable Dependencies Principle) and SLP-001 (Skip-Layer Principle) violations.

**Top-level return dict:**

```jsonc
{
  "nodes": [ /* LayerNode */ ],
  "edges": [ /* LayerEdge */ ],
  "violations": [ /* ViolationSDP | ViolationSLP */ ],
  "debug_info": { "package": "app", "total_modules": 47, ... }
}
```

**`nodes[]` element:**

```jsonc
{
  "id": "services",          // str — logical layer name (from solid_config.json)
  "ca": 2,                   // int — afferent coupling (layers depending on this)
  "ce": 3,                   // int — efferent coupling (layers this depends on)
  "instability": 0.6         // float — ce / (ca + ce); 0.0 = stable, 1.0 = unstable
}
```

**`edges[]` element:**

```jsonc
{ "source": "routers", "target": "services" }   // layer-level dependency
```

**SDP-001 violation:**

```jsonc
{
  "rule": "SDP-001",
  "layer": "services",          // str — source layer
  "instability": 0.8,           // float — I(source)
  "dependency": "models",       // str — target layer
  "dep_instability": 0.2,       // float — I(target)
  "severity": "error",
  "message": "...",
  "evidence": []                // reserved (to be filled by pipeline orchestrator)
}
```

**SLP-001 violation:**

```jsonc
{
  "rule": "SLP-001",
  "layer": "routers",
  "tier": 0,
  "dependency": "models",
  "dep_tier": 4,
  "skip_distance": 3,           // int — number of tiers skipped
  "severity": "error",          // "error" | "warning"
  "message": "...",
  "evidence": [1, 2, 3]         // List[int] — skipped tier indices
}
```

**Primary keys for matching:**

| Scope | Key | Joins with |
|---|---|---|
| Per layer (node) | `layer_name` (= `id`) | ImportLinter contracts (same layer names) |
| Per dependency edge | `(source, target)` | ImportLinter `broken_imports` (map module → layer) |

---

### 1.4 ImportLinterAdapter

Generates a temporary `.importlinter_auto_<pkg>` config from the base `.importlinter`, runs `lint-imports` CLI, parses text output (ANSI-stripped). Produces two representations: flat `violations: List[str]` for backward compat, and structured `violation_details` for aggregation.

**Top-level return dict:**

```jsonc
{
  "is_success": false,          // bool — returncode == 0
  "contracts_checked": 1,       // int
  "broken_contracts": 1,        // int
  "kept_contracts": 0,          // int
  "violations": [               // List[str] — contract names only (backward compat)
    "Scopus API layered architecture"
  ],
  "violation_details": [ /* ContractViolation */ ],
  "raw_output": "..."           // str — ANSI-cleaned CLI output
}
```

**`violation_details[]` element:**

```jsonc
{
  "contract_name": "Scopus API layered architecture",
  "status": "BROKEN",
  "broken_imports": [
    {
      "importer": "app.routers.search",  // str — fully qualified module
      "imported": "app.models.paper"     // str — fully qualified module
    }
  ]
}
```

**Primary keys for matching:**

| Scope | Key | Joins with |
|---|---|---|
| Per contract | `contract_name` | — (single contract per project) |
| Per import pair | `(importer, imported)` → resolve to `(from_layer, to_layer)` | ImportGraph edges + nodes |

---

### 1.5 Pyan3Adapter

Two-pass pyan3 text output parsing. Pass 1 detects name-collision blocks (`suspicious_blocks`). Pass 2 parses nodes + edges with `confidence` ("high"/"low"). Dead nodes = no incoming AND no outgoing edges. Root nodes = no incoming but has outgoing.

**Top-level return dict:**

```jsonc
{
  "is_success": true,
  "node_count": 150,
  "edge_count": 312,
  "edge_count_high": 280,
  "edge_count_low": 32,
  "nodes": ["app.services.search_service.SearchService.run", ...],  // List[str] sorted
  "edges": [ /* CallEdge */ ],
  "dead_node_count": 7,
  "dead_nodes": ["app.utils.legacy.old_format", ...],               // List[str] sorted
  "root_node_count": 12,
  "root_nodes": ["app.routers.search.search_papers", ...],          // List[str] sorted
  "suspicious_blocks": ["login", "create", ...],                    // List[str] sorted
  "collision_rate": 0.08,     // float — suspicious_blocks / total_nodes
  "raw_output": "..."
}
```

**`edges[]` element:**

```jsonc
{
  "from": "app.services.search_service.SearchService.run",  // str — qualified name
  "to": "app.infrastructure.db.SessionFactory.get_session", // str — qualified name
  "confidence": "high"   // "high" | "low"
}
```

**Primary keys for matching:**

| Scope | Key | Joins with |
|---|---|---|
| Per node | `qualified_name` (e.g. `app.services.X.method`) | Radon items (partial: file prefix + method name), ImportGraph layers (module prefix) |
| Per dead node | `qualified_name` | ImportGraph modules with Ca=0 |

---

## 2) Overlaps and Synergy

### 2.1 Comparative table

| | **Unit of analysis** | **Metrics** | **Anomaly form** | **Aggregation levels** |
|---|---|---|---|---|
| **RadonAdapter** | function, method, file | CC, rank, MI, MI.rank | implicit: CC>10, rank F/E/D; MI rank C | per-function, per-file, global mean |
| **CohesionAdapter** | class, file | LCOM4, class_kind, methods_count | implicit: LCOM4 > threshold | per-class (concrete), global means |
| **ImportGraphAdapter** | layer, edge (layer→layer) | Ca, Ce, Instability, tier | explicit violations: SDP-001, SLP-001 | per-layer, global (edge set) |
| **ImportLinterAdapter** | contract, edge (module→module) | — (binary: kept/broken) | explicit: BROKEN contract + broken_imports | per-contract, per import pair |
| **Pyan3Adapter** | function/method node, call edge | confidence | explicit: dead_nodes; implicit: suspicious_blocks | per-node, global counts |

---

### 2.2 Concrete overlaps

#### Overlap A — Overloaded / low-quality classes

**Phenomenon:** A class has methods with high CC (RadonAdapter) AND the class itself has low cohesion (CohesionAdapter). These are complementary signals about the same class.

| Adapter | Fields used | Matching key |
|---|---|---|
| RadonAdapter | `items[type=="method"].{filepath, lineno, name, complexity, rank}` | `filepath` → narrow to class via lineno proximity |
| CohesionAdapter | `classes[class_kind=="concrete"].{filepath, name, cohesion_score, methods_count}` | `(filepath, name)` |

**Joining strategy:** For a Cohesion record `(filepath, class_name)`, find all Radon `items` with `type="method"` and `filepath == filepath`. Since Radon does not store `class_name`, the class must be resolved by lineno range (class lineno ≤ method lineno ≤ next class lineno). The report aggregator must build this range index.

**Which adapter provides what:**
- Radon: raw per-method metrics (CC number, rank, parameter_count).
- Cohesion: class-level interpretation (LCOM4, kind classification, cohesion_score_norm).

---

#### Overlap B — Layered architecture violations / forbidden imports

**Phenomenon:** A dependency between two layers/modules is both flagged as a contract violation (ImportLinterAdapter) and exists as a real edge in the import graph (ImportGraphAdapter). These are the same architectural breach seen from two angles.

| Adapter | Fields used | Matching key |
|---|---|---|
| ImportLinterAdapter | `violation_details[].broken_imports[].{importer, imported}` | module → layer mapping |
| ImportGraphAdapter | `edges[].{source, target}` | `(source_layer, target_layer)` |

**Joining strategy:** Both adapters use the same `layer_order` from `solid_config.json`. A broken import `(app.routers.search → app.models.paper)` maps to layer pair `(routers → models)`. This pair must exist in ImportGraph's `edges`. Additionally, the SDP/SLP violation records from ImportGraph for the same `(layer, dependency)` pair enrich the merged event with `instability`, `tier`, `skip_distance` data.

**Which adapter provides what:**
- ImportLinterAdapter: contract name, concrete import pairs (strongest evidence — actual `import` statement paths).
- ImportGraphAdapter: stability metrics (Ca, Ce, I), tier violation type (SDP/SLP), violation distance.

---

#### Overlap C — Dead / unreachable code

**Phenomenon:** A node has no callers in the call graph (Pyan3Adapter `dead_nodes`); a module has no importers (ImportGraphAdapter `ca == 0`). Both signal potentially removable code.

| Adapter | Fields used | Matching key |
|---|---|---|
| Pyan3Adapter | `dead_nodes[]` (qualified node names) | module prefix of qualified name |
| ImportGraphAdapter | `nodes[ca==0]` (layers with no afferent coupling) | `layer_name` |

**Joining strategy:** A Pyan3 dead node `app.utils.legacy.old_format` → module prefix is `app.utils` → resolve to layer (e.g., `core`). This is a weak signal overlap: Pyan3 operates at function granularity, ImportGraph at layer granularity. A Ca=0 layer is a much coarser signal. The aggregator should treat these as separate event types but may attach them as corroborating evidence when the layer of a dead node also has Ca=0.

**Which adapter provides what:**
- Pyan3: precise node identity (qualified name), confidence level.
- ImportGraph: layer-level isolation (no consumers at all).

---

## 3) Architectural Events and Deduplication

### 3.1 Event catalog

| # | Event type | Human name | Source adapter(s) | Primary key | Severity default |
|---|---|---|---|---|---|
| E1 | `HIGH_CC_METHOD` | High cyclomatic complexity | Radon (`items`, CC > threshold) | `(filepath, lineno, name)` | warning (CC 11–15), error (CC > 15) |
| E2 | `LOW_MI_FILE` | Low maintainability index | Radon (`maintainability.files`, rank C) | `filepath` | warning (rank B), error (rank C) |
| E3 | `LOW_COHESION_CLASS` | Low cohesion class | Cohesion (`classes`, LCOM4 > threshold, class_kind==concrete) | `(filepath, class_name)` | warning (LCOM4 = 2), error (LCOM4 ≥ 3) |
| E4 | `LAYER_VIOLATION` | Architecture layer violation | ImportLinter (`violation_details`) + ImportGraph (`violations` SDP/SLP, `edges`) | `(from_layer, to_layer)` | error (BROKEN + SDP), warning (SLP-warning) |
| E5 | `IMPORT_CYCLE` | Import cycle detected | ImportGraph (bidirectional edges: `(A→B)` and `(B→A)` both present) | `frozenset({layer_a, layer_b})` | error |
| E6 | `DEAD_CODE_NODE` | Dead code node | Pyan3 (`dead_nodes`) | `qualified_name` | warning (low-confidence node), error (high-confidence node) |
| E7 | `OVERLOADED_CLASS` | Overloaded class (CC + LCOM4) | Radon + Cohesion (joined on filepath/lineno range) | `(filepath, class_name)` | error (both signals present), warning (one signal) |
| E8 | `SDP_VIOLATION` | Stable Dependencies violation | ImportGraph (`violations`, rule=SDP-001) | `(from_layer, to_layer)` | error |
| E9 | `SLP_VIOLATION` | Skip-layer violation | ImportGraph (`violations`, rule=SLP-001) | `(from_layer, to_layer, skip_distance)` | per adapter (`severity` field) |
| E10 | `HIGH_PARAMETER_COUNT` | Too many parameters (ISP signal) | Radon (`items`, `parameter_count ≥ threshold`, requires lizard_used=true) | `(filepath, lineno, name)` | warning |

**Note on E4 vs E8/E9:** `LAYER_VIOLATION` (E4) is the merged cross-adapter event combining ImportLinter evidence (contract broken, specific import pairs) with ImportGraph SDP/SLP evidence for the same layer pair. `SDP_VIOLATION` (E8) and `SLP_VIOLATION` (E9) are single-source ImportGraph events emitted when ImportLinter has no corresponding contract or when only ImportGraph fires.

---

### 3.2 ViolationEvent structure

```jsonc
{
  "id": "HIGH_CC_METHOD::app/services/search_service.py::47::create_search_query",
  // format: "<type>::<key_parts_joined_by_::>"

  "type": "HIGH_CC_METHOD",     // str — one of the event types above
  "severity": "warning",        // "info" | "warning" | "error"

  "location": {
    "filepath": "app/services/search_service.py",
    "lineno": 47,               // Optional[int]
    "name": "create_search_query",  // Optional[str]
    "class_name": null,         // Optional[str]
    "layer": null,              // Optional[str]
    "from_layer": null,         // Optional[str]
    "to_layer": null            // Optional[str]
  },

  "metrics": {
    // type-specific numeric fields; all Optional
    "cc": 12,
    "rank": "C",
    "parameter_count": 5,
    "mi": null,
    "lcom4": null,
    "instability": null,
    "dep_instability": null,
    "skip_distance": null
  },

  "evidence": [
    {
      "source": "radon",        // adapter name
      "details": {              // raw fields from adapter output verbatim
        "complexity": 12,
        "rank": "C",
        "type": "method"
      }
    }
    // second entry added if another adapter corroborates
  ],

  "strength": "strong"          // "strong" (≥2 adapters) | "weak" (1 adapter)
}
```

---

### 3.3 Deduplication rules

#### Rule D1 — LAYER_VIOLATION merge (ImportLinter + ImportGraph)

**Trigger:** An ImportLinter `broken_import (importer → imported)` resolves to layer pair `(L_from, L_to)`, AND ImportGraph has an edge `{source: L_from, target: L_to}` AND/OR a violation with `layer == L_from` and `dependency == L_to`.

**Action:**
1. Create a single `ViolationEvent` with `type = "LAYER_VIOLATION"` and key `(L_from, L_to)`.
2. `evidence[0]` = ImportLinter source with `details.contract_name`, `details.broken_imports`.
3. `evidence[1]` = ImportGraph source with `details.rule` (SDP-001/SLP-001), `details.instability`, `details.dep_instability` (or `details.skip_distance`, `details.severity`).
4. `strength = "strong"`.
5. Severity = max("error" from linter BROKEN + ImportGraph severity).

**If only ImportLinter fires (no ImportGraph edge):** emit as single-source, `strength = "weak"`.
**If only ImportGraph fires (no linter contract):** emit as `SDP_VIOLATION` or `SLP_VIOLATION`, `strength = "weak"`.

---

#### Rule D2 — OVERLOADED_CLASS merge (Radon + Cohesion)

**Trigger:** CohesionAdapter emits a `LOW_COHESION_CLASS` candidate, and for the same `(filepath, class_lineno_range)` there exists at least one Radon item with `type="method"`, `complexity > threshold`.

**Action:**
1. Create a single `ViolationEvent` with `type = "OVERLOADED_CLASS"`.
2. `evidence[0]` = Cohesion source with `details.cohesion_score`, `details.methods_count`, `details.class_kind`.
3. `evidence[1]` = Radon source with `details.max_cc_method_name`, `details.max_complexity`, `details.mean_cc_in_class`.
4. `strength = "strong"`.
5. If LCOM4 ≥ 3 AND max CC > 15 → `severity = "error"`, otherwise `"warning"`.

---

#### Rule D3 — Severity for single-source events

| Condition | strength | severity |
|---|---|---|
| ≥2 adapters agree on same location/key | `"strong"` | take the max of individual severities |
| 1 adapter, metric clearly above threshold | `"weak"` | "warning" |
| 1 adapter, metric marginally above threshold | `"weak"` | "info" |

---

#### Rule D4 — IMPORT_CYCLE detection

ImportGraph `edges` contains cycle `A→B` and `B→A` (both edges present). These are not emitted as violations by the adapter itself. The aggregator must detect them by scanning the edge set for bidirectional pairs.

---

## 4) Proposed Aggregated Report Schema

### 4.1 Top-level structure

```jsonc
{
  "meta": {
    "generated_at": "2026-04-08T21:56:00",
    "adapter_versions_available": ["radon", "cohesion", "import_graph", "import_linter", "pyan3"],
    "adapters_succeeded": ["radon", "cohesion", "import_graph", "import_linter", "pyan3"],
    "adapters_failed": [],      // list of adapter keys where run() returned error
    "lizard_used": true
  },

  "summary": {
    "complexity": {
      "total_items": 42,
      "mean_cc": 3.14,
      "high_complexity_count": 5,    // CC > 10
      "rank_distribution": {"A": 30, "B": 7, "C": 3, "D": 2, "E": 0, "F": 0}
    },
    "maintainability": {
      "total_files": 18,
      "mean_mi": 62.4,
      "low_mi_count": 2,            // rank C (MI < 10)
      "rank_distribution": {"A": 14, "B": 2, "C": 2}
    },
    "cohesion": {
      "total_classes_analyzed": 24,
      "concrete_classes_count": 17,
      "mean_lcom4_all": 1.35,
      "mean_lcom4_multi_method": 1.58,
      "low_cohesion_count": 4,
      "low_cohesion_threshold": 1
    },
    "imports": {
      "contracts_checked": 1,
      "broken_contracts": 1,
      "sdp_violations": 2,
      "slp_violations": 3,
      "import_cycles": 1
    },
    "dead_code": {
      "dead_node_count": 7,
      "high_confidence_dead": 5,
      "collision_rate": 0.08
    },
    "violations_total": 18,
    "strong_violations": 4,
    "weak_violations": 14
  },

  "entities": {
    "files":     [ /* FileMetrics */ ],
    "classes":   [ /* ClassMetrics */ ],
    "functions": [ /* FunctionMetrics */ ],
    "layers":    [ /* LayerMetrics */ ]
  },

  "violations": [ /* ViolationEvent — see §3.2 */ ],

  "dead_code":  [ /* DeadCodeEntry */ ]
}
```

---

### 4.2 Entity models

#### FileMetrics

```jsonc
{
  "file_id": "app/services/search_service.py",  // str — stable ID == filepath (normalized)
  "filepath": "app/services/search_service.py",
  "mi": 8.3,                   // Optional[float] — null if MI adapter failed
  "mi_rank": "C",              // Optional[str]
  "function_count": 5,         // int — count of items in Radon for this file
  "mean_cc": 7.2,              // float — mean CC of all functions in file
  "max_cc": 12,                // int — max CC in file
  "high_cc_count": 1,          // int — functions with CC > 10
  "class_count": 2             // int — from Cohesion
}
```

#### ClassMetrics

```jsonc
{
  "class_id": "app/services/search_service.py::SearchService",
  // str — "<filepath>::<class_name>" — stable across runs

  "filepath": "app/services/search_service.py",
  "class_name": "SearchService",
  "lineno": 12,
  "class_kind": "concrete",    // "concrete"|"abstract"|"interface"|"dataclass"
  "lcom4": 2.0,                // Optional[float] — null if Cohesion failed
  "lcom4_norm": 0.5,           // Optional[float]
  "methods_count": 6,
  "excluded_from_aggregation": false,
  "file_mi": 8.3,              // Optional[float] — denormalized for LLM context
  "max_method_cc": 12,         // Optional[int] — denormalized from Radon
  "mean_method_cc": 4.8        // Optional[float]
}
```

#### FunctionMetrics

```jsonc
{
  "function_id": "app/services/search_service.py::47::create_search_query",
  // str — "<filepath>::<lineno>::<name>" — stable

  "filepath": "app/services/search_service.py",
  "name": "create_search_query",
  "type": "method",
  "lineno": 47,
  "cc": 12,
  "rank": "C",
  "parameter_count": 4,        // Optional[int] — null if Lizard not used
  "class_id": "app/services/search_service.py::SearchService",  // Optional[str]
  "file_mi": 8.3,              // Optional[float] — denormalized
  "class_lcom4": 2.0           // Optional[float] — denormalized
}
```

#### LayerMetrics

```jsonc
{
  "layer_id": "services",      // str — stable ID == layer name from config
  "layer_name": "services",
  "tier": 1,                   // Optional[int] — from tier_map; null for utility_layers
  "ca": 2,
  "ce": 3,
  "instability": 0.6,
  "sdp_violation_count": 1,    // int — SDP-001 violations where this is source
  "slp_violation_count": 0,
  "linter_broken_imports": 3,  // int — broken_imports where importer starts with this layer
  "is_utility_layer": false    // bool
}
```

#### DeadCodeEntry

```jsonc
{
  "dead_id": "app.utils.legacy.old_format",
  "qualified_name": "app.utils.legacy.old_format",
  "confidence": "high",        // "high" | "low"
  "filepath": null,            // Optional[str] — inferred from module prefix if possible
  "layer": "core"              // Optional[str] — resolved layer if possible
}
```

---

### 4.3 LLM context requirements

For `_build_context()` to enrich a candidate, the following IDs must be stable and unique:

| ID field | Format | Usage |
|---|---|---|
| `file_id` | normalized `filepath` | look up `FileMetrics` |
| `class_id` | `"<filepath>::<class_name>"` | look up `ClassMetrics` |
| `function_id` | `"<filepath>::<lineno>::<name>"` | look up `FunctionMetrics` |
| `layer_id` | `layer_name` from config | look up `LayerMetrics` |

**Minimal LLM-visible fields by entity type:**

| For... | Must include | Purpose |
|---|---|---|
| Function/method | `cc`, `rank`, `parameter_count`, `file_mi`, `class_lcom4` | OCP-H-004, ISP signal |
| Class | `lcom4`, `lcom4_norm`, `methods_count`, `class_kind`, `file_mi` | LOW_COHESION, class role |
| Layer | `ca`, `ce`, `instability`, `sdp_violation_count`, `linter_broken_imports` | SDP/SLP context |

---

## 5) Report_aggregator Implementation Plan

### 5.1 Module location and interface

**File:** `tools/solid_verifier/solid_dashboard/report_aggregator.py`

**Public API:**

```python
def aggregate_results(context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Entry point. Consumes context dict with keys:
      "radon", "cohesion", "import_graph", "import_linter", "pyan3"
    All keys are optional — absent or error-containing values degrade gracefully.
    Returns the aggregated report dict matching the schema in §4.
    """
```

**Helper functions:**

```python
def _normalize_radon(raw: Dict) -> Tuple[List[FunctionMetrics], Optional[MIResult]]: ...
def _normalize_cohesion(raw: Dict) -> List[ClassMetrics]: ...
def _normalize_import_graph(raw: Dict) -> Tuple[List[LayerMetrics], List[LayerEdge], List[GraphViolation]]: ...
def _normalize_import_linter(raw: Dict) -> List[ContractViolation]: ...
def _normalize_pyan3(raw: Dict) -> Tuple[List[NodeRecord], List[DeadCodeEntry]]: ...

def _build_file_index(fns: List[FunctionMetrics], mi_files: List[MIRecord]) -> Dict[str, FileMetrics]: ...
def _build_class_index(classes: List[ClassMetrics]) -> Dict[str, ClassMetrics]: ...
def _build_function_index(fns: List[FunctionMetrics]) -> Dict[str, FunctionMetrics]: ...
def _build_layer_index(layers: List[LayerMetrics]) -> Dict[str, LayerMetrics]: ...

def _resolve_function_to_class(
    fns: List[FunctionMetrics],
    classes: List[ClassMetrics]
) -> Dict[str, str]: ...
# Returns {function_id: class_id} using lineno-range matching

def _resolve_module_to_layer(module: str, layer_config: Dict) -> Optional[str]: ...

def _merge_layer_violations(
    graph_violations: List[GraphViolation],
    contract_violations: List[ContractViolation],
    layer_index: Dict[str, LayerMetrics],
    module_to_layer: Callable[[str], Optional[str]]
) -> List[ViolationEvent]: ...

def _emit_cc_events(fns: List[FunctionMetrics], cc_threshold: int) -> List[ViolationEvent]: ...
def _emit_mi_events(files: List[FileMetrics]) -> List[ViolationEvent]: ...
def _emit_cohesion_events(classes: List[ClassMetrics], threshold: int) -> List[ViolationEvent]: ...
def _emit_dead_code_events(dead_entries: List[DeadCodeEntry]) -> List[ViolationEvent]: ...
def _detect_import_cycles(edges: List[LayerEdge]) -> List[ViolationEvent]: ...

def _attach_cross_metrics(
    functions: Dict[str, FunctionMetrics],
    classes: Dict[str, ClassMetrics],
    files: Dict[str, FileMetrics],
    fn_to_class: Dict[str, str]
) -> None: ...
# Denormalizes file_mi → FunctionMetrics, class_lcom4 → FunctionMetrics, etc.

def _emit_overloaded_class_events(
    class_index: Dict[str, ClassMetrics],
    fn_to_class: Dict[str, str],
    fn_index: Dict[str, FunctionMetrics],
    cc_threshold: int,
    lcom4_threshold: int
) -> List[ViolationEvent]: ...

def _compute_summary(
    files: Dict[str, FileMetrics],
    classes: Dict[str, ClassMetrics],
    layers: Dict[str, LayerMetrics],
    violations: List[ViolationEvent],
    dead_code: List[DeadCodeEntry],
    fns: Dict[str, FunctionMetrics]
) -> Dict[str, Any]: ...

def _make_event_id(event_type: str, *key_parts: str) -> str: ...
# e.g., "HIGH_CC_METHOD::app/services/X.py::47::create_query"

def _is_error_result(raw: Dict) -> bool: ...
# True if adapter returned {"error": ...} or is_success=False
```

---

### 5.2 Algorithm steps

```
aggregate_results(context):

Step 1 — Guard and normalize raw adapter outputs
  for each adapter key in ["radon", "cohesion", "import_graph", "import_linter", "pyan3"]:
    raw = context.get(key) or {}
    if _is_error_result(raw): mark adapter as failed; skip normalization
    else: call _normalize_<adapter>(raw) → typed structures

Step 2 — Build entity indexes
  file_index     = _build_file_index(functions, mi_files)
  class_index    = _build_class_index(cohesion_classes)
  fn_index       = _build_function_index(radon_functions)
  layer_index    = _build_layer_index(graph_nodes)

Step 3 — Cross-adapter resolution
  fn_to_class = _resolve_function_to_class(radon_functions, cohesion_classes)
    // for each (filepath, lineno) in radon functions:
    // find class in same file where class.lineno <= fn.lineno < next_class.lineno
    // → {fn_id: class_id}

  module_to_layer = partial(_resolve_module_to_layer, layer_config=config["layers"])
    // "app.routers.search" → "routers"

Step 4 — Denormalize cross-metrics
  _attach_cross_metrics(fn_index, class_index, file_index, fn_to_class)
    // fn.file_mi ← file_index[fn.filepath].mi
    // fn.class_lcom4 ← class_index[fn_to_class[fn.function_id]].lcom4
    // class.file_mi ← file_index[class.filepath].mi
    // class.max_method_cc ← max CC among functions in fn_to_class[class_id]

Step 5 — Emit single-source ViolationEvent protos
  cc_events       = _emit_cc_events(radon_functions, cc_threshold=10)
  mi_events       = _emit_mi_events(files with rank C)
  cohesion_events = _emit_cohesion_events(cohesion_classes, lcom4_threshold)
  dead_events     = _emit_dead_code_events(pyan3_dead_nodes)
  cycle_events    = _detect_import_cycles(graph_edges)
    // bidirectional scan: for (A,B) in edges, if (B,A) also in edges → IMPORT_CYCLE

Step 6 — Merge multi-source events
  layer_violations = _merge_layer_violations(
      graph_violations,      // SDP-001 + SLP-001 from ImportGraph
      contract_violations,   // BROKEN contracts from ImportLinter
      layer_index,
      module_to_layer
  )
    // Dedup key: (from_layer, to_layer)
    // For each key:
    //   if ImportLinter fired: evidence[0] = linter details
    //   if ImportGraph fired: evidence[1] = graph details (rule, instability, skip_distance)
    //   strength = "strong" if both fired, else "weak"
    //   severity = max of both adapter severities

  overloaded_events = _emit_overloaded_class_events(
      class_index, fn_to_class, fn_index, cc_threshold, lcom4_threshold
  )
    // For each class with lcom4 > threshold AND at least one method CC > threshold:
    //   evidence[0] = cohesion, evidence[1] = radon
    //   strength = "strong"

Step 7 — Deduplicate and assign IDs
  all_events = cc_events + mi_events + cohesion_events + dead_events +
               cycle_events + layer_violations + overloaded_events
  for each event: event.id = _make_event_id(event.type, *event_key_parts)
  deduplicate by id (keep one, merge evidence if duplicate id appears from different paths)

Step 8 — Compute summary
  summary = _compute_summary(file_index, class_index, layer_index,
                              all_events, pyan3_dead_entries, fn_index)

Step 9 — Assemble final report
  return {
    "meta": { adapters_succeeded, adapters_failed, generated_at, lizard_used },
    "summary": summary,
    "entities": {
      "files":     sorted(file_index.values(), key=filepath),
      "classes":   sorted(class_index.values(), key=class_id),
      "functions": sorted(fn_index.values(), key=function_id),
      "layers":    sorted(layer_index.values(), key=layer_name)
    },
    "violations": sorted(all_events, key=(severity_rank DESC, type)),
    "dead_code":  sorted(pyan3_dead_entries, key=qualified_name)
  }
```

---

### 5.3 Graceful degradation contract

| Missing adapter | Effect on report |
|---|---|
| `radon` absent/failed | `entities.functions = []`, CC fields in summary = 0, no HIGH_CC/LOW_MI events, no cross-metrics for CC |
| `cohesion` absent/failed | `entities.classes = []`, cohesion fields in summary = 0, no LOW_COHESION events, no OVERLOADED events |
| `import_graph` absent/failed | `entities.layers = []`, import summary = 0, no SDP/SLP events, LAYER_VIOLATION loses graph evidence |
| `import_linter` absent/failed | No BROKEN contract events, LAYER_VIOLATION (if ImportGraph fired) demoted to SDP/SLP, strength = "weak" |
| `pyan3` absent/failed | `dead_code = []`, no DEAD_CODE_NODE events |
| `lizard_used = false` | `parameter_count = null` everywhere, no HIGH_PARAMETER_COUNT events |

All cases: `meta.adapters_failed` records which adapters were skipped. Report structure is always valid — missing sections are empty lists/dicts, never absent keys.

---

### 5.4 Integration test scenarios

#### Test T1 — LAYER_VIOLATION dedup: two adapters → one event

**Setup context:**
```python
context["import_graph"]["violations"] = [
    {"rule": "SDP-001", "layer": "routers", "dependency": "models",
     "instability": 1.0, "dep_instability": 0.2, "severity": "error", ...}
]
context["import_linter"]["violation_details"] = [
    {"contract_name": "Scopus API layered architecture", "status": "BROKEN",
     "broken_imports": [{"importer": "app.routers.search", "imported": "app.models.paper"}]}
]
```

**Assert:**
- `report["violations"]` contains exactly **one** event with `type="LAYER_VIOLATION"` and key `(routers, models)`.
- `event.evidence` has length **2** — sources `"import_linter"` and `"import_graph"`.
- `event.strength == "strong"`.
- `event.severity == "error"`.

---

#### Test T2 — Cross-metric denormalization: LCOM4 on function record

**Setup context (minimal):**
- Cohesion: class `SearchService` at `app/services/search_service.py:12`, LCOM4=2.
- Radon: method `create_query` at `app/services/search_service.py:47`, CC=12.

**Assert:**
- `fn_index["app/services/search_service.py::47::create_query"].class_lcom4 == 2.0`
- `class_index["app/services/search_service.py::SearchService"].max_method_cc == 12`
- An `OVERLOADED_CLASS` event is emitted with `strength="strong"`.

---

#### Test T3 — Graceful degradation: pyan3 missing

**Setup context:** exclude `"pyan3"` key entirely.

**Assert:**
- `report["dead_code"] == []`
- `report["summary"]["dead_code"]["dead_node_count"] == 0`
- `report["meta"]["adapters_failed"]` contains `"pyan3"`
- No `KeyError` raised.
- All other sections populated normally.

---

#### Test T4 — HIGH_CC_METHOD event

**Setup:** Radon item with `complexity=16`, `rank="F"`.

**Assert:**
- `ViolationEvent` with `type="HIGH_CC_METHOD"`, `severity="error"`, `metrics.cc == 16`.
- `evidence[0].source == "radon"`.
- `strength == "weak"` (single source).

---

#### Test T5 — DEAD_CODE_NODE severity by confidence

**Setup:** Pyan3 dead_nodes contain both a `high`-confidence and a `low`-confidence node.

**Assert:**
- High-confidence dead node → `severity="error"`.
- Low-confidence dead node → `severity="warning"`.
- Both appear in `report["dead_code"]`.

---

#### Test T6 — IMPORT_CYCLE detection

**Setup:** ImportGraph edges contain both `{"source": "services", "target": "infrastructure"}` and `{"source": "infrastructure", "target": "services"}`.

**Assert:**
- `report["violations"]` contains an event with `type="IMPORT_CYCLE"`.
- `event.location.from_layer` and `event.location.to_layer` form the cycle pair.
- `event.severity == "error"`.
