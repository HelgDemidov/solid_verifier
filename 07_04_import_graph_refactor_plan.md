# Итоговый план рефакторинга `ImportGraphAdapter`

> **Версия 2 — с учётом аудита внутренней совместимости (07.04.2026)**
> Этот документ — абсолютная точка истины для всего процесса реализации.
> Перед началом каждого коммита сверяться с соответствующим блоком.

---

## Контекст и принципы

Два ключевых принципа, которые пронизывают весь план:

- **Разделение труда**: Import Graph = слоевая семантика + правила; Pyan3 = доказательная база символов
- **Backward compatibility**: при отсутствии `layer_order` адаптер работает как сейчас, без исключений и шума

---

## Блок 0 — Изменения конфигурации

**Файл:** `solid_config.json`

Добавляется секция `layer_order` в блок `import_graph` в формате tier-based (Вариант B).
Порядок тиров — от наиболее нестабильного (индекс 0) к наиболее стабильному (последний индекс).

```json
"import_graph": {
    "layers": { ... },
    "external_layers": { ... },
    "layer_order": [
        ["routers"],
        ["services", "infrastructure"],
        ["interfaces"],
        ["models"],
        ["db_libs", "web_libs"]
    ],
    "interface_layers": ["interfaces"],
    "sdp_tolerance": 0.0
}
```

**`sdp_tolerance`** — пользовательски настраиваемый параметр (float, дефолт `0.0`).
Нарушение SDP фиксируется если `I(target) - I(source) > sdp_tolerance`.
При `0.0` — абсолютно строгий режим.

**`interface_layers`** — список имён слоёв, которые считаются interface-слоями
для целей skip-layer severity-градации. Дефолт: `["interfaces"]`.
Пользователь может указать любые имена: `["interfaces", "ports", "abstractions"]`.
Разделение на поле конфига (а не hardcode) устраняет хрупкость при переименовании слоёв.

> ⚠️ **Аудит-поправка #3:** поле `interface_layers` обязательно выносится в конфиг —
> не hardcode в коде. `_get_interface_layer_names()` читает его с дефолтом `["interfaces"]`.

---

## Блок 1 — Структура данных `violations` (scaffold)

**Файл:** `import_graph_adapter.py`

Вводится схема одного нарушения. Добавляется в выходной словарь `run()` как поле `"violations": []`.

```python
# Схема нарушения (расширенная, с зарезервированным evidence)
{
    "rule": "SDP_VIOLATION",          # str: SDP_VIOLATION | SKIP_LAYER
    "source_layer": "routers",        # str
    "target_layer": "infrastructure", # str
    "severity": "warning",            # str: warning | error
    "details": "I(routers)=1.0, I(infrastructure)=0.75 — ...",
    "evidence": []                    # list: зарезервировано для pyan3_edges (сейчас всегда [])
}
```

Метод `run()` расширяется: `violations` собирается из результатов обоих детекторов и
добавляется в возвращаемый dict рядом с `nodes` и `edges`.

> ⚠️ **Аудит-поправка #5:** одно ребро может одновременно дать нарушения от обоих
> детекторов (SDP_VIOLATION + SKIP_LAYER). Это **корректное и намеренное поведение** —
> разные правила независимы. Список `violations` **не дедуплицируется** по рёбрам.
> Пример: ребро `interfaces → routers` нарушает и SDP (стабильный → нестабильный),
> и SKIP_LAYER (обратное направление через тиры) — два separate violation-объекта.

---

## Блок 2 — Tier-резолвер (`_resolve_tier_map`)

**Файл:** `import_graph_adapter.py`

Приватный метод, вызывается один раз в `run()` до вызова детекторов.
Возвращает `dict[str, int]` — маппинг `layer_name → tier_index`.

**Логика парсинга `layer_order`:**
- Если список списков (Вариант B): каждый вложенный список получает свой индекс тира
- Если плоский список строк (Вариант A, fallback): каждый элемент = отдельный тир
- Если отсутствует в конфиге: возвращает `{}`, логирует `debug`

**Обработка `external_layers`:**

