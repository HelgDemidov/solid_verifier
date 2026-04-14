# SOLID Verifier Dashboard

Russian version: [README.ru.md](README.ru.md)

## Introduction

The idea for this code analyzer grew out of a paradox of modern AI-assisted development: while building my first educational project, *Scopus Search Code*, the speed of code generation began to outpace my ability to fully understand the growing graph of dependencies inside the project. My analytical background pushed me to regain control over the system as it evolved. I wanted a tool that could solve two problems at once: serve as a strict independent verifier of architectural correctness and also act as a readable map of the hidden connections and dependency paths inside the codebase.

That is how the concept of the SOLID Verifier emerged. For object-oriented Python, SOLID remains one of the most mature and universal frameworks for reasoning about architectural quality. 

***

`solid_dashboard` is a config-driven CLI tool that analyzes Python projects for adherence to SOLID principles and layered architecture. It runs a pipeline of static analyzers, computes metrics, checks architectural contracts, and — optionally — deepens OCP/LSP analysis with an LLM layer. Results from all static adapters are aggregated by `report_aggregator.py` into a single structured report (`aggregated_report`) and written into the machine-readable file `solid_report.log` (JSON format).

The tool is project-agnostic: it can be reused across different Python codebases.

## Key Features

- **Single CLI entry point** and full orchestration via `pipeline.py` for running the entire analysis pipeline against a target project.
- **Strict isolation and configuration-driven behavior** (`solid_config.json`):
  - **Single point of directory control:** the target directory (`package_root`) and exclusions (`ignore_dirs`) are defined in one place and strictly respected by all static and LLM adapters. The pipeline is safely isolated from `.venv`, tests, and utility scripts.
  - Define architectural layers and their module prefixes.
  - Define external "library layers" (database, web frameworks, etc.).
  - Secure secret management (LLM keys are automatically loaded from a `.env` file, keeping the JSON config clean).
- **Static metrics**:
  - **Complexity and maintainability** via `radon`: Cyclomatic Complexity (CC) per function/method with rank A–F, and Maintainability Index (MI, 0–100) per file with rank A–C — aggregated with `total_files`, `mean_mi`, and `low_mi_count` (files ranked C).
  - **Cohesion** via a custom **LCOM4** adapter based on Python's built-in `ast` (with smart filtering of properties and utility methods to eliminate false positives).
  - **Call graph and dead code** via `pyan3` (fully safe execution with no global environment state mutations, a two-pass name-collision detector, and confidence labelling of edges).
- **Architecture and dependencies**:
  - **Import graph** based on `grimp`.
  - **Layered architecture contracts** enforced through the `import-linter` CLI (via on-the-fly dynamic generation of temporary contract files).
  - Martin stability metrics (`Ca`, `Ce`, `Instability`) for each layer.
- **LLM-based OCP/LSP analysis** (implemented, optionally enabled through `solid_config.json`):
  - AST heuristics identify potential OCP/LSP violations and build a list of candidates.
  - `LlmSolidAdapter` sends each candidate to an LLM for verification (via OpenRouter).
  - A two-level Anti-Corruption Layer (ACL-A + ACL-B) protects the pipeline from malformed model responses: ACL-B distinguishes `success`, `partial`, and `failure` statuses per candidate and records `parse_failures`, `parse_partials`, and `parse_warnings` in run metadata.
  - Heuristic signals from `HeuristicsAdapter` are forwarded into the LLM prompt via the `{findings}` placeholder in `user_base.md`: for each candidate, only the signals belonging to that specific class are included; when no signals exist for a class, an explicit fallback text is substituted. The LLM receives them as supplementary context and may override them.
- **Extensible pipeline** through a clear `IAnalyzer` interface explicitly implemented by all static adapters.
- **Result aggregation and deduplication** via `report_aggregator.py`: results from all five static adapters are normalized into a unified schema (Pydantic models in `schema.py`), enriched with cross-adapter metrics, deduplicated by event ID with severity upgraded to the maximum, and stored in `results["aggregated_report"]` and `context["aggregated_report"]`. Existing context keys are not overwritten (backward compatibility). Aggregator failure is isolated: the pipeline continues and returns `{"error": ...}` in the `aggregated_report` field.
- **Machine-readable report** in `solid_report.log` (JSON) and planned visual HTML dashboards.

***

## Current Architecture and Adapters

The verifier is deliberately built around division of labor and minimization of nondeterminism. We do not use an LLM where deterministic analysis already does the job better.

- Three out of five SOLID principles — **SRP**, **ISP**, and **DIP** — are covered effectively and with 100% reliability by deterministic static analysis: LCOM4 and complexity metrics reveal overloaded objects, while import graphs strictly track dependency inversion. Static analysis is transparent, instant, and not vulnerable to hallucinations, model drift, or network failures.
- The more expensive **heuristics + LLM** path is applied selectively, only for **OCP** (Open/Closed Principle) and **LSP** (Liskov Substitution Principle). Only these two principles require semantic understanding of architectural contracts and business logic, so the model is engaged strictly at the nodes that static heuristics explicitly point to.

### Static Adapters

At the static-analysis level, the dashboard is implemented as an internal framework of **adapters** orchestrated by a central `pipeline.py` module. Every static adapter strictly obeys the single point of directory control (`package_root` and `ignore_dirs` from `solid_config.json`):

- Inherits from the common `IAnalyzer` interface (`solid_dashboard.interfaces.analyzer.IAnalyzer`).
- Exposes a `name` property.
- Implements `run(target_dir, context, config) -> Dict[str, Any]`.

The LLM layer is implemented separately from `IAnalyzer` and is invoked directly by `pipeline.py` based on heuristic results.

