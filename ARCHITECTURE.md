# SOLID Verifier: архитектурное и техническое видение

***

## Суть проекта

SOLID Verifier — это **трёхслойный статический аналитический пайплайн** для Python-кодовой базы, запускаемый CLI и выдающий JSON-отчёт о нарушениях OCP и LSP (плюс архитектурных принципов — SDP, SLP). Три слоя:

1. **Статические адаптеры** (5 штук) — метрики без семантики
2. **Эвристический слой** (7 AST-эвристик) — фильтрация кандидатов на нарушение
3. **LLM-слой** (OpenRouter / gpt-4o-mini) — семантическая верификация только отфильтрованных кандидатов

«Тренировочный объект» — `app/` (FastAPI, 5 слоёв: routers → services → infrastructure → interfaces → models + crosscutting: core, schemas).

***

## Полная карта файлов: финальный статус

### Корневые файлы репозитория

| Файл | Назначение | Статус |
|---|---|---|
| `solid_config.json` | Единый источник конфигурации: слои, LLM, tolerance, exceptions, ignore_dirs | ✅ Полный, актуальный |
| `run_solid_dashboard.py` | CLI-обёртка: запуск `python -m solid_dashboard` через subprocess с правильным cwd | ✅ Работает |
| `run_export_skeleton.py` | Тонкий делегатор: вызывает `docs/export_skeleton.py` — инструмент экспорта дерева файлов для документирования | ✅ Реализован, служебный скрипт вне основного пайплайна |
| `.importlinter` | Базовый шаблон конфига import-linter: контракт `Scopus API layered architecture`, 5 слоёв | ✅ Актуален; `ImportLinterAdapter` динамически генерирует из него временный `_auto`-файл |

### `solid_dashboard/` — пакет верификатора

#### Точки входа и оркестрация

| Файл | Статус |
|---|---|
| `__main__.py` | ✅ Полный. CLI-точка: argparse, load_dotenv, адаптеры, pipeline, JSON → `report/solid_report.log` |
| `pipeline.py` | ✅ Полный. Оркестратор: последовательный запуск 6 адаптеров → context → LLM |
| `config.py` | ✅ Полный. Загрузка JSON, `load_llm_config()`, валидация `layer_order` |
| `schema.py` | ✅ Изучен. **Важное наблюдение**: содержит Pydantic-схемы `RadonResult`, `CohesionResult`, `PydepsResult` — но `PydepsResult` является **артефактом прошлого** (до рефакторинга, когда был pydeps). Сейчас не используется ни в одном адаптере; `PydepsResult.edges` типизирован через `Dict[str, Any]`, что указывает на незавершённую уборку. |

#### Адаптеры

| Адаптер | Что делает | Ключевые детали | Статус |
|---|---|---|---|
| `RadonAdapter` | CC через `radon cc --json` + MI через `radon mi --json` (два независимых subprocess) + обогащение `parameter_count` через Lizard | **Два subprocess:** `_run_mi()` вызывается после CC, использует те же `ignore_dirs`. Сбой MI изолирован: возвращает `{}`, не ломает CC-результат. Сортировка: CC-список → по сложности DESC; MI-список → по MI ASC (худшие первыми). `maintainability` — под-объект в возвращаемом dict. Lizard — опциональная зависимость (`LIZARD_AVAILABLE`). Ошибки индексации → `RuntimeWarning`, не исключение | ✅ Полный |
| `CohesionAdapter` | LCOM4 через двухпроходной собственный AST | Pass 1: сбор классов, их методов, атрибутов. Pass 2: обогащение атрибутами предков через MRO. `_MethodUsageVisitor`. DFS-подсчёт компонент. Использует `adapters/class_classifier.py` | ✅ Полный |
| `ImportGraphAdapter` | Граф слоёв через `grimp`, метрики Ca/Ce/I (Martin Stability), SDP violations (SDP-001), Skip-Layer violations (SLP-001) | Два детектора: `_detect_sdp_violations()` и `_detect_skip_layer_violations()`. `utility_layers` (core, schemas) — в графе, но tier=None → fail-silent в обоих детекторах. `sdp_tolerance=0.10` из конфига. `allowed_dependency_exceptions` = models→db_libs (SQLAlchemy). Два поля `evidence` в нарушениях зарезервированы для будущей агрегации | ✅ Полный |
| `ImportLinterAdapter` | Проверка контрактов слоёв через `lint-imports` CLI | Динамически генерирует `.importlinter_auto_<package>` из базового `.importlinter`, заменяя `root_packages` и `layers`. `layer_order` — единственный источник истины для порядка. Атомарная запись через `.tmp`. `unmatched_ignore_imports=warn` — страховочный слой. Парсит `violation_details: List[Dict]` — готово для будущего `report_aggregator` | ✅ Полный |
| `Pyan3Adapter` | Граф вызовов через `pyan3 --uses --no-defines --text --quiet` | **Confidence-система (Вариант B)**: двухпроходной алгоритм. Pass 1: `_detect_suspicious_blocks()` — Counter по `[U]`-именам, кратное вхождение = name collision. Pass 2: парсинг рёбер, confidence=`low` если source suspicious; цель ребра на confidence не влияет. Де-дупликация рёбер: пессимистичная стратегия — если хотя бы одно дублирующееся ребро `"low"`, результат `"low"`. Разделение на `root_nodes` (нет входящих, есть исходящие) и `dead_nodes` (ни входящих ни исходящих). Санити-чек: nodes > 0 но edges = 0 → `RuntimeWarning` | ✅ Полный |
| `HeuristicsAdapter` | Двухканальный мост к LLM | Строит `ProjectMap` через `ast_parser.py`, прогоняет 7 эвристик через `_runner.py`, пишет runtime-объекты в `context["heuristics"]` (для pipeline), JSON-summary в `return`. Порядок адаптеров в пайплайне критичен: HeuristicsAdapter должен быть последним перед LLM | ✅ Полный |