> ⚠️ **Аудит-поправка #2 (КРИТИЧНО):** `external_layers` из конфига **не обязаны** дублироваться
> в `layer_order`. `_resolve_tier_map()` автоматически добавляет все `external_layers` на тир
> `max_tier + 1` — если они **отсутствуют** в `layer_order`.
> Если пользователь явно включил `external_layers` в `layer_order` — используется
> указанный тир (явное > автоматическое). Это устраняет DRY-нарушение в конфиге.

```
Пример автоматического поведения:
  layer_order = [["routers"], ["services"], ["models"]]
  external_layers = {"db_libs": ..., "web_libs": ...}

  Результат tier_map:
    routers  → 0
    services → 1
    models   → 2
    db_libs  → 3   ← auto-assigned (max_tier=2, +1)
    web_libs → 3   ← auto-assigned
```

**Вспомогательный метод `_get_interface_layer_names()`:**
Читает `config["import_graph"].get("interface_layers", ["interfaces"])`.
Возвращает `set[str]`. Используется skip-layer детектором для severity-градации.

---

## Блок 3 — Детектор нарушений SDP (`_detect_sdp_violations`)

**Файл:** `import_graph_adapter.py`

**Сигнатура:**
```python
def _detect_sdp_violations(
    self,
    layer_edges: set[tuple[str, str]],
    instability_map: dict[str, float],   # {layer_name: instability_value}
    sdp_tolerance: float,
) -> list[dict]: ...
```

> ⚠️ **Аудит-поправка #4:** метод принимает готовый `instability_map: dict[str, float]`,
> а не `nodes: list[dict]`. Это упрощает изолированное тестирование и снимает
> зависимость детектора от внутренней структуры node-словаря.

**Алгоритм:**
```
instability_map строится в run() перед вызовом детекторов:
    instability_map = {n["id"]: n["instability"] for n in nodes}

для каждого ребра (source, target) в layer_edges:
    I_source = instability_map.get(source)
    I_target = instability_map.get(target)
    если любое из значений None → пропустить (неизвестный слой)

    если I_target - I_source > sdp_tolerance:
        → нарушение SDP
        severity = "warning"
        details = f"I({source})={I_source:.2f}, I({target})={I_target:.2f} — зависимость от более нестабильного слоя"
```

> ⚠️ **Аудит-поправка #1 — направление условия:**
> Условие `I(target) - I(source) > tolerance` означает:
> «source зависит от target, который нестабильнее source» — это и есть нарушение SDP.
> **Пример-якорь (не инвертировать!):**
> `routers (I=1.0) → interfaces (I=0.33)`: `0.33 - 1.0 = -0.67` → не нарушение ✅
> `models (I=0.2) → services (I=0.8)`: `0.8 - 0.2 = 0.6 > 0` → НАРУШЕНИЕ ❌
> При реализации явно оставить этот пример в docstring метода.

**Граничный случай — слои с `I=0.0`:**
Ребро `routers (I=1.0) → db_libs (I=0.0)`: `0.0 - 1.0 = -1.0` — не нарушает SDP математически.
SDP-детектор это ребро **пропускает**. Skip-layer детектор **поймает** его как пропуск тиров.
Разделение труда между детекторами работает корректно.

---

## Блок 4 — Детектор skip-layer (`_detect_skip_layer_violations`)

**Файл:** `import_graph_adapter.py`

**Сигнатура:**
```python
def _detect_skip_layer_violations(
    self,
    layer_edges: set[tuple[str, str]],
    tier_map: dict[str, int],
    interface_layers: set[str],
) -> list[dict]: ...
```

**Алгоритм:**
```
если tier_map пустой → return [], log.debug("layer_order not configured, skip-layer detection skipped")

для каждого ребра (source, target) в layer_edges:
    t_source = tier_map.get(source)
    t_target = tier_map.get(target)
    если t_source или t_target равны None → пропустить (слой не в tier_map)

    пропущено_тиров = t_target - t_source - 1
    если пропущено_тиров > 0:
        пропущенные_тиры = тиры с индексами от t_source+1 до t_target-1 включительно
        # tier_to_layers строится в run() из tier_map (инверсия)
        пропущенные_слои = все слои в этих тирах

        если пропущенные_слои ∩ interface_layers не пустое:
            severity = "error"
            skipped = пропущенные_слои ∩ interface_layers
            details = f"Пропущен interface-слой: {sorted(skipped)}"
        иначе:
            severity = "warning"
            details = f"Пропущено {пропущено_тиров} тир(а) между '{source}' и '{target}'"

        → добавить SKIP_LAYER violation
```