- **`radon_adapter.py`**  
  Runs two independent `radon` subprocesses against the target directory, both respecting `ignore_dirs` via the `-i` flag (empty strings and whitespace-only entries are filtered before the command is assembled).

  **`radon cc --json`** — produces a normalized flat list of records per function/method: name, type (`function`/`method`), cyclomatic complexity, rank (A–F), line number, and file path. Items are sorted by complexity descending.
  **`radon mi --json`** — produces a per-file Maintainability Index report (MI score 0–100, rank A/B/C). Aggregated into a `maintainability` sub-object with `total_files`, `mean_mi`, `low_mi_count` (files ranked C, i.e. MI < 10), and a `files` list sorted by MI ascending (worst first). **MI failure is fully isolated:** any subprocess or parse error returns an empty `{}` for `maintainability` and never interrupts the CC result.

  Additionally, the adapter integrates `lizard` solely to retrieve `parameter_count` — the number of parameters per function or method (including `self` for methods), which is not available in standard radon output. Metrics computed by both tools (e.g., CC) are intentionally deduplicated, with radon taking precedence. Normalization of `parameter_count` is deliberately deferred to the aggregation layer. In `report_aggregator.py`, the fact that lizard was used is recorded in `ReportMeta.lizard_used` based on the `context["radon"]["lizard_used"]` flag.

- **`cohesion_adapter.py`**  
  A custom, dependency-free implementation of the **LCOM4** (Lack of Cohesion of Methods 4) metric built entirely on Python's built-in `ast`. The adapter consists of two collaborating components: `CohesionAdapter` (the main adapter implementing `IAnalyzer`) and the helper `class_classifier.py` (semantic class classification). The algorithm operates in two passes:
  - **Pass 1:** recursively traverses the target directory respecting `ignore_dirs`, parses every file through `ast`, and builds `ClassInfo` and `MethodInfo` for every class while constructing a global definition index.
  - **Pass 2:** enriches every class with attributes declared in ancestor `__init__` methods by recursively traversing the MRO chain through the index; when attributes are extended, `_MethodUsageVisitor` is re-run to update the connectivity graph.

  The connectivity graph is built from two types of edges: shared instance attributes (`self.field`) and inter-method calls (`self.method()`, `super().method()`). Strategically excluded from the graph: `__init__`, `@property` methods, and empty stubs (`pass` / `...` / `raise NotImplementedError`). The `class_classifier.py` module assigns each class a semantic role (`concrete`, `abstract`, `interface`, `dataclass`) — aggregate metrics (`mean_cohesion_all`, `mean_cohesion_multi_method`, `low_cohesion_count`) are computed **only** for `concrete` classes so that interfaces and dataclass models do not distort the overall cohesion picture. 
  
  Name-collision resolution when the same class name appears in multiple files is handled by a "same-file wins" priority; when ambiguity cannot be resolved, the adapter degrades gracefully with logging. The adapter is fully covered by tests in the `tests/static_adapters/test_cohesion_adapter/` package.

- **`import_graph_adapter.py`**  
  Builds an architectural import graph using `grimp`, lifting the level of analysis from individual modules to the layers defined in `solid_config.json` (`layers`, `utility_layers`, `external_layers`, `layer_order`). Computes Martin's stability metrics (`Ca`, `Ce`, `Instability`) for each layer and produces a layer-level graph that includes external "library layers". Supports crosscutting `utility_layers` that participate in metrics and visualisation but are deliberately excluded from SDP/SLP checks to avoid false violations. 
  
  Implements a Stable Dependencies Principle detector (`SDP-001`) with a configurable `sdp_tolerance` and an explicit `allowed_dependency_exceptions` list. Implements a Skip-Layer Principle detector (`SLP-001`) that flags direct dependencies skipping one or more tiers from `layer_order`; violation severity is tuned by `interface_layers` semantics. Returns all violations in a unified `violations` structure and exposes `debug_info` for each, making the adapter ready for cross-adapter correlation in the Report Aggregator.

- **`import_linter_adapter.py`**  
  Enforces layered-architecture contracts using the `lint-imports` CLI tool, invoked in an isolated `subprocess`. Before each run, the adapter **dynamically generates a temporary config file** (`.importlinter_auto_*`): it reads the base `.importlinter` via `configparser`, writes `root_packages` from `package_root`, and synchronizes the layer order from `layer_order` — the single source of truth. The temporary file is unconditionally removed in a `finally` block after the run.

  Raw `lint-imports` output is stripped of ANSI escape codes; contract statistics (`kept`/`broken`) are extracted via regex with a `returncode`-based fallback. The result exposes two violation representations: `violations` (a flat list of contract names — kept for backward compatibility) and `violation_details` (a structured `List[Dict]` with fields `contract_name`, `status`, `broken_imports: [{importer, imported}]` — ready for future cross-adapter aggregation). The adapter implements `IAnalyzer` and returns a consistent 7-field schema regardless of execution outcome.

- **`pyan3_adapter.py`**  
  Uses `pyan3` to build a static call graph and identify potentially unused code. The key architectural decision is running `pyan3` via `subprocess` with the `cwd` parameter — without `os.chdir` — keeping the adapter stateless and project-agnostic. Python files are collected manually with `ignore_dirs` filtering, excluding `.venv`, tests, and tooling directories. The adapter implements a **two-pass parsing model** over `pyan3`'s text output: the first pass detects blocks with name collisions (identified by duplicate `[U]`-entries within a block before deduplication); the second pass builds the graph and assigns a `"high"` / `"low"` confidence label to each edge. 
  
  Confidence is determined solely by the source block: an edge receives `"low"` if its source is marked as suspicious; the target node has no effect on confidence. Nodes are categorised into three groups: `root_nodes` (no incoming edges, has outgoing — entry points), `dead_nodes` (no edges at all — genuinely unused code), and normally connected nodes. In the aggregator, `confidence` for `dead_nodes` is derived from the `collision_rate` field in the raw adapter output: when `collision_rate ≥ 0.35` all dead nodes are marked `confidence="low"`; below that threshold they receive `confidence="high"`.

