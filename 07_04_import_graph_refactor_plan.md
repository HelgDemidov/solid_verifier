## Итоговый план рефакторинга `ImportGraphAdapter`

***

### Контекст и принципы

Все решения зафиксированы. Два ключевых принципа, которые пронизывают весь план:

- **Разделение труда**: Import Graph = слоевая семантика + правила; Pyan3 = доказательная база символов
- **Backward compatibility**: при отсутствии `layer_order` адаптер работает как сейчас, без исключений и шума

***

### Блок 0 — Изменения конфигурации

**Файл:** `solid_config.json`

Добавляется секция `layer_order` в блок `import_graph` в формате tier-based (Вариант B). Порядок тиров — от наиболее нестабильного (индекс 0) к наиболее стабильному (последний индекс). Также добавляется опциональный параметр `sdp_tolerance`.

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
    "sdp_tolerance": 0.0
}
```

`sdp_tolerance` — пользовательски настраиваемый параметр (float, дефолт `0.0`). Документируется в комментарии конфига: нарушение SDP фиксируется если `I(target) - I(source) > sdp_tolerance`. При `0.0` — абсолютно строгий режим.

***

### Блок 1 — Структура данных `violations` (scaffold)

**Файл:** `import_graph_adapter.py`

Вводится схема одного нарушения. Добавляется в выходной словарь `run()` как поле `"violations": []`.

```python
# Схема нарушения (расширенная, с зарезервированным evidence)
{
    "rule": "SDP_VIOLATION",          # str: SDP_VIOLATION | SKIP_LAYER
    "source_layer": "routers",        # str
    "target_layer": "infrastructure", # str
    "severity": "warning",            # str: warning | error
    "details": "I(routers)=1.0, I(infrastructure)=0.75 — нарушение SDP",
    "evidence": []                    # list: зарезервировано для pyan3_edges (сейчас всегда [])
}
```

Метод `run()` расширяется: `violations` собирается из результатов обоих детекторов и добавляется в возвращаемый dict рядом с `nodes` и `edges`.

***

### Блок 2 — Tier-резолвер (`_resolve_tier_map`)

**Файл:** `import_graph_adapter.py`

Приватный метод, вызывается один раз в `run()` после чтения конфига. Возвращает `dict[str, int]` — маппинг `layer_name → tier_index`.

**Логика парсинга:**
- Если `layer_order` — список списков (Вариант B): каждый вложенный список получает свой индекс тира
- Если `layer_order` — плоский список строк (Вариант A, fallback): каждый элемент получает уникальный индекс (эквивалентно тиру из одного слоя)
- Если `layer_order` отсутствует в конфиге: возвращает `{}`, логирует на уровне `debug`

Дополнительно: метод `_get_interface_layer_names()` — возвращает множество имён слоёв, которые являются interface-слоями (для skip-layer severity). Определяется через ключ `"interfaces"` в `layer_config` (или конфигурируемый список — пока hardcode через имя `"interfaces"`).

***

### Блок 3 — Детектор нарушений SDP (`_detect_sdp_violations`)

**Файл:** `import_graph_adapter.py`

```
Входные данные:
  - layer_edges: Set[Tuple[str, str]]
  - nodes: List[Dict]  — уже посчитанные instability-значения
  - sdp_tolerance: float  — из конфига, дефолт 0.0

Алгоритм:
  для каждого ребра (source, target) в layer_edges:
      I_source = instability[source]
      I_target = instability[target]
      если I_target - I_source > sdp_tolerance:
          → нарушение SDP (source зависит от более нестабильного target)
          severity = "warning"
          details = f"I({source})={I_source}, I({target})={I_target} — зависимость от более нестабильного слоя"

Граничный случай (развилка 1.2):
  Слои с I=0.0 (db_libs, web_libs) — пропускаются SDP-детектором,
  потому что для них I(target)=0.0 < I(source), нарушения нет математически.
  Ребро routers → db_libs (I=1.0 → I=0.0) НЕ является SDP-нарушением.
  Оно будет поймано skip-layer детектором как пропуск тиров.
```

***

### Блок 4 — Детектор skip-layer (`_detect_skip_layer_violations`)

**Файл:** `import_graph_adapter.py`

```
Входные данные:
  - layer_edges: Set[Tuple[str, str]]
  - tier_map: Dict[str, int]  — из _resolve_tier_map()
  - interface_layers: Set[str]  — из _get_interface_layer_names()

Алгоритм:
  если tier_map пустой → возвращаем [], логируем debug "layer_order not configured"
  
  для каждого ребра (source, target) в layer_edges:
      t_source = tier_map.get(source)
      t_target = tier_map.get(target)
      если t_source или t_target не найдены → пропустить (external layer или неизвестный)
      
      пропущено_тиров = t_target - t_source - 1
      если пропущено_тиров > 0:
          # Определяем severity
          пропущенные_тиры = тиры с индексами от t_source+1 до t_target-1
          пропущенные_слои = все слои в этих тирах
          если пропущенные_слои ∩ interface_layers не пустое:
              severity = "error"
              details = f"Пропущен interface-слой: {пропущенные_слои ∩ interface_layers}"
          иначе:
              severity = "warning"  
              details = f"Пропущено {пропущено_тиров} тир(а) между {source} и {target}"
          
          → нарушение SKIP_LAYER