**Пример:**
```
routers (tier=0) → db_libs (tier=4 — auto-assigned):
    пропущены тиры 1, 2, 3
    tier 2 содержит "interfaces" → severity = "error"
```

**Примечание о `tier_to_layers`:**
В `run()` строится вспомогательный словарь `tier_to_layers: dict[int, set[str]]`
(инверсия `tier_map`) для эффективного поиска слоёв по номеру тира.
Передаётся в `_detect_skip_layer_violations` или строится внутри метода — на усмотрение реализации.

---

## Блок 5 — Интеграция `evidence` (заглушка + точка расширения)

**Файл:** `import_graph_adapter.py`

Поле `evidence` в каждом нарушении остаётся `[]` в данной итерации.
Добавляется docstring-комментарий:

```python
# evidence: список pyan3-рёбер, подтверждающих нарушение.
# Формат элемента: {"from": str, "to": str, "confidence": str}
# Заполняется внешним pipeline-оркестратором после запуска Pyan3Adapter.
# ImportGraphAdapter намеренно не знает о Pyan3 — SRP соблюдён.
```

---

## Блок 6 — Порядок вызовов в `run()` (обязательная последовательность)

> ⚠️ **Аудит-поправка #4:** зафиксирован строгий порядок для предотвращения
> зависимостей по данным при реализации.

```python
def run(self) -> dict:
    # 1. Строим граф — получаем nodes и edges
    nodes, layer_edges = self._build_layer_graph()

    # 2. Резолвим tier_map и interface_layers — не зависят от nodes/edges
    tier_map = self._resolve_tier_map()
    interface_layers = self._get_interface_layer_names()

    # 3. Строим instability_map из nodes — нужен для SDP-детектора
    instability_map = {n["id"]: n["instability"] for n in nodes}

    # 4. Строим tier_to_layers из tier_map — нужен для skip-layer детектора
    tier_to_layers = _invert_tier_map(tier_map)  # dict[int, set[str]]

    # 5. Запускаем детекторы
    sdp_violations = self._detect_sdp_violations(layer_edges, instability_map, sdp_tolerance)
    skip_violations = self._detect_skip_layer_violations(layer_edges, tier_map, interface_layers)

    violations = sdp_violations + skip_violations

    # 6. Возвращаем расширенный результат
    return {
        "nodes": nodes,
        "edges": list(layer_edges),
        "violations": violations,
    }
```

---

## Блок 7 — Тесты

**Директория:** `tests/test_import_graph_adapter/` (новая папка)

| Тест | Проверяет |
|---|---|
| `test_tier_map_nested_list` | `_resolve_tier_map` с Вариантом B |
| `test_tier_map_flat_list` | `_resolve_tier_map` с Вариантом A (fallback) |
| `test_tier_map_missing_config` | возвращает `{}`, не бросает исключение |
| `test_tier_map_external_layers_auto_assigned` | внешние слои попадают на `max_tier+1` автоматически |
| `test_tier_map_external_layers_explicit_override` | явный тир в `layer_order` приоритетнее автоматического |
| `test_sdp_violation_detected` | ребро стабильный→нестабильный (I_target > I_source) фиксируется |
| `test_sdp_no_violation_nonstable_to_stable` | легальная зависимость (нестабильный→стабильный) не флагируется |
| `test_sdp_tolerance_suppresses_borderline` | `tolerance=0.1` подавляет нарушение с дельтой `0.08` |
| `test_sdp_skips_zero_instability_target` | `routers → db_libs (I=0.0)` не попадает в SDP-нарушения |
| `test_sdp_receives_instability_map_not_nodes` | детектор принимает `dict[str, float]`, а не `list[dict]` |
| `test_skip_layer_warning` | пропуск обычного тира → `severity = "warning"` |
| `test_skip_layer_error_interface_skipped` | пропуск тира с interface-слоем → `severity = "error"` |
| `test_skip_layer_no_config` | fail-silent при отсутствии `layer_order` |
| `test_skip_layer_external_auto_tier` | `routers → db_libs` (auto-tier) корректно детектируется |
| `test_violations_in_output` | поле `violations` присутствует в выходе `run()` |
| `test_evidence_field_reserved` | каждый violation содержит `evidence: []` |
| `test_one_edge_multiple_violations` | одно ребро может дать SDP + SKIP_LAYER одновременно |