### Report Aggregator

The `report_aggregator.py` module is the final stage of the static pipeline. It is called by `pipeline.py` **after** all static adapters have run and **before** the LLM layer. Public entry point: `aggregate_results(context, config)`.

**Architecture: 9 sequential steps**

| Step | Name | What happens |
|------|------|--------------|
| 1 | Normalize | Each adapter is normalized by a dedicated `_normalize_*` handler into typed Pydantic models (`FunctionMetrics`, `ClassMetrics`, `LayerMetrics`, `DeadCodeEntry`). If an adapter returned `{"error": ...}` or `None`, it is added to `adapters_failed` and steps 2–9 continue uninterrupted (graceful degradation). **Important:** `import_linter` is marked failed **only** when the `"error"` key is present, **not** when `is_success=False` — a broken contract is a normal operating state that carries violation data for aggregation. |
| 2 | Index | Four lookup indexes are built: `file_index`, `class_index`, `fn_index`, `layer_index` — dictionaries with stable identifiers as keys for O(1) access in subsequent steps. |
| 3 | Cross-resolve | Methods are bound to their classes via `_resolve_function_to_class` (line-range matching within the same file). Layers receive a `tier` value from the `layer_order` config key. A `module_to_layer_map` is built to resolve concrete modules to their architectural layer names. |
| 4 | Denormalize | Cross-adapter metrics are propagated downward: `file_mi` into functions and classes, `class_lcom4` into functions, `max_method_cc` / `mean_method_cc` into classes. |
| 5 | Single-source events | Violation events are emitted from a single source: `HIGH_CC_METHOD`, `LOW_MI_FILE`, `LOW_COHESION_CLASS`, `DEAD_CODE_NODE`, `IMPORT_CYCLE`. |
| 6 | Multi-source events | `LAYER_VIOLATION` (import_graph + import_linter, `strength="strong"`), `SDP_VIOLATION`, `SLP_VIOLATION`, `OVERLOADED_CLASS` (cohesion + radon, `strength="strong"`) — events backed by evidence from two adapters simultaneously. |
| 7 | Deduplicate | Events are deduplicated by `id` (type + location key parts): when IDs collide, evidence lists are merged, severity is upgraded to the maximum, and strength is promoted to `"strong"` when ≥ 2 sources are present. The final list is sorted: `error` first, then `warning`, with `type` ASC within each group. |
| 8 | Summary | Summary sub-sections are computed: `ComplexitySummary`, `MaintainabilitySummary`, `CohesionSummary`, `ImportsSummary`, `DeadCodeSummary`. CC threshold = 10 (module-level constant `CC_THRESHOLD`); LCOM4 threshold is read from `config["cohesion_threshold"]` (default 1). |
| 9 | Assemble | The final `AggregatedReport` Pydantic object is built from `meta`, `summary`, `entities`, `violations`, and `dead_code`, then serialized via `.model_dump()`. |

**Output schema** is defined in `schema.py` via Pydantic v2. Key types:

- `AggregatedReport` — root container
- `ReportMeta` — run metadata: `generated_at`, `adapters_succeeded`, `adapters_failed`, `lizard_used`, `config_defaults_used`
- `EntitiesSection` — normalized entities: `files`, `classes`, `functions`, `layers`
- `ViolationEvent` — violation event: `id`, `type`, `severity`, `location`, `metrics`, `evidence[]`, `strength`
- `AggregatedSummary` — aggregate metrics across all dimensions

**Known limitation of the cycle detector (Phase 1):** `_detect_import_cycles` identifies only bidirectional pairs (A→B and B→A simultaneously present). Cycles of length ≥ 3 (A→B→C→A) are silently missed. This is documented in the source code and in `SOLID_audit.md`; a Phase 2 fix using Tarjan's SCC algorithm is planned.

### LLM Layer: Heuristics and Adapter

The LLM layer now relies on a more intelligent static front: before invoking the model, the entire project is passed through an AST-based classification of class roles (`ClassRole`) and an updated set of SOLID heuristics for OCP/LSP.

**Heuristic layer (AST + ClassRole)**

The `heuristics` module no longer inspects classes blindly. For each `ast.ClassDef`, the tool first computes a class role:

- `PURE_INTERFACE` — ABC/Protocol without state: all non-dunder methods are abstract.
- `INFRA_MODEL` — Pydantic/SQLAlchemy models and similar (InfraScore ≥ 2 across 5 structural signals).
- `CONFIG` — configuration classes (`BaseSettings` and equivalents).
- `DOMAIN` — all other domain and service classes (default role).

OCP heuristics exclude classes with the `INFRA_MODEL` and `CONFIG` roles; all others (`DOMAIN`, `PURE_INTERFACE`) pass through filtering. LSP heuristics exclude abstract classes (ABC, Protocol) via `_is_abstract_class()`.

**Current set of LSP/OCP heuristics**

The updated set of AST heuristics focuses on domain classes and respects the role of the parent:

| ID          | Principle | What it detects (with ClassRole applied)                        |
|-------------|-----------|-----------------------------------------------------------------|
| `OCP-H-001` | OCP       | Top-level `if/elif` chain with `isinstance` (≥ 3 branches)     |
| `OCP-H-002` | OCP       | `match/case` used as a type dispatcher                          |
| `OCP-H-004` | OCP       | High cyclomatic complexity + `isinstance` in domain methods     |
| `LSP-H-001` | LSP       | `raise NotImplementedError` in an overriding method             |
| `LSP-H-002` | LSP       | Empty stub method in a subclass                                 |
| `LSP-H-003` | LSP       | `isinstance` check on a parameter of the base type             |
| `LSP-H-004` | LSP       | Problematic child `__init__` relative to the parent constructor |

`LSP-H-004` detects a subclass `__init__` that omits `super().__init__()`. Exceptions: `@dataclass` classes, classes whose parent is in `_LSP_H004_EXCLUDED_PARENTS` (`object`, `ABC`, `Protocol`, `TypedDict`, `NamedTuple`, `BaseModel`), and parents with the `PURE_INTERFACE` role (with the caveat that interfaces from `project_map.interfaces` without `source_code` cannot be fully verified).

The heuristic layer also implements a **multi-signal INFRA filter**: Pydantic/SQLAlchemy/Settings classes are recognized by a set of structural signals (decorators `table_name`/`__tablename__`, base classes `Base`/`DeclarativeBase`/`DeclarativeBaseNoMeta`, `BaseModel`, `BaseSettings`) and are assigned the `INFRA_MODEL` or `CONFIG` role, after which they are fully excluded from LSP/OCP analysis.

The heuristic layer also implements a **multi-signal INFRA filter**: Pydantic/SQLAlchemy/Settings classes are recognized by a set of structural signals (decorators `table_name`/`__tablename__`, base classes `Base`/`DeclarativeBase`/`DeclarativeBaseNoMeta`, `BaseModel`, `BaseSettings`) and are assigned the `INFRA_MODEL` or `CONFIG` role, after which they are fully excluded from LSP/OCP analysis.

**Forwarding heuristic signals to the LLM**

After `HeuristicsAdapter` produces `findings` and the `candidates` list, `pipeline.py` passes both objects into `LlmSolidAdapter` via the `LlmAnalysisInput.heuristic_findings` field. Inside `_build_context()`, the findings list is filtered by the current candidate's `class_name` and formatted into a `Static analysis signals:` section of the user prompt (`user_base.md`):

```text
Static analysis signals:
The following signals were detected by automated heuristic analysis for this class.
Use them as supplementary context — weigh them against the code above and override
if the evidence does not support them.

- [OCP] Top-level isinstance chain (rule: OCP-H-001)
```

This creates semantic continuity between the static and LLM layers: the model sees the same structural anomalies that the AST analyzer detected and can either confirm them with an explanation or — when the code evidence is insufficient — explicitly produce no finding. When no heuristic signals are found for a given candidate, the prompt receives an explicit fallback: `No static heuristic signals found for this class.`

**Known limitation:** the total token budget (`llm.max_tokens_per_run` in `solid_config.json`) must be sufficient to process all candidates. When the budget is exhausted, the remaining candidates are skipped with a `BudgetExhaustedError` — the pipeline does not abort and returns partial results based on static analysis and heuristics. Recommended minimum: ~10,000 tokens per candidate × number of candidates.

### LLM Analysis of OCP/LSP: Layer Architecture

The LLM path in SOLID Verifier is designed as a dedicated layer on top of static heuristics, not as a black box. It operates only on candidates pre-identified by AST heuristics and returns findings in the same domain format as the rest of the tool (`Finding` / `FindingDetails`).

The high-level purpose of this layer is not to replace static analysis but to refine and explain heuristic suspicions around OCP and LSP: provide a human-readable explanation, a concrete recommendation, and a calibrated confidence estimate without the risk that model hallucinations break the overall report.

***

#### Data Flow: Analysis Levels

The SOLID Verifier follows a strict two-level architecture. The pipeline separates fast deterministic metric collection from deeper contextual reasoning performed by the LLM.

**Level 1: Base static analysis (parallel layer)**  
All classes implementing `IAnalyzer` run independently of one another:

- **Metric adapters** (`Radon`, `Cohesion`, `ImportGraph`, `ImportLinter`, `Pyan3`) collect statistics, compute complexity and coupling, and build dependency graphs.
- **The heuristic adapter** (`HeuristicsAdapter`) independently parses the project source code, builds an AST-based `ProjectMap`, and identifies suspicious code fragments — candidates for potential OCP/LSP violations (`HeuristicResult.candidates`).

**Level 2: LLM overlay (deep analysis)**  
This level starts only after Level 1 completes and only if LLM analysis is enabled in the configuration. `pipeline.py` takes the project AST map and the candidate list from heuristics and passes them into `LlmSolidAdapter`. For each candidate, the layer performs:

- **Context Assembler**: collects isolated context (class source code, dependencies, interfaces).
- **Prompt Builder**: builds system/user prompts from `.md` templates and a strict JSON response schema.
- **LlmGateway**: sends the request to the provider (OpenRouter/OpenAI) while handling caching, token budget control, and retries.
- **Response Parser (ACL-B)**: safely validates the model output and converts it into typed `Finding` objects.

At the end, the **Report Aggregator** merges the flat stream of static metrics with heuristic and LLM findings into one consolidated report (`solid_report.log` / HTML).