#### Два классификатора классов: полное сравнение

Это подтверждённый архитектурный долг:

| Характеристика | `adapters/class_classifier.py` | `llm/analysis/class_role.py` |
|---|---|---|
| Потребитель | `CohesionAdapter` | Все 7 OCP-эвристик |
| Сигнатура | `classify_class(class_node) → str` | `classify_class(class_node, import_aliases) → ClassRole` |
| Возвращаемый тип | строки: `"interface"`, `"abstract"`, `"dataclass"`, `"concrete"` | `Enum`: `PURE_INTERFACE`, `INFRA_MODEL`, `CONFIG`, `DOMAIN` |
| import_aliases | Нет | Да (для разрешения `from pydantic import BaseModel as BM`) |
| InfraScore | Нет | Да: 5 сигналов, порог ≥2 для `INFRA_MODEL` |
| Назначение | Фильтр для LCOM4: пропустить интерфейсы и датаклассы | Фильтр для OCP: пропустить INFRA_MODEL и CONFIG |
| Конфликт классификации? | Теоретически возможен для edge-случаев | — |

#### `llm/` — LLM-слой

##### `llm/analysis/`

| Файл | Ключевые детали | Статус |
|---|---|---|
| `ast_parser.py` | **Двухпроходной AST-парсер** для `ProjectMap`. Pass 1: все файлы → `ClassInfo` и `InterfaceInfo` (ABC, Protocol). Pass 2: заполняет `implemented_interfaces`, пересчитывает `is_override` для каждого метода. `ProjectMap.classes: Dict[str, ClassInfo]`, `ProjectMap.interfaces: Dict[str, InterfaceInfo]` | ✅ Полный |
| `class_role.py` | `ClassRole` Enum + `InfraScore`. Сигналы InfraScore: декоратор `table_name`/`__tablename__`, база `Base`/`DeclarativeBase`, `BaseModel`, `BaseSettings`, `TypedDict`. Порог ≥2 = `INFRA_MODEL`. `CONFIG` = только `BaseSettings`/`AppConfig`. `PURE_INTERFACE` = ABC/Protocol без конкретных методов | ✅ Полный |

##### `llm/heuristics/` — полная логика всех 7 эвристик

**OCP-H-001**: `if/elif`-цепочка с **≥3 ветвями, содержащими `isinstance()`**. Считает именно isinstance-ветви, не общую длину цепочки. Исключает `INFRA_MODEL` и `CONFIG`. Один Finding на метод.

**OCP-H-002**: `match/case` (Python 3.10+) с **≥3 type-ветвями** (`ast.MatchClass`). Поддержка `MatchOr` (каждый подпаттерн считается). Graceful degradation на Python < 3.10 через `getattr(ast, "Match", None)`.

**OCP-H-003**: **Намеренно удалена**. Эвристика существовала, затем была удалена в процессе рефакторинга. Номерация сохранена для документирования истории решений.

