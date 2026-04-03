# SOLID Verifier Dashboard

Russian version: [README.ru.md](README.ru.md)

## Introduction

The idea for this code analyzer grew out of a paradox of modern AI-assisted development: while building my first educational project, *Scopus Search Code*, the speed of code generation began to outpace my ability to fully understand the growing graph of dependencies inside the project. My analytical background pushed me to regain control over the system as it evolved. I wanted a tool that could solve two problems at once: serve as a strict independent verifier of architectural correctness and also act as a readable map of the hidden connections and dependency paths inside the codebase.

That is how the concept of the SOLID Verifier emerged. For object-oriented Python, SOLID remains one of the most mature and universal frameworks for reasoning about architectural quality. But this choice also has a deeply personal dimension. The inner logic, beauty, and philosophy of SOLID were once explained to me by a close friend — a talented engineer and experienced developer. This project is a tribute to his craft and is dedicated to him.

***

`solid_dashboard` is a config-driven CLI tool that analyzes Python projects for adherence to SOLID principles and layered architecture. It runs a pipeline of static analyzers, computes metrics, checks architectural contracts, and — optionally — deepens OCP/LSP analysis with an LLM layer. The result is written into a machine-readable JSON report (`solid_report.log`).

The tool is project-agnostic: it can be reused across different Python codebases, not only within the Scopus Search API project.

## Key Features

- **Single CLI entry point** and full orchestration via `pipeline.py` for running the entire analysis pipeline against a target project.
- **Strict isolation and configuration-driven behavior** (`solid_config.json`):
  - **Single point of directory control:** the target directory (`package_root`) and exclusions (`ignore_dirs`) are defined in one place and strictly respected by all static and LLM adapters. The pipeline is safely isolated from `.venv`, tests, and utility scripts.
  - Define architectural layers and their module prefixes.
  - Define external "library layers" (database, web frameworks, etc.).
  - Secure secret management (LLM keys are automatically loaded from a `.env` file, keeping the JSON config clean).
- **Static metrics**:
  - **Complexity and maintainability** via `radon` (Cyclomatic Complexity, Maintainability Index).
  - **Cohesion** via a custom **LCOM4** adapter based on Python’s built-in `ast` (with smart filtering of properties and utility methods to eliminate false positives).
  - **Call graph and dead code** via `pyan3` (fully safe execution with no global environment state mutations).
- **Architecture and dependencies**:
  - **Import graph** based on `grimp`.
  - **Layered architecture contracts** enforced through the `import-linter` CLI (via on-the-fly dynamic generation of temporary contract files).
  - Martin stability metrics (`Ca`, `Ce`, `Instability`) for each layer.
- **LLM-based OCP/LSP analysis** (implemented, optionally enabled through `solid_config.json`):
  - AST heuristics identify potential OCP/LSP violations and build a list of candidates.
  - `LlmSolidAdapter` sends each candidate to an LLM for verification (via OpenRouter).
  - A two-level Anti-Corruption Layer (ACL-A + ACL-B) protects the pipeline from malformed model responses.
  - LLM results are normalized into the same `Finding` format as static and heuristic results.
- **Extensible pipeline** through a clear `IAnalyzer` interface implemented by all static adapters.
- **Machine-readable report** in `solid_report.log` (JSON) and planned visual HTML dashboards.

***

## Current Architecture and Adapters

- The verifier is deliberately built around division of labor and minimization of nondeterminism. We do not use an LLM where deterministic analysis already does the job better.
- Three out of five SOLID principles — **SRP**, **ISP**, and **DIP** — are covered effectively and reliably by deterministic static analysis: LCOM4 and complexity metrics reveal overloaded objects, while import graphs strictly track dependency inversion. Static analysis is transparent, fast, and not vulnerable to hallucinations, model drift, or network failures.
- The more expensive **heuristics + LLM** path is applied selectively, only for **OCP** and **LSP**. These two principles require semantic reasoning about behavior, contracts, and architectural intent, so the model is used only where static heuristics explicitly indicate that deeper analysis is necessary.

### Static Adapters

At the static-analysis level, the dashboard is implemented as an internal framework of **adapters** orchestrated by a central `pipeline.py` module. Every static adapter strictly obeys the single point of directory control (`package_root` and `ignore_dirs` from `solid_config.json`):

- Inherits from the common `IAnalyzer` interface (`solid_dashboard.interfaces.analyzer.IAnalyzer`).
- Exposes a `name` property.
- Implements `run(target_dir, context, config) -> Dict[str, Any]`.

The LLM layer is implemented separately from `IAnalyzer` and is invoked directly by `pipeline.py` based on heuristic results.