```text
LEVEL 1: Base independent adapters
┌────────────────┐ ┌────────────────┐ ┌────────────────────────────────┐
│ Radon, Cohesion│ │  ImportGraph   │ │ HeuristicsAdapter (AST analysis)│
│ ImportLinter   │ │   Pyan3        │ │  └─ ProjectMap                 │
└───────┬────────┘ └───────┬────────┘ │  └─ LlmCandidate[]             │
        │                  │          └────────────────┬───────────────┘
        │                  │                           ▼
        │                  │   LEVEL 2: LLM overlay (LlmSolidAdapter)
        │                  │          ┌────────────────┴───────────────┐
        │                  │          ├─ Context Assembler             │
        │                  │          ├─ Prompt Builder                │
        │                  │          ├─ LlmGateway (Cache/Budget/ACL) │
        │                  │          └─ Response Parser (ACL-B)       │
        │                  │          └────────────────┬───────────────┘
        ▼                  ▼                           ▼
     =======================================================
            Report Aggregator → solid_report.log / HTML
```

#### Anti-Corruption Layer: Two Levels of Protection

The LLM layer is isolated from the rest of the system by two independent Anti-Corruption Layers.

- **ACL-A (Gateway, transport level):**  
  `OpenRouterProvider._parse_success(...)` and `LlmGateway` are responsible for safe HTTP-response handling, `finish_reason` processing, API errors, timeouts, and token limits. At this level, the LLM adapter always receives a normalized `LlmResponse` with fields `content`, `tokens_used`, and `model` — not a raw provider-specific payload.

- **ACL-B (Response Parser, semantic level):**  
  The function `parse_llm_response(content: str, candidate: LlmCandidate) -> ParseResult` performs:
  - JSON extraction (plain JSON / fenced code / regex-based fallbacks);
  - structural validation (`findings` must be a list, each item must be a dict);
  - per-item validation via `validate_finding(raw, candidate)`;
  - assembly of `ParseResult(findings, warnings, status)` with statuses `success / partial / failure`.

Any failure at either level may partially or fully disable the LLM path for a particular candidate, but it **never breaks the whole pipeline**: static and heuristic findings continue to work as usual.

#### Two Sources of Truth: Heuristics and LLM

After the heuristics refactor, the "two sources of truth" architecture became even more explicit.

**1. Static analysis and heuristics — source of coordinates and trust**

The static layer (Radon, Cohesion, ImportGraph, ImportLinter) plus AST heuristics and `ClassRole` determine:

- which files, classes and methods are even worth analyzing;
- which of them are domain `BEHAVIORAL` objects potentially violating OCP/LSP;
- which exact heuristics fired (for example, the combination of `LSP-H-001` + `OCP-H-001` + `OCP-H-004` for the same class).

For each candidate, an `LlmCandidate` is built and treated as the **source of truth for coordinates**:

- `file` — file path;
- `class_name` — class name;
- principle type (`OCP`/`LSP`) — a base frame for the final rule (`OCP-LLM-001` / `LSP-LLM-001`);
- the list of fired heuristics (`heuristic_reasons`).

The static layer is self-sufficient: at the level of the latest report, all goals of the heuristics refactor are achieved even with zero LLM contribution (LLM candidates were successfully selected, but model responses did not participate in decisions yet).

**2. LLM — source of meaning and explanations**

The LLM only sees pre-filtered candidates and is used as a **source of semantic content**:

- generates a human-readable `message` (problem description);
- refines the principle (`details.principle`) and provides an explanation (`details.explanation`);
- proposes concrete refactoring suggestions (`details.suggestion`);
- when possible, points to the method (`details.method_name`) and gives best-effort information on which heuristics were corroborated (`details.heuristic_corroboration`).

All LLM findings pass through the `Response Parser` (ACL-B), which strictly validates JSON and populates the `Finding` domain model. Key fields (`file`, `class_name`, `source="llm"`, rule, principle) are always derived from **heuristics** rather than free-form model text so that the trusted static layer remains leading.

The result: static and LLM-based findings live in the same `Finding` list but with different "zones of responsibility": coordinates and principle assignment are defined by statics, while explanations and recommendations come from the LLM.

#### LLM Finding Field Map

| Field                               | Stored in                                 | Default source               | Implementation layer                                   |
|-------------------------------------|-------------------------------------------|------------------------------|--------------------------------------------------------|
| `rule`                              | `Finding.rule`                            | Computed (`OCP-LLM-001`)     | `validate_finding` (ACL-B)                             |
| `file`                              | `Finding.file`                            | Heuristics (`candidate`)     | `validate_finding` (from `candidate.filepath`)         |
| `class_name`                        | `Finding.class_name`                      | Heuristics (`candidate`)     | `validate_finding` (from `candidate.classname`)        |
| `line`                              | `Finding.line`                            | Always `None` for LLM        | `validate_finding` (fixed)                             |
| `severity`                          | `Finding.severity`                        | LLM → normalized             | `validate_finding` (`error/warning/info`)              |
| `message`                           | `Finding.message`                         | LLM (required field)         | `validate_finding` (`raw["message"]`)                  |
| `source`                            | `Finding.source`                          | Always `"llm"`               | `validate_finding` (fixed)                             |
| `details.principle`                 | `FindingDetails.principle`                | LLM + candidate fallback     | `validate_finding` (JSON → `OCP/LSP` → fallback)       |
| `details.explanation`               | `FindingDetails.explanation`              | LLM                          | `validate_finding` (`"explanation"` / `"details"`)      |
| `details.suggestion`                | `FindingDetails.suggestion`               | LLM                          | `validate_finding` (`"suggestion"`)                    |
| `details.analyzed_with`             | `FindingDetails.analyzed_with`            | LLM (best-effort list[str])  | `validate_finding` (filtered list of strings)          |
| `details.heuristic_corroboration`   | `FindingDetails.heuristic_corroboration`  | Computed                     | `validate_finding` (`True` if LLM confirms heuristic)  |
| `details.method_name`               | `FindingDetails.method_name`              | LLM (optional)               | `validate_finding` (`"method_name"`)                   |