**OCP-H-004**: CC метода **≥5** (`_compute_method_cc`) **И** наличие `isinstance()` в теле. Использует `_iter_method_nodes` для корректного обхода без захвата вложенных функций.

**LSP-H-001**: Переопределённый метод (`is_override=True` из `ProjectMap`) **бросает `NotImplementedError`**. Исключает абстрактные классы через `_is_abstract_class(class_info, project_map)`.

**LSP-H-002**: Переопределённый метод с **пустым телом** (только `pass` или docstring-only). Тихое нарушение, более опасное чем LSP-H-001. Исключает абстрактные классы.

**LSP-H-003**: Метод с параметром, аннотированным **базовым типом из `ProjectMap`**, при этом внутри метода есть **`isinstance(param, ...)`**. Поддержка только простых `ast.Name`-аннотаций (не Union/Optional).

**LSP-H-004**: `__init__` подкласса **без вызова `super().__init__()`**. Исключения: `@dataclass`, классы из `_LSP_H004_EXCLUDED_PARENTS` (object, ABC, Protocol, TypedDict, NamedTuple, BaseModel), родители с ролью `PURE_INTERFACE`. Функция `_parent_is_pure_interface()` парсит AST родителя из `project_map.classes[parent_name].source_code` — это ограниченная реализация: `InterfaceInfo` без `source_code` не поддерживается (всегда `return False`).

##### `llm/heuristics/_runner.py` — дедупликация и приоритизация

`identify_candidates()`:
- Прогоняет все 7 эвристик на каждый класс из `ProjectMap`
- **Дедупликация findings**: по `(rule, file, class_name)` — сохраняется только первый
- **Дедупликация candidates**: по `(file, class_name)` — `candidate_type` выбирается по приоритету `both > ocp > lsp`
- `priority` = `(количество findings) + (1 если has_hierarchy)`
- Кандидат с `has_hierarchy=True` добавляется даже без findings (LLM получит и "чистые" иерархии)

##### `llm/llm_client/` — инфраструктура LLM

**`provider.py`** (`OpenRouterProvider`): HTTP через `httpx`. ACL-A барьер `_parse_success()` — 9 последовательных шагов извлечения контента. HTTP-классификация: `RetryableError` (5xx, 429, timeout), `NonRetryableError` (400, 401, 403, 404, невалидный JSON).

**`gateway.py`** (`LlmGateway`): SHA256-ключ кэша из (messages, options). `cache.get()` → если hit: `tokens_used=0`. Если miss: `budget.is_exhausted()` → если да: `BudgetExhaustedError`. Иначе: retry-цикл (3 попытки, delays 2s/5s). После успеха: `budget.record_tokens()`, `cache.set()`.

**`budget.py`** (`TokenBudgetController`): `max_tokens=3000`, `used_tokens` счётчик. `max_tokens≤0` = неограниченный бюджет. Контроллер сам не бросает исключений — это задача Gateway.

**`cache.py`** (`FileCache`): `<cache_dir>/<sha256_key>.json`. Атомарная запись через `.tmp` → `replace()`. Сериализует только `content`, `tokens_used`, `model` (raw не сохраняется — часть ACL-дизайна).

**`interfaces.py`** (`LlmCache`): Protocol для кэша. Позволяет подменить FileCache на Redis/in-memory без изменения Gateway.

**`errors.py`**: иерархия `LlmError` → `RetryableError`, `NonRetryableError`, `BudgetExhaustedError`, `LlmUnavailableError`. `BudgetExhaustedError` формирует строку `"used X out of Y"`.

**`factory.py`** (`create_llm_adapter()`): `Provider → FileCache → TokenBudgetController → Gateway → LlmSolidAdapter`. Fail-fast валидация `api_key`.

**`llm_adapter.py`** (`LlmSolidAdapter`): Полная ACL-B реализация. `_extract_json_content()` — 3 попытки: прямой парсинг, вырезка из ````json ... ````, поиск первого `{` и последнего `}`. `_validate_structure()` — наличие `findings: list`. `_validate_finding()` — `message` обязателен, `severity` нормализуется до `warning` при неизвестном значении, `principle` = fallback из `candidate_type`, `rule` генерируется внутри (`OCP-LLM-001`, `LSP-LLM-001`), поддержка поля `details` как синонима `explanation` (обратная совместимость). `ParseResult.status`: `success | partial | failure`.

##### Промпты (`tools/solid_verifier/prompts/`)