---

## План коммитов

```
Коммит 1 (docs — этот коммит):
  docs: update import graph refactor plan — resolve 5 internal conflicts

  Содержание: версия 2 плана с устранёнными противоречиями
  Риск: нулевой

──────────────────────────────────────────────────────────────

Коммит 2 (chore / config):
  chore: add layer_order, interface_layers and sdp_tolerance to solid_config.json

  Файлы: solid_config.json
  Содержание: tier-based layer_order, interface_layers, sdp_tolerance=0.0
  Риск: нулевой — только конфиг, адаптер ещё не читает новые поля

──────────────────────────────────────────────────────────────

Коммит 3 (feat / scaffold):
  feat(import_graph): add violations field scaffold to adapter output

  Файлы: import_graph_adapter.py
  Содержание: поле violations: [] в run(), схема violation-словаря в docstring,
  резервирование поля evidence с комментарием
  Риск: минимальный — пустой список не ломает существующих потребителей

──────────────────────────────────────────────────────────────

Коммит 4 (feat / tier-resolver):
  feat(import_graph): add tier map resolver with A/B format and auto external_layers

  Файлы: import_graph_adapter.py
  Содержание: _resolve_tier_map() с поддержкой nested/flat list,
  auto-assign external_layers на max_tier+1, _get_interface_layer_names()
  читает из конфига, fail-silent при отсутствии layer_order
  Риск: минимальный — новые приватные методы, не вызываются из run()

──────────────────────────────────────────────────────────────

Коммит 5 (feat / sdp-detector):
  feat(import_graph): implement SDP violation detector

  Файлы: import_graph_adapter.py
  Содержание: _detect_sdp_violations(layer_edges, instability_map, tolerance),
  интеграция в run() со строгим порядком вызовов (Блок 6),
  sdp_tolerance читается из конфига с дефолтом 0.0,
  docstring с примером-якорем направления условия
  Риск: средний — изменяется run()

──────────────────────────────────────────────────────────────

Коммит 6 (feat / skip-layer-detector):
  feat(import_graph): implement skip-layer violation detector

  Файлы: import_graph_adapter.py
  Содержание: _detect_skip_layer_violations(layer_edges, tier_map, interface_layers),
  severity-логика (warning vs error), tier_to_layers инверсия,
  интеграция в run()
  Риск: средний — аналогичен коммиту 5

──────────────────────────────────────────────────────────────

Коммит 7 (test):
  test(import_graph): add violation detection test suite

  Файлы: tests/test_import_graph_adapter/__init__.py
         tests/test_import_graph_adapter/conftest.py
         tests/test_import_graph_adapter/test_violations.py
  Содержание: 17 тест-кейсов по Блоку 7 плана
  Риск: нулевой
```

---

## Итоговая карта зависимостей между коммитами

```
[1: docs]
    ↓
[2: config] ──► [4: tier-resolver] ──► [6: skip-layer]
                                     ↗
[3: scaffold] ──► [5: SDP] ─────────
                              ↘
                               [7: tests]
```

Коммиты 2 и 3 независимы и могут идти параллельно.
Коммит 4 зависит от 2.
Коммиты 5 и 6 зависят от 3 и 4.
Коммит 7 — финальный, зависит от всех предыдущих.

---

## Аудит внутренней совместимости — сводка решений

| # | Зона | Тип проблемы | Решение |
|---|---|---|---|
| 1 | Направление SDP-условия | Риск инверсии при кодировании | Docstring с примером-якорем в `_detect_sdp_violations` |
| 2 | skip-layer vs external_layers | **Структурный конфликт** | `_resolve_tier_map()` авто-добавляет `external_layers` на `max_tier+1` |
| 3 | `_get_interface_layer_names()` hardcode | Хрупкость | Поле `interface_layers: [...]` в конфиге, читается с дефолтом |
| 4 | Сигнатура детекторов | Архитектурная ясность | Детекторы принимают `instability_map: dict[str,float]` |
| 5 | Дублирование violations для одного ребра | Поведенческая неопределённость | Задокументировано: N violations от разных правил — норма, без дедупликации |