### In Development

- **`differ.py`** and **`generator.py`**  
  Tools for rendering the JSON report into a visual HTML dashboard (Jinja2 templates) and for tracking metric degradation or improvement over time by comparing current reports with a stored baseline.

***

## Repository Structure

File structure of the project, reflecting the location of SOLID Verifier components and their current naming:

```text
│       solid_verifier/                           # Project root directory
│       ├── prompts/                              # External prompt templates and LLM response schema
│       │   ├── system.md                         # System prompt (expert role and base rules)
│       │   ├── user_base.md                      # Base prompt section (code injection, context and heuristic signals)
│       │   ├── user_ocp_section.md               # OCP-specific verification instructions
│       │   ├── user_lsp_section.md               # LSP-specific verification instructions
│       │   └── response_schema.json              # Strict JSON response contract expected from the model
│       ├── tests/                                # Unit and integration tests for the verifier
│       │   ├── fixtures/                         # Mock data and fake projects (sample_project)
│       │   ├── static_adapters/                  # Tests for all static adapters (Radon, Cohesion, ImportGraph, ImportLinter, Pyan3)
│       │   │   ├── test_cohesion_adapter/        # LCOM4 adapter tests: computation, ancestor enrichment, classifier
│       │   │   ├── test_import_graph_adapter/    # Import graph and stability metrics tests
│       │   │   ├── test_import_linter_adapter/   # Architectural contract verification tests
│       │   │   ├── test_pyan3_adapter/           # Call-graph construction and dead code detection tests
│       │   │   └── test_radon_adapter/           # Cyclomatic complexity metrics tests
│       │   └── llm/                              # Unit and E2E tests for LLM integration (Gateway, ACL)
│       │       └── test_heuristics/              # Unit and integration test suite for SOLID heuristics
│       ├── solid_dashboard/                      # Main Python package of the tool
│       │   ├── __main__.py                       # CLI entry point
│       │   ├── config.py                         # solid_config.json parsing and validation logic
│       │   ├── defaults.py                       # Pipeline configuration constants and default values
│       │   ├── pipeline.py                       # Central orchestrator of static adapters and LLM analysis
│       │   ├── report_aggregator.py              # Aggregation, deduplication and normalization of all adapter results
│       │   ├── schema.py                         # Pydantic schemas for the aggregated report
│       │   ├── py.typed                          # PEP 561 marker: package ships inline types for static analysers
│       │   ├── interfaces/                       # Abstract base classes / Python protocols
│       │   │   └── analyzer.py                   # Base IAnalyzer interface
│       │   ├── adapters/                         # Classic static analysis tool implementations
│       │   │   ├── radon_adapter.py              # radon + lizard (parameters, nesting)
│       │   │   ├── cohesion_adapter.py           # Custom LCOM4 (two-pass, ancestor enrichment, ignore @property)
│       │   │   ├── class_classifier.py           # Semantic class role classification (concrete/abstract/interface/dataclass)
│       │   │   ├── import_graph_adapter.py       # Import graph (grimp) + stability metrics
│       │   │   ├── import_linter_adapter.py      # CLI lint-imports + dynamic contract generation
│       │   │   ├── pyan3_adapter.py              # Call graph and dead code (two-pass, project-agnostic)
│       │   │   └── heuristics_adapter.py         # Adapter for 7 LSP/OCP heuristics
│       │   ├── llm/                              # Isolated LLM analysis and integration layer
│       │   │   ├── analysis/                     # AST analysis: parser and class role classifier (ClassRole)
│       │   │   ├── heuristics/                   # Public heuristics package facade: 7 independent OCP/LSP modules
│       │   │   ├── llm_client/                   # LLM infrastructure client (gateway, provider, cache, budget, adapter)
│       │   │   ├── errors.py                     # LLM layer domain errors (Retryable/NonRetryable, config validation)
│       │   │   ├── types.py                      # LLM layer domain types (Finding, LlmCandidate, ParseResult, LlmConfig)
│       │   │   └── __init__.py                   # High-level LLM layer API exports for the rest of the project
│       │   └── report/                           # Report generation and processing module
│       │       ├── dashboard/                    # HTML dashboard rendering and comparison tools (differ, generator)
│       │       ├── pipeline_report/              # Pipeline run logs (solid_debug.log, solid_pipeline_report.log)
│       │       ├── project_mask/                 # Project mask generator and artifact (skeleton export)
│       │       ├── project_tree/                 # Project tree generator and text artifact
│       │       └── templates/                    # Jinja2 templates for the HTML report
│       ├── .env                                  # Environment secrets (LLM keys; not committed)
│       ├── .env.example                          # .env template with example environment variables
│       ├── .gitignore                            # Files and directories excluded from git
│       ├── .importlinter                         # Base import-linter config (dynamically extended by the adapter)
│       ├── pyproject.toml                        # Project metadata and build tool configuration
│       ├── README.md                             # Project documentation (EN)
│       ├── README.ru.md                          # Project documentation (RU)
│       ├── requirements.txt                      # Project dependencies list
│       ├── run_solid_dashboard.py                # Helper script for running the pipeline
│       └── solid_config.json                     # Main verifier configuration file
```

***

## Configuration (`solid_config.json`)

`solid_config.json` lives in the **root of the analysed project** (`solid_verifier/`) and is the **single configuration point** for the entire pipeline. Every adapter — static and LLM — unconditionally respects its rules. Changing any parameter takes effect immediately across the whole pipeline without touching Python code.

---

### Full File with Annotations