| Файл | Содержимое |
|---|---|
| `system.md` | Роль: "expert Python code reviewer specializing in SOLID". Инструкция: консервативный режим, только явные доказательства из кода, не изобретать контракты. OCP-фокус: нужно ли изменять код при добавлении нового поведения. LSP-фокус: behavioral substitutability, preconditions/postconditions/invariants. |
| `user_base.md` | Шаблон с 4 переменными: `{candidate_type}`, `{class_name}`, `{file_path}`, `{source_code}`. Инструкция: только кандидат, только OCP/LSP, только конкретные улики, не спекулировать. |
| `user_ocp_section.md` | Дополнительная OCP-секция (прочитана, но заблокирована системой безопасности при передаче). Логика: добавляется только для `candidate_type in ("ocp", "both")`. |
| `user_lsp_section.md` | Дополнительная LSP-секция. Аналогично для `candidate_type in ("lsp", "both")`. |
| `response_schema.json` | `{"instruction": "Output ONLY valid JSON...", "expected_output": {...}}`. LLM получает только поле `instruction`. Пример в `expected_output` использует `"details"` как поле — это объясняет, почему `_validate_finding()` поддерживает оба имени (`explanation` и `details`). **Важно**: `rule` в примере = `"OCP-Violation-TypeBranching"`, но код LlmSolidAdapter игнорирует `rule` из LLM-ответа и генерирует его сам как `OCP-LLM-001`/`LSP-LLM-001`. |

#### Тесты

Структура: `tests/test_cohesion_adapter/` (10 файлов, включая `test_run_integration.py` 27 KB) и `tests/llm/`. Отдельные файлы: `test_import_graph_adapter.py` (13 KB), `test_import_linter_adapter.py` (12 KB). Покрытие — **в основном `CohesionAdapter` и адаптеры статического анализа**. Тесты для LLM-слоя (`tests/llm/`) — директория существует, содержимое не читал.

#### `pyproject.toml` и `requirements.txt`

Зависимости проекта: `radon`, `lizard`, `grimp`, `import-linter`, `pyan3`, `httpx`, `python-dotenv`, `pydantic`, `networkx`, `jinja2`. Для тестов: `pytest`. Проект оформлен как `pyproject.toml`-пакет со стандартной структурой.

***

## Поток данных: финальная точная версия

```
[ПОЛЬЗОВАТЕЛЬ]
  python run_solid_dashboard.py (из корня scopus_search_code)
     └─ subprocess: python -m solid_dashboard
           --target-dir <root>
           --config solid_config.json
        cwd = tools/solid_verifier/

[solid_dashboard/__main__.py]
  load_dotenv()                 # OPENROUTER_API_KEY из .env
  load_config("solid_config.json")
  adapters = [Radon, Cohesion, ImportGraph, ImportLinter, Pyan3, Heuristics]
  run_pipeline(target_dir=<root>, config, adapters)

[pipeline.py::run_pipeline()]
  analysis_root = root / "app"  ← config["package_root"] = "app"
  context = {}

  1. RadonAdapter.run(app/, context, config)
     → subprocess: radon cc --json  → items[], mean_cc, high_complexity_count
     → subprocess: radon mi --json  → maintainability{total_files, mean_mi, low_mi_count, files[]}
                                       (при сбое: maintainability = {})
     → {items, mean_cc, high_complexity_count, maintainability, lizard_used}
     context["radon"] = result

  2. CohesionAdapter.run(app/, context, config)
     → {classes: [...], mean_cohesion, low_cohesion_count}
     context["cohesion"] = result

  3. ImportGraphAdapter.run(app/, context, config)
     → {nodes, edges, violations: [SDP-001..., SLP-001...]}
     context["import_graph"] = result

  4. ImportLinterAdapter.run(app/, context, config)
     генерирует .importlinter_auto_app →
     lint-imports --config .importlinter_auto_app →
     → {is_success, violations, violation_details}
     удаляет .importlinter_auto_app
     context["import_linter"] = result

  5. Pyan3Adapter.run(app/, context, config)
     pyan3 [файлы] --uses --no-defines --text --quiet →
     → {nodes, edges (с confidence), dead_nodes, root_nodes}
     context["pyan3"] = result

  6. HeuristicsAdapter.run(app/, context, config)
     ast_parser.py → ProjectMap (двухпроходной AST)
     _runner.identify_candidates(ProjectMap) → [LlmCandidate]
     context["heuristics"] = {
       "project_map": <ProjectMap>,   ← runtime-объект для LLM
       "candidates": [...]
     }
     return {findings_count, candidates_count, ...}  ← JSON-summary

  [LLM enabled=True]
  llm_config = load_llm_config(config)  # model=gpt-4o-mini, max_tokens=3000
  adapter = create_llm_adapter(llm_config)  # DI-фабрика
  input = LlmAnalysisInput(
    project_map = context["heuristics"]["project_map"],
    candidates  = context["heuristics"]["candidates"]
  )
  output = adapter.analyze(input)
  # Для каждого кандидата (по убыванию priority):
  #   1. _build_context() → {class_name, file_path, source_code, candidate_type}
  #      (project_map ИГНОРИРУЕТСЯ — задокументированная заглушка)
  #   2. _build_prompt_and_options() → system.md + user_base.md + [ocp/lsp секция] + schema
  #   3. gateway.analyze(messages, options)
  #      → cache.get(sha256_key) → hit: LlmResponse(tokens_used=0)
  #      → miss: budget.is_exhausted()? → BudgetExhaustedError
  #             provider.call() [retry 3x] → LlmResponse
  #             budget.record_tokens(), cache.set()
  #   4. _parse_response() → ParseResult{findings, status, warnings}
  results["llm"] = output

[__main__.py продолжение]
  _to_jsonable(results)   # рекурсивная нормализация dataclass → dict
  json.dumps(report, indent=2)
  print(report)
  save → report/solid_report.log
  save → report/solid_pipeline.log  (logging handlers)
```