- **`radon_adapter.py`**  
  Uses `radon` to compute cyclomatic complexity and Maintainability Index, strictly limiting itself to the target directory. It additionally integrates `lizard` to extract **only** `parameter_count` and maximum nesting depth. Overlapping metrics are intentionally ignored.

- **`cohesion_adapter.py`**  
  A custom implementation of the **LCOM4** metric with no external dependencies, built directly on Python’s `ast`. It surgically traverses files, filtering them by `ignore_dirs`. It smartly handles `async` and Pydantic annotations, and purposefully **excludes `@property` methods** and `__init__` constructors. This radically reduces false positives and ensures mathematical precision in calculating class cohesion.

- **`import_graph_adapter.py`**  
  Builds the import graph using `grimp`. It groups modules into layers based on the configuration, filters out ignored directories, identifies dependencies on third-party libraries (DIP), and calculates Martin stability metrics (`Ca`, `Ce`, `Instability`).

- **`import_linter_adapter.py`**  
  Integrates with `import-linter` through its stable CLI (`lint-imports`). The adapter **dynamically generates a temporary configuration file** (`.importlinter_auto_*`), injecting the `root_packages` based on `package_root` and translating `ignore_dirs` into `ignore_imports` on the fly. After running the isolated CLI process, it parses the output — stripping ANSI terminal colors — and forms a structured JSON report of broken/kept contracts.

- **`pyan3_adapter.py`**  
  Uses `pyan3` to build a call graph and identify potential **dead code**. During refactoring, this adapter was stripped of global environment state mutations (removed `os.chdir` calls), making it absolutely safe and project-agnostic. FastAPI-specific glue code is filtered out to avoid false positives.

### LLM Layer: Heuristics and Adapter

The LLM layer consists of two connected parts: a heuristics module and the LLM adapter itself.

**`heuristics.py`** implements 7 static AST heuristics for detecting OCP/LSP violation candidates:

| ID          | Principle | What it detects |
|-------------|-----------|-----------------|
| `OCP-H-001` | OCP       | `if/elif` chain with `isinstance` (3+ branches) |
| `OCP-H-002` | OCP       | `match/case` used as a type dispatcher (Python 3.10+) |
| `OCP-H-004` | OCP       | High cyclomatic complexity combined with `isinstance` |
| `LSP-H-001` | LSP       | `raise NotImplementedError` inside an overridden method |
| `LSP-H-002` | LSP       | Stub method (`pass` or docstring only) in a subclass |
| `LSP-H-003` | LSP       | `isinstance` check for a parameter annotated with a base type |
| `LSP-H-004` | LSP       | Subclass `__init__` without `super().__init__()` |

**`llm_adapter.py`** (`LlmSolidAdapter`) orchestrates LLM analysis: for each heuristic candidate it assembles context, builds a prompt, and sends the request through `LlmGateway`.

### OCP/LSP LLM Analysis: Layer Architecture

The LLM path in SOLID Verifier is designed as a dedicated layer on top of static heuristics, not as a black box. It operates only on candidates pre-identified by AST heuristics and returns findings in the same domain format as the rest of the tool (`Finding` / `FindingDetails`).

The high-level purpose of this layer is not to replace static analysis but to refine and explain heuristic suspicions around OCP and LSP: provide a human-readable explanation, a concrete recommendation, and a calibrated judgment while minimizing the risk that model hallucinations break the overall report.

#### Data Flow: Analysis Levels

The SOLID Verifier follows a strict two-level architecture. The pipeline separates fast deterministic metric collection from deeper contextual reasoning performed by the LLM.

**Level 1: Base static analysis (parallel layer)**  
All classes implementing `IAnalyzer` run independently of one another:

- **Metric adapters** (`Radon`, `Cohesion`, `ImportGraph`, `ImportLinter`, `Pyan3`) collect statistics, compute complexity and coupling, and build dependency graphs.
- **The heuristic adapter** (`HeuristicsAdapter`) independently parses the project source code, builds an AST-based `ProjectMap`, and identifies suspicious code fragments — candidates for potential OCP/LSP violations (`HeuristicResult.candidates`).

**Level 2: LLM overlay (deep analysis)**  
This level starts only after Level 1 completes and only if LLM analysis is enabled in the configuration. `pipeline.py` takes the project AST map and the candidate list from heuristics and passes them into `LlmSolidAdapter`. For each candidate, the layer performs:

- **Context Assembler**: collects isolated context (class source, dependencies, interfaces).
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
  `OpenRouterProvider.parse_success(...)` and `LlmGateway` are responsible for safe HTTP-response handling, `finish_reason` processing, API errors, timeouts, and token limits. At this level, the LLM adapter always receives a normalized `LlmResponse` with fields such as `content`, `tokens_used`, and `model`, rather than a raw provider-specific payload.

- **ACL-B (Response Parser, semantic level):**  
  The function `parse_llm_response(content: str, candidate: LlmCandidate) -> ParseResult` performs:
  - JSON extraction (plain JSON / fenced code / regex-based fallbacks);
  - structural validation (`findings` must be a list, each item must be a dict);
  - per-item validation via `validate_finding(raw, candidate)`;
  - assembly of `ParseResult(findings, warnings, status)` with `success / partial / failure`.

Any failure at either level may partially or fully disable the LLM path for a particular candidate, but it **never breaks the whole pipeline**: static and heuristic findings continue to work as usual.

#### Two Sources of Truth: Heuristics and LLM

A key characteristic of the Response Parser is that it deliberately combines two different data sources with different trust levels.

- **Heuristics (`LlmCandidate`) are the source of truth for coordinates**:
  - `file = candidate.filepath`
  - `class_name = candidate.classname`
  - `source = "llm"`
  - `line = None` (the LLM does not provide reliable line numbers)
- **The LLM is the source of semantic content**:
  - `message` (required description of the issue)
  - `severity` (normalized to `error / warning / info`)
  - `details.principle` — from JSON with fallback to `candidate.candidate_type`
  - `details.explanation`, `details.suggestion`, `details.method_name`, `details.analyzed_with` — best effort

Based on the inferred principle (`OCP` or `LSP`), the code automatically builds `rule` (`OCP-LLM-001` or `LSP-LLM-001`), so LLM findings fit naturally into the same rule system as heuristic rules like `OCP-H-001`.

#### LLM Finding Field Map

| Field | Stored in | Default source | Implementation layer |
|------|-----------|----------------|----------------------|
| `rule` | `Finding.rule` | Computed (`OCP-LLM-001`) | `validate_finding` (ACL-B) |
| `file` | `Finding.file` | Heuristics (`candidate`) | `validate_finding` (from `candidate.filepath`) |
| `class_name` | `Finding.class_name` | Heuristics (`candidate`) | `validate_finding` (from `candidate.classname`) |
| `line` | `Finding.line` | Always `None` for LLM | `validate_finding` |
| `severity` | `Finding.severity` | LLM → normalized | `validate_finding` (`error/warning/info`) |
| `message` | `Finding.message` | LLM (required field) | `validate_finding` (`raw["message"]`) |
| `source` | `Finding.source` | Always `"llm"` | `validate_finding` |
| `details.principle` | `FindingDetails.principle` | LLM + candidate fallback | `validate_finding` |
| `details.explanation` | `FindingDetails.explanation` | LLM | `validate_finding` |
| `details.suggestion` | `FindingDetails.suggestion` | LLM | `validate_finding` |
| `details.analyzed_with` | `FindingDetails.analyzed_with` | LLM (best-effort list[str]) | `validate_finding` |
| `details.heuristic_corroboration` | `FindingDetails.heuristic_corroboration` | Computed | `validate_finding` (`True` if LLM confirms heuristic suspicion) |
| `details.method_name` | `FindingDetails.method_name` | LLM (optional) | `validate_finding` |

### In Development

- **`differ.py`** and **`generator.py`**  
  Tools for rendering the JSON report into a visual HTML dashboard (Jinja2 templates) and for tracking metric degradation or improvement over time by comparing current reports with a stored baseline.

***

## Repository Layout

The file structure below reflects the location of SOLID Verifier components and their current names:

```text
scopus_search_code/                           # Root directory of the analyzed project
├── app/                                      # Main application package (package_root)
├── tools/                                    # Internal developer tools and scripts
│   └── solid_verifier/                       # Root directory of the SOLID Verifier tool
│       ├── prompts/                          # External prompt templates and LLM response schema
│       │   ├── system.md                     # System prompt (expert role and base rules)
│       │   ├── user_base.md                  # Base user prompt (source code and context injection)
│       │   ├── user_ocp_section.md           # OCP-specific instructions for candidate verification
│       │   ├── user_lsp_section.md           # LSP-specific instructions for candidate verification
│       │   └── response_schema.json          # Strict JSON contract for model output
│       ├── tests/                            # Unit and integration tests
│       │   ├── fixtures/                     # Mock data and fake projects (sample_project)
│       │   └── llm/                          # Unit and E2E tests for LLM integration (Gateway, ACL)
│       ├── solid_dashboard/                  # Main Python package of the tool
│       │   ├── __main__.py                   # CLI entry point
│       │   ├── config.py                     # Parsing and validation of solid_config.json
│       │   ├── pipeline.py                   # Central orchestrator for static and LLM analysis
│       │   ├── schema.py                     # Data schemas for reports
│       │   ├── interfaces/                   # Python Abstract Base Classes / Protocols
│       │   │   └── analyzer.py               # Base IAnalyzer interface
│       │   ├── adapters/                     # Static analysis adapters
│       │   │   ├── radon_adapter.py          # radon + lizard (parameters, nesting depth)
│       │   │   ├── cohesion_adapter.py       # custom LCOM4 (ignores @property, strict AST traversal)
│       │   │   ├── import_graph_adapter.py   # import graph (grimp) + stability metrics
│       │   │   ├── import_linter_adapter.py  # CLI lint-imports + dynamic contract generation
│       │   │   └── pyan3_adapter.py          # call graph & dead code (safe, project-agnostic)
│       │   ├── llm/                          # Isolated LLM analysis and heuristics layer
│       │   │   ├── ast_parser.py             # AST parser building the ProjectMap
│       │   │   ├── heuristics.py             # Static heuristics (finding OCP/LSP candidates)
│       │   │   ├── llm_adapter.py            # LLM orchestrator
│       │   │   ├── gateway.py                # LlmGateway (cache, budget, retry)
│       │   │   ├── provider.py               # API Providers (OpenRouter) and ACL-A
│       │   │   ├── response_parser.py        # ACL-B: safe semantic parsing of LLM responses
│       │   │   ├── cache.py                  # LLM response caching based on prompt hashes
│       │   │   ├── budget.py                 # Token budget controller
│       │   │   └── types.py                  # Domain types (Finding, LlmCandidate, ParseResult)
│       │   └── report/                       # Report generation and processing module
│       │       ├── templates/                # Jinja2 templates for visual HTML reports
│       │       ├── differ.py                 # Logic for comparing current report with baseline
│       │       └── generator.py              # HTML dashboard rendering from JSON and templates
│       ├── .env                              # Local environment variables (OPENROUTER_API_KEY)
│       ├── .env.example                      # Example environment variables
│       ├── README.md                         # English documentation (this file)
│       ├── README.ru.md                      # Russian documentation
│       ├── pyproject.toml                    # Package metadata and build configuration
│       ├── pytest.ini                        # Test runner configuration
│       └── requirements.txt                  # Strict dependencies (radon, lizard, grimp, httpx, etc.)
├── solid_config.json                         # Single configuration point (layers, ignore_dirs, LLM)
└── run_solid_dashboard.py                    # Convenience wrapper script for pipeline execution
```

***

## Configuration (`solid_config.json`)

The `solid_config.json` file, located in the root of the analyzed project (`scopus_search_code/`), is the single point of configuration for the tool. Thanks to strict orchestration, the entire pipeline (both static and LLM) unquestioningly follows these routing rules.

- **`package_root`**: the root Python package to analyze (e.g., `app`). This is the **single point of target directory control** for all adapters.
- **`layers`**: a mapping of logical architectural layers to module prefixes. Used by both the import graph and the `import-linter` adapter.
- **`ignore_dirs`**: a global list of excluded directories (`.venv`, `__pycache__`, `tests`, `tools`, etc.). Ensures no adapter ever strays outside the core business logic.
- **`external_layers`**: a mapping of logical names for external dependencies.
- **`llm`**: LLM layer settings. The `api_key` parameter can (and should) be left as `null` — the adapter will automatically and safely load the `OPENROUTER_API_KEY` environment variable from the `.env` file.

```json
{
  "package_root": "app",
  "layers": {
    "routers": ["routers"],
    "services": ["services"],
    "infrastructure": ["infrastructure"],
    "models": ["models"],
    "interfaces": ["interfaces"]
  },
  "ignore_dirs": [".venv", "__pycache__", "tests", "tools"],
  "external_layers": {
    "db_libs": ["sqlalchemy"],
    "web_libs": ["fastapi", "starlette"]
  },
  "llm": {
    "enabled": true,
    "provider": "openrouter",
    "model": "CHOOSE_YOUR_MODEL_AT_https://openrouter.ai/models",
    "api_key": null,
    "endpoint": null,
    "max_tokens_per_run": 1000,
    "cache_dir": ".solid-cache/llm",
    "prompts_dir": "tools/solid_verifier/prompts"
  }
}
```

***

## Running the Dashboard

There are two main ways to run the tool.

