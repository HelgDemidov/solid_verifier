# SOLID Verifier Dashboard

Russian version: [README.ru.md](README.ru.md)

`solid_dashboard` is a config‑driven CLI tool that analyzes Python projects for adherence to SOLID principles and layered architecture. It runs a pipeline of static analyzers, calculates metrics, checks import contracts, and produces a machine‑readable JSON report (with an optional human‑readable layer on top).

The tool is designed to be project‑agnostic: it can be reused across multiple Python codebases, not just the Scopus Search API.

---

## Key Features

- **Single entrypoint CLI** to run the whole analysis pipeline against a target project.
- **Config‑driven setup** via `solid_config.json`:
  - Definition of architectural layers and their module prefixes.
  - Definition of external "library layers" (DB, web, etc.).
  - Toggle and configure individual analyzers.
- **Static metrics**:
  - **Complexity & maintainability** via `radon` (CC, MI).
  - **Cohesion** via custom **LCOM4** adapter based on `ast`.
  - **Call graph & dead code** via `pyan3`.
- **Architecture & dependencies**:
  - **Import graph** based on `grimp` (the same engine used by `import-linter`).
  - **Layered import contracts** enforced by `import-linter` via CLI.
  - Stability metrics (Robert C. Martin: `Ca`, `Ce`, `Instability`) per layer.
  - Explicit modeling of **third‑party libraries** as separate layers (DB/web).
- **Extensible pipeline**:
  - Clean `IAnalyzer` interface implemented explicitly by all adapters.
  - Pluggable analyzers (planned: `llmadapter` for AI‑assisted SOLID analysis).
- **Machine‑readable report** in `solid_report.log` (JSON format) and planned visual HTML reports.

---

## Current Architecture & Adapters

The dashboard is implemented as a small internal framework of **adapters**. Each adapter:

- Inherits from a common `IAnalyzer` interface (`solid_dashboard.interfaces.analyzer.IAnalyzer`).
- Exposes a `name` property.
- Implements `run(target_dir, context, config) -> Dict[str, Any]`.

### Implemented adapters

- `radon_adapter.py`  
  Uses `radon` to compute cyclomatic complexity and Maintainability Index. Additionally integrates `lizard` to extract **only** `parameter_count` (ISP signal) and `max_nesting_depth` per function. Lizard's overlapping metrics (CCN/NLOC/MI) are deliberately ignored to keep `radon` as the single source of truth.

- `cohesion_adapter.py`  
  Custom, dependency‑free implementation of **LCOM4** using Python's `ast` module. Accurately handles modern Python features like `async`, `@property`, and Pydantic fields (`__annotations__`), explicitly ignoring `__init__` constructors for mathematical precision. Fully implements the `IAnalyzer` interface.

- `import_graph_adapter.py`  
  Builds the import graph using `grimp` (the same engine as `import-linter`). Groups modules into layers based on `solid_config.json`, identifies third‑party dependencies (DIP), and calculates Martin's Stability metrics (`Ca`, `Ce`, `Instability`) for each layer. All results are synchronized with the declared architecture in `solid_config.json`.

- `importlinteradapter.py`  
  Integrates with `import-linter` via the stable CLI (`lint-imports`) instead of internal Python APIs. It dynamically rewrites the `layers:` block in a temporary config based on `solid_config.json`, then runs the CLI as a subprocess with an isolated `PYTHONPATH`, parsing the output into structured JSON (contracts kept/broken, violations, raw console text).

- `pyan3adapter.py`  
  Uses `pyan3` to build a call graph, deduplicate edges, and identify potential **dead code**. FastAPI framework glue (like router endpoint functions) is filtered out to avoid false positives in dead‑code detection. Provides node/edge counts and the list of nodes/edges, again via the `IAnalyzer` interface.

### Planned adapters & Features

- `llmadapter.py` (Planned)  
  AI‑assisted analysis using OpenAI/Ollama APIs to evaluate higher‑level SOLID principles (OCP, LSP, ISP, SRP, DIP) based on context gathered by static adapters (import graph, call graph, cohesion, etc.). Will return structured findings with confidence scores and severity.

- `differ.py` & `generator.py` (In Progress)  
  Tools to render the JSON output into a visual HTML dashboard (Jinja2 templates) and to track metric degradation or improvement over time by diffing current reports against a stored baseline.

---

## Repository Layout

Below is the file structure of the project highlighting the components of the SOLID Verifier in its current location and naming:

```text
scopus_search_code/                           # Root directory of the analyzed project
├── app/                                      # Main FastAPI application package
│   ├── routers/
│   ├── services/
│   ├── infrastructure/
│   ├── models/
│   └── ...
├── tools/                                    # Internal developer tools and scripts
│   └── solid_verifier/                       # Root directory of the SOLID Verifier tool
│       ├── tests/                            # Unit tests for the verifier itself
│       │   └── fixtures/                     # Mock data and test files
│       │       └── sample_project            # A dummy Python project used to test AST parsers
│       ├── solid_dashboard/                  # Main Python package for the tool
│       │   ├── __main__.py                   # CLI entrypoint (python -m tools.solid_verifier.solid_dashboard)
│       │   ├── config.py                     # Logic for parsing and validating solid_config.json
│       │   ├── pipeline.py                   # Main runner that orchestrates all adapters
│       │   ├── schema.py                     # Data models/schemas for reports (e.g., Pydantic / TypedDict)
│       │   ├── interfaces/                   # Abstract base classes / protocols
│       │   │   └── analyzer.py               # Base IAnalyzer interface for all adapters
│       │   ├── adapters/                     # Implementations of specific analysis tools
│       │   │   ├── radon_adapter.py          # radon + lizard (param_count, nesting), IAnalyzer-based
│       │   │   ├── cohesion_adapter.py       # custom AST-based LCOM4, IAnalyzer-based
│       │   │   ├── import_graph_adapter.py   # grimp-based import graph + stability, IAnalyzer-based
│       │   │   ├── importlinteradapter.py    # lint-imports CLI + layer sync, IAnalyzer-based
│       │   │   ├── pyan3adapter.py           # call graph & dead code, IAnalyzer-based
│       │   │   └── llmadapter.py             # (planned) AI-assisted SOLID analysis
│       │   └── report/                       # Report generation and processing module
│       │       ├── templates/                # Jinja2 templates for visual reports
│       │       │   └── report.html.j2        # Base HTML template for the dashboard
│       │       ├── solid_report.log          # The latest generated machine-readable JSON report
│       │       ├── differ.py                 # (WIP) Logic to compare current report with a baseline
│       │       └── generator.py              # (WIP) Logic to render HTML from JSON and templates
│       ├── README.md                         # English documentation for the verifier (this file)
│       ├── README.ru.md                      # Russian documentation for the verifier
│       ├── pyproject.toml                    # Package metadata and build system configuration
│       └── requirements.txt                  # Strict dependencies (radon, lizard, grimp, etc.)
├── .importlinter                             # Base import-linter config (used as template)
├── solid_config.json                         # SOLID Dashboard configuration for this project (layers, thresholds)
└── run_solid_dashboard.py                    # Convenience wrapper script to run the pipeline
```

---

## Configuration (`solid_config.json`)

`solid_config.json` lives in the root of the analyzed project (`scopus_search_code/`) and describes how the dashboard should interpret the project structure:

- `package_root`: root Python package to analyze (e.g. `app`).
- `layers`: mapping of logical layers to module prefixes (relative to `package_root`), e.g.:

  ```json
  {
    "package_root": "app",
    "layers": {
      "routers": ["routers"],
      "services": ["services"],
      "infrastructure": ["infrastructure"],
      "models": ["models"]
    },
    "ignore_dirs": ["__pycache__", ".venv", "tests"],
    "external_layers": {
      "db_libs": ["sqlalchemy"],
      "web_libs": ["fastapi", "starlette"]
    }
  }
  ```

These settings drive both:

- `import_graph_adapter.py` (for visualization and stability metrics).
- `importlinteradapter.py` (for generating the `layers:` contract consumed by `lint-imports`).

---

## Running the Dashboard

There are two main ways to run the pipeline.

### 1. Directly via the internal CLI

From the repository root (`scopus_search_code/`), with the virtual environment activated and dependencies installed:

```bash
python -m tools.solid_verifier.solid_dashboard \
    --target-dir ./app \
    --config ./solid_config.json
```

This:

1. Loads `solid_config.json` from the project root.
2. Runs all configured adapters in sequence (Radon, Cohesion, Import Graph, Import Linter, Pyan3).
3. Prints the JSON report to stdout.
4. Writes the same JSON to `tools/solid_verifier/solid_dashboard/report/solid_report.log`.

### 2. Using the convenience wrapper script

From the same project root:

```bash
python run_solid_dashboard.py
```

The script:

1. Resolves `target_dir` as `./app` and `config_path` as `./solid_config.json`.
2. Invokes the internal CLI:

   ```bash
   python -m tools.solid_verifier.solid_dashboard \
       --target-dir ./app \
       --config ./solid_config.json
   ```

3. Forwards the exit code of the pipeline process.

This keeps the usage stable even if the internal package path (`tools.solid_verifier.solid_dashboard`) changes in the future.

---

## Dependencies

Core third‑party libraries used by the dashboard:

- [`radon`](https://pypi.org/project/radon/) — complexity & maintainability metrics.
- [`lizard`](https://pypi.org/project/lizard/) — function parameter counts and nesting depth only.
- [`grimp`](https://github.com/seddonym/grimp) — import graph construction.
- [`import-linter`](https://pypi.org/project/import-linter/) — enforcement of layered contracts via CLI.
- [`pyan3`](https://pypi.org/project/pyan3/) — call graph and dead‑code analysis.
- [`jinja2`](https://pypi.org/project/Jinja2/) — HTML report generation (planned).
- [`httpx`] (>=0.24.0): Synchronous HTTP client with granular timeout control, utilized by the `llmadapter` for executing robust HTTP calls to external and local LLM providers (OpenAI, Ollama).

All are pinned via `requirements.txt` to stable versions compatible with the current Python runtime.

---

## Roadmap

- **Short term**
  - Finalize documentation in both English and Russian.
  - Stabilize the JSON schema of `solid_report.log` for external consumers.
- **Medium term**
  - Implement `llmadapter.py` for AI‑assisted OCP/LSP/ISP/DIP analysis.
  - Implement `generator.py` and `differ.py` for HTML dashboard and baseline diffing.
- **Long term**
  - Extract SOLID Verifier into a standalone, pip‑installable package.
  - Provide a basic web UI or VS Code extension for browsing reports across multiple projects.