***

## Архитектурные факты: полный список

**Подтверждённые:**

1. **`schema.py` содержит `PydepsResult`** — артефакт до-рефакторинговой эпохи (когда был pydeps-адаптер). Класс нигде не используется, не удалён. Технический долг.

2. **`schema.py` теперь содержит `MaintainabilityFileMetrics` и `MaintainabilityResult`** — Pydantic-схемы для поуфайлового MI-отчёта. `RadonResult` расширен двумя опциональными полями: `maintainability: Optional[MaintainabilityResult] = None` и `lizard_used: bool = False`. Поле `maintainability` может быть `None` в Pydantic-схеме или пустым `{}` в сыром dict — это нормальное состояние при сбое `radon mi`.

3. **Два классификатора**: `adapters/class_classifier.py` (4 категории-строки, без import_aliases) и `llm/analysis/class_role.py` (4 Enum-роли, InfraScore, import_aliases). Разные API, разная семантика, потенциально разная классификация edge-cases.

4. **`_build_context()` — документированная заглушка**: LLM видит только source_code кандидата в изоляции. `project_map` передаётся в метод, но игнорируется. Комментарий в коде прямо говорит о будущем расширении.

5. **`LSP-H-004._parent_is_pure_interface()`** — частичная заглушка для `InterfaceInfo`: класс из `project_map.interfaces` не может быть верифицирован через PURE_INTERFACE (нет `source_code`), метод возвращает `False`. Это означает: если родитель — зарегистрированный Protocol без source_code, LSP-H-004 может выдать ложный Finding.

6. **`response_schema.json` содержит пример с `"rule": "OCP-Violation-TypeBranching"`**, тогда как код игнорирует `rule` из LLM-ответа. Если модель ориентируется на пример, она напишет нестандартный rule-код, который молча выбрасывается.

7. **`max_tokens_per_run = 3000`** — тестовый лимит. Бюджет проверяется **до** запроса, не после: исчерпание бюджета означает, что оставшиеся кандидаты вообще не попадут к LLM. Порядок обработки — по убыванию `priority`.

8. **OCP-H-003 удалена намеренно**. Номерация `001, 002, [003 удалена], 004` сохранена как документация истории.

9. **`report/` директория** — место хранения `solid_report.log` (JSON-отчёт) и `solid_pipeline.log` (логи выполнения). Дополнительных рендереров (HTML, Markdown) нет — только сырые `.log`-файлы.

10. **`.env.example`**: `OPENROUTER_API_KEY=your_key_here` — единственная внешняя секрет. `api_key: null` в `solid_config.json` означает: ключ читается исключительно из `.env`/переменной окружения.

11. **Тесты сосредоточены на `CohesionAdapter`** (10 файлов, глубокое покрытие включая MRO-enrichment и LCOM4 edge-cases), `ImportGraphAdapter` и `ImportLinterAdapter`. Тесты для LLM-слоя и эвристик — директория `tests/llm/` существует, детали не читал.

***