### 1. Direct CLI launch

```bash
python -m tools.solid_verifier.solid_dashboard \
    --target-dir ./app \
    --config ./solid_config.json
```

This will:

1. Load `solid_config.json` from the project root.
2. Run all static adapters in sequence (`Radon`, `Cohesion`, `ImportGraph`, `ImportLinter`, `Pyan3`).
3. If `llm.enabled: true` and OCP/LSP candidates are found, perform LLM analysis via OpenRouter.
4. Print a JSON report to stdout and save it into `tools/solid_verifier/solid_dashboard/report/solid_report.log`.

### 2. Through the wrapper script

```bash
python run_solid_dashboard.py
```

The script hardcodes `target_dir = ./app` and `config_path = ./solid_config.json`, invokes the internal CLI, and forwards the exit code. It is convenient for Git hooks and CI pipelines.

### 3. Running tests

From the `tools/solid_verifier/` directory:

```bash
# All tests except those requiring a real external API
pytest -m "not manual" -vv

# Manual end-to-end test with the real OpenRouter API
# (requires OPENROUTER_API_KEY to be set)
pytest tests/llm/test_llm_adapter_manual.py -m manual -vv -s
```

### Preliminary setup

Before running the verifier, install dependencies and, if needed, configure the API key for the LLM layer:

```bash
pip install -r tools/solid_verifier/requirements.txt
```

Example environment variable:

```bash
export OPENROUTER_API_KEY=YOUR_SECRET_KEY
```

On Windows PowerShell:

```powershell
$Env:OPENROUTER_API_KEY="YOUR_SECRET_KEY"
```

***

## Dependencies

The project uses the following main libraries (declared in `tools\solid_verifier\requirements.txt`):

- **[radon](https://pypi.org/project/radon/)** (`>=6.0.1`) — cyclomatic complexity and maintainability metrics (MI, Halstead).
- **[pydeps](https://pypi.org/project/pydeps/)** (`>=1.12.0`) — import graph construction and dependency analysis.
- **[pydantic](https://pypi.org/project/pydantic/)** (`>=2.0.0`) — strict typing and validation of config, schemas, and reports.
- **[pyan3](https://pypi.org/project/pyan3/)** (`>=2.2,<3.0`) — static call-graph analyzer for modern Python versions (3.10–3.14).
- **[import-linter](https://pypi.org/project/import-linter/)** (`==2.11`) — enforcement of architectural contracts and layer isolation. The version is strictly pinned because the adapter parses unstructured console text output from the CLI tool, which may change in newer releases.
- **[lizard](https://pypi.org/project/lizard/)** (`==1.17.10`) — a lightweight analyzer used exclusively to extract two side metrics: parameter count (for ISP) and maximum nesting depth. The version is pinned to protect against changes in the internal structure of its AST objects.
- **[python-dotenv](https://pypi.org/project/python-dotenv/)** (`>=1.0.0`) — secure loading of secrets (like `OPENROUTER_API_KEY`) from a `.env` file into environment variables, preventing leaks into version control or JSON configs.
- **[httpx](https://pypi.org/project/httpx/)** (`>=0.24.0`) — HTTP client for interacting with LLM provider APIs (OpenRouter/OpenAI).
- **[Jinja2](https://pypi.org/project/Jinja2/)** — HTML report generation (in development).

All dependencies are pinned in `requirements.txt` to stable versions compatible with the current Python runtime.

***

## Roadmap

- **Near-term tasks**
  - **Integrate strict JSON schema for LLM:** transition OpenRouter models to guaranteed response generation in the `response_schema.json` format using modular prompt templates (`system.md`, `user_base.md`, etc.).
  - **Decompose heuristics monolith:** comprehensive refactoring of `heuristics.py` and `test_heuristics.py` (~1500 lines each). Split into 7 independent modules (one heuristic per file) and 7 corresponding test suites to radically improve readability, testability, and ease of adding new rules.

- **Mid-term tasks**
  - Implement `generator.py` and `differ.py` for rendering visual HTML dashboards and calculating diffs (degradations/improvements) against a stored baseline of metrics.
  - Extract the SOLID Verifier into an independent package installable globally via pip (`pip install solid-verifier`).
  - Develop a basic web interface or a VS Code extension for browsing reports and navigating metrics across local projects in real time.

- **Long-term tasks**
  - **LLM Mentoring for SRP and DIP:** expand the role of the LLM. Feed the neural network precise mathematical data from static analyzers (failed `import-linter` contracts and high LCOM4 scores) to automatically generate human-readable explanations and architectural refactoring suggestions.