Пример:
  routers (tier=0) → db_libs (tier=4): пропущены тиры 1,2,3
  tier 2 содержит "interfaces" → severity = "error"
```

***

### Блок 5 — Интеграция `evidence` (заглушка + точка расширения)

**Файл:** `import_graph_adapter.py`

Поле `evidence` в каждом нарушении остаётся `[]` в данной итерации. Добавляется docstring-комментарий:

```python
# evidence: список pyan3-рёбер, подтверждающих нарушение.
# Формат элемента: {"from": str, "to": str, "confidence": str}
# Заполняется внешним pipeline-оркестратором после запуска Pyan3Adapter.
# ImportGraphAdapter намеренно не знает о Pyan3 — SRP соблюдён.
```

Это зафиксирует контракт для будущего pipeline-оркестратора, который после запуска обоих адаптеров сможет обогатить `evidence` без изменения адаптеров.

***

### Блок 6 — Тесты

**Файл:** `tests/test_import_graph_adapter/` (новый или существующий тест-файл)

| Тест | Проверяет |
|---|---|
| `test_tier_map_nested_list` | `_resolve_tier_map` с Вариантом B |
| `test_tier_map_flat_list` | `_resolve_tier_map` с Вариантом A (fallback) |
| `test_tier_map_missing_config` | возвращает `{}`, не бросает |
| `test_sdp_violation_detected` | ребро нестабильный→стабильный с I-дельтой выше tolerance |
| `test_sdp_no_violation_stable_to_unstable` | легальная зависимость не флагируется |
| `test_sdp_tolerance_suppresses_borderline` | tolerance=0.1 подавляет нарушение с дельтой 0.08 |
| `test_sdp_skips_zero_instability_target` | `routers → db_libs` не попадает в SDP |
| `test_skip_layer_warning` | пропуск обычного тира → `warning` |
| `test_skip_layer_error_interface_skipped` | пропуск тира с interfaces → `error` |
| `test_skip_layer_no_config` | fail-silent при отсутствии `layer_order` |
| `test_violations_in_output` | поле `violations` присутствует в выходе `run()` |
| `test_evidence_field_reserved` | каждый violation содержит `evidence: []` |

***

### План коммитов

```
Коммит 1 (chore / config):
  chore: add layer_order and sdp_tolerance to solid_config.json

  Файлы: solid_config.json
  Содержание: tier-based layer_order, sdp_tolerance=0.0
  Риск: нулевой — только конфиг, адаптер ещё не читает новые поля

──────────────────────────────────────────────────────────────

Коммит 2 (feat / scaffold):
  feat(import_graph): add violations field scaffold to adapter output

  Файлы: import_graph_adapter.py
  Содержание: поле violations: [] в run(), схема violation-словаря
  в docstring, резервирование поля evidence с комментарием
  Риск: минимальный — пустой список не ломает существующих потребителей

──────────────────────────────────────────────────────────────

Коммит 3 (feat / tier-resolver):
  feat(import_graph): add tier map resolver with A/B format support

  Файлы: import_graph_adapter.py
  Содержание: _resolve_tier_map(), _get_interface_layer_names(),
  поддержка nested list (B) и flat list (A), fail-silent при отсутствии конфига
  Риск: минимальный — новые приватные методы, не вызываются ещё из run()

──────────────────────────────────────────────────────────────

Коммит 4 (feat / sdp-detector):
  feat(import_graph): implement SDP violation detector

  Файлы: import_graph_adapter.py
  Содержание: _detect_sdp_violations(), интеграция в run(),
  sdp_tolerance читается из конфига с дефолтом 0.0,
  граничный случай I=0.0 корректно обходится математикой
  Риск: средний — изменяется run(), нужна проверка что violations
  не ломает downstream (дашборд, рендерер)

──────────────────────────────────────────────────────────────

Коммит 5 (feat / skip-layer-detector):
  feat(import_graph): implement skip-layer violation detector

  Файлы: import_graph_adapter.py
  Содержание: _detect_skip_layer_violations(), severity-логика
  (warning vs error для interface-слоя), интеграция в run()
  Риск: средний — аналогичен коммиту 4

──────────────────────────────────────────────────────────────

Коммит 6 (test):
  test(import_graph): add violation detection test suite

  Файлы: tests/test_import_graph_adapter/test_violations.py
  Содержание: 12 тест-кейсов по блоку 6 плана
  Риск: нулевой

──────────────────────────────────────────────────────────────

Коммит 7 (docs / опциональный):
  docs(import_graph): document sdp_tolerance and layer_order in config comments

  Файлы: solid_config.json (inline-комментарии если формат позволяет),
  README или ARCHITECTURE.md
  Риск: нулевой
```

***

### Итоговая карта зависимостей между коммитами

```
[1: config] ──► [3: tier-resolver] ──► [5: skip-layer]
                                    ↗
[2: scaffold] ──► [4: SDP] ────────
                              ↘
                               [6: tests]
```

Коммиты 1 и 2 независимы и могут идти параллельно. Коммит 3 зависит от 1. Коммиты 4 и 5 зависят от 2 и 3. Коммит 6 — финальный, зависит от всех предыдущих.