```json
{
  "package_root": "solid_dashboard",

  "layers": {
    "pipeline":      ["pipeline", "report_aggregator", "__main__"],
    "adapters":      ["adapters"],
    "interfaces":    ["interfaces"],
    "llm":           ["llm"],
    "schema":        ["schema", "defaults", "config"]
  },

  "utility_layers": {
    "report": ["report"]
  },

  "layer_order": [
    "pipeline",
    "adapters",
    "interfaces",
    "llm",
    "schema"
  ],

  "interface_layers": [
    "interfaces"
  ],

  "sdp_tolerance": 0.10,

  "allowed_dependency_exceptions": [
    {
      "source": "adapters",
      "target": "llm",
      "reason": "Adapters (heuristics_adapter) delegate to LLM subsystem directly — acceptable cross-tier coupling via explicit interface"
    }
  ],

  "ignore_dirs": [
    ".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", "tests", "report", "build", "dist",
    ".solid-cache"
  ],

  "external_layers": {
    "ast_libs":     ["ast", "astroid"],
    "analysis_libs": ["radon", "lizard"],
    "graph_libs":   ["pyan3", "networkx"],
    "linter_libs":  ["grimp"],
    "llm_libs":     ["openai", "httpx"],
    "web_libs":     ["fastapi", "starlette"]
  },

  "pyan3": {
    "collision_rate_threshold": 0.35,
    "abort_on_high_collision": false
  },

  "llm": {
    "enabled": true,
    "provider": "openrouter",
    "model": "openai/gpt-4o-mini",
    "api_key": null,
    "endpoint": null,
    "max_tokens_per_run": 70000,
    "cache_dir": ".solid-cache/llm",
    "prompts_dir": "prompts"
  }
}
```

---

### Field Reference

#### Target package

| Field | Type | Description |
|-------|------|-------------|
| `package_root` | string | Name of the root Python package to analyse. **The single directory-control point** for all adapters (Radon, Cohesion, ImportGraph, ImportLinter, Pyan3, Heuristics) — every adapter operates strictly within this package. |

---

#### Architectural layers

| Field | Type | Description |
|-------|------|-------------|
| `layers` | `Dict[str, List[str]]` | Maps logical architecture layer names to sub-package names inside `package_root`. Used by `ImportGraphAdapter` to build the layer graph and compute stability metrics, and by `ImportLinterAdapter` to generate layered-architecture contracts. |
| `utility_layers` | `Dict[str, List[str]]` | Cross-cutting layers (e.g. `core`, `schemas`) that may be imported by and may import any other layer. They participate in metrics and visualisation but are **intentionally excluded** from SDP and SLP checks to prevent false violations. |
| `layer_order` | `List[str]` | Ordered list of layers from the topmost (user-facing) to the bottommost (infrastructure). The **single source of truth** for the allowed dependency direction: upper layers may depend on lower ones; the reverse is a violation. Used by both architectural adapters. |
| `interface_layers` | `List[str]` | Layers declared as "interface" layers. Affects violation severity in the `SLP-001` detector: a Skip-Layer jump that bypasses an interface layer is rated more strictly. |

---

#### SDP settings

| Field | Type | Description |
|-------|------|-------------|
| `sdp_tolerance` | float | Acceptable instability gap for the Stable Dependencies Principle check (`SDP-001`). If the instability of a dependent layer exceeds the instability of its dependency by less than this value, no violation is recorded. Current value: `0.10` (10%). |
| `allowed_dependency_exceptions` | `List[Object]` | Explicit exceptions to SDP checks. Each entry: `source` (the depending layer), `target` (external layer name), `reason` (justification). Use for deliberate and documented architectural trade-offs — e.g. ORM models inheriting `SQLAlchemy Base`. |

---

#### Directory exclusions

| Field | Type | Description |
|-------|------|-------------|
| `ignore_dirs` | `List[str]` | Global list of directories excluded from all adapters. **Guarantees** that no adapter reaches beyond business logic: virtual environments, caches, tests, tooling, and build artefacts are excluded at the configuration level, not in code. |

---

#### External dependencies

| Field | Type | Description |
|-------|------|-------------|
| `external_layers` | `Dict[str, List[str]]` | Maps logical names of external libraries to their real package names. Used by `ImportGraphAdapter` to include external dependencies in the layer graph and correctly compute stability metrics (`Ca`, `Ce`, `Instability`). Allows tracking dependencies on `sqlalchemy`, `fastapi`, etc. in an architectural context rather than as a black box. |

---

#### LLM settings

| Field | Type | Description |
|-------|------|-------------|
| `llm.enabled` | bool | Enables or completely disables the LLM layer. When `false`, the pipeline runs on static analysis and heuristics only — no network calls are made. |
| `llm.provider` | string | Provider name. Currently supported value: `"openrouter"`. |
| `llm.model` | string | Model identifier in the provider's format. For OpenRouter — `"openai/gpt-4o-mini"` or any other model from [openrouter.ai/models](https://openrouter.ai/models). |
| `llm.api_key` | null | **Always leave as `null`**. The key is automatically read from the `OPENROUTER_API_KEY` environment variable (`.env` file). This is an intentional safeguard against secrets leaking into version control or JSON configs. |
| `llm.endpoint` | null | Overrides the API endpoint URL. `null` uses the default OpenRouter address. Useful for proxies or self-hosted LLMs. |
| `llm.max_tokens_per_run` | int | Total token budget for the entire LLM run. When the budget is exhausted, remaining candidates are skipped — only static and heuristic findings will appear in the report for them. Candidates are processed in descending priority order (number of heuristic hits + presence of class hierarchy). |
| `llm.cache_dir` | string | Path to the file-based LLM response cache directory. The cache key is the SHA-256 hash of the (prompt + options) pair: an identical request is never sent twice and no tokens are spent. |
| `llm.prompts_dir` | string | Path to the directory containing `.md` prompt templates and `response_schema.json`. Allows prompt changes without modifying Python code. |

***

## Running the Dashboard

### Preliminary Setup

From the repository root (`solid_verifier/`) with the virtual environment activated:

```bash
pip install -r solid_verifier/requirements.txt
```

If LLM analysis is enabled, set the API key environment variable:

```bash
# Windows (PowerShell)
$env:OPENROUTER_API_KEY = "sk-or-..."

# Linux / macOS
export OPENROUTER_API_KEY="sk-or-..."
```

### 1. Direct CLI launch

```bash
python -m tools.solid_verifier.solid_dashboard \
    --target-dir ./app \
    --config ./solid_config.json
```

This will:

1. Load `solid_config.json` from the project root.
2. Run all static adapters in sequence (Radon, Cohesion, ImportGraph, ImportLinter, Pyan3).
3. If `llm.enabled: true` and OCP/LSP candidates are found, perform LLM analysis via OpenRouter.
4. Print a JSON report to stdout and save it into `solid_verifier/solid_dashboard/report/solid_report.log`.

### 2. Through the wrapper script

```bash
python run_solid_dashboard.py
```

The script hardcodes `target_dir = ./app` and `config_path = ./solid_config.json`, invokes the internal CLI, and forwards the exit code. It is convenient for Git hooks and CI pipelines.

### 3. Running tests

From the directory (`solid_verifier/`):

```bash
# All tests except those requiring a real external API
pytest -m "not manual" -vv

# Manual end-to-end test with the real OpenRouter API
# (requires OPENROUTER_API_KEY to be set)
pytest tests/llm/test_open_router_manual.py -m manual -vv -s
```

***

## Dependencies

The project uses the following main libraries (declared in `solid_verifier/requirements.txt`):

- **[radon](https://pypi.org/project/radon/)** (`>=6.0,<7.0`) — cyclomatic complexity and maintainability metrics (MI, Halstead).
- **[pydantic](https://pypi.org/project/pydantic/)** (`>=2.0,<3.0`) — typing and validation of report data schemas (`RadonResult`, `CohesionResult` in `schema.py`). Used as a contract layer for the future Report Aggregator.
- **[grimp](https://pypi.org/project/grimp/)** (`>=2.3,<3.0`) — building and analysing the import graph at the architectural-layer level. Used by `ImportGraphAdapter` to compute Martin's stability metrics (Ca, Ce, Instability), detect SDP and SLP violations.
- **[import-linter](https://pypi.org/project/import-linter/)** (`==2.11`) — enforcement of architectural contracts and layer isolation. The version is strictly pinned because the adapter parses unstructured console text output from the CLI tool, which may change in newer releases.
- **[lizard](https://pypi.org/project/lizard/)** (`==1.17.10`) — a lightweight analyzer used exclusively to extract two side metrics: parameter count (for ISP) and maximum nesting depth. The version is pinned to protect against changes in the internal structure of its AST objects.
- **[pyan3](https://pypi.org/project/pyan3/)** (`>=2.2,<3.0`) — static call-graph analyzer for modern Python versions (3.10–3.14).
- **[httpx](https://pypi.org/project/httpx/)** (`>=0.27,<2.0`) — HTTP client for interacting with LLM provider APIs (OpenRouter/OpenAI).
- **[Jinja2](https://pypi.org/project/Jinja2/)** (`>=3.1,<4.0`) — HTML report generation (in development).
- **[python-dotenv](https://pypi.org/project/python-dotenv/)** (`>=1.0,<2.0`) — secure loading of secrets (like `OPENROUTER_API_KEY`) from a `.env` file into environment variables, preventing leaks into version control or JSON configs.
- **[networkx](https://pypi.org/project/networkx/)** (`>=3.0,<4.0`) — a transitive dependency of both `grimp` and `pyan3`: both libraries use it to build and traverse import and call graphs. Pinned explicitly in `pyproject.toml` to guarantee version compatibility between the two consumers.

All dependencies are pinned in `requirements.txt` to stable versions compatible with the current Python runtime.

***

## Roadmap

- **Short-term tasks**

  - Extend `import_graph_adapter` with layer-level cycle detection and full `evidence` population for SDP/SLP violations based on import traces.
  - Bring the LLM layer to production-ready: stabilize prompts and the JSON response schema, reduce `parse_failures` to zero on typical projects while preserving strict ACL guarantees (no model error should ever break the static report).
  - Fine-tune handling of infrastructural classes: choose between "LLM-only" analysis and full filtering via an enhanced `ClassRole` so that OCP/LSP checks stay maximally focused on domain layers.
  - Evolve the visual HTML dashboard (`generator.py`, `differ.py`) with explicit attribution of contributions from the static layer, heuristics and LLM for each finding.

- **Mid-term tasks**

  - Extract SOLID Verifier into a standalone package (`pip install solid_verifier`) while preserving the current adapter and LLM architecture.
  - Integrate with IDEs / VS Code for interactive browsing of LSP/OCP candidates and contextual LLM recommendations directly in the editor.
  - **Switch `pyan3_adapter` to `--dot` mode:** replace the text parser with a Graphviz DOT-format parser.
  - Extend `cohesion_adapter` to correctly account for attributes declared via `__slots__`, `dataclasses.field()`, and Pydantic validators — enabling accurate classification of such classes and eliminating false LCOM4 violations.

- **Long-term tasks**

  - Use the LLM not only for OCP/LSP, but also as a "mentoring layer" on top of SRP/DIP metrics: interpret high LCOM4, broken import contracts and highly complex functions as human-readable refactoring advice, **without** giving the model control over the report itself.
