# ===================================================================================================
# Адаптер графа импортов (Import Graph Adapter)
#
# Ключевая роль: Построение архитектурного графа зависимостей между слоями приложения
# и расчет метрик стабильности Мартина (Martin's Stability Metrics).
#
# Основные архитектурные задачи:
# 1. Построение полного графа импортов Python-пакета с использованием библиотеки grimp.
# 2. Маппинг физических модулей на логические архитектурные слои (layers) на основе конфигурации из solid_config.json.
# 3. Интеграция внешних библиотек (external_layers) в общий граф для контроля DIP (Dependency Inversion Principle).
# 4. Расчет метрик: Ca (Afferent Coupling), Ce (Efferent Coupling), Instability (I)
#    для каждого слоя, выявление потенциальных нарушений потока управления.
# 5. Разрешение tier-мапа из solid_config.json для SDP/SAP-проверок (коммит 4+).
# 6. Поддержка utility_layers (core, schemas) — crosscutting-слои в графе без SDP-проверки (коммит 4.6).
# 7. SDP violation detector: выявление нарушений Stable Dependencies Principle (коммит 5).
# 8. Skip-layer violation detector: выявление прямых зависимостей через несколько тиров (коммит 6).
# ===================================================================================================


import collections
import sys
from pathlib import Path
from typing import Any, DefaultDict, Dict, List, Optional, Set, Tuple
from solid_dashboard.interfaces.analyzer import IAnalyzer
import grimp


class ImportGraphAdapter(IAnalyzer):
    """
    Адаптер для построения графа архитектурных слоев на основе grimp.

    Использует тот же движок, что и import-linter. Это снижает риск
    расхождения между визуальным графом и контрактной проверкой.

    Схема словаря нарушения SDP (rule="SDP-001") в поле ``violations``:

    .. code-block:: python

        {
            "rule": str,               # "SDP-001"
            "layer": str,              # слой-источник нарушения
            "instability": float,      # I(source)
            "dependency": str,         # слой-цель (target)
            "dep_instability": float,  # I(target)
            "severity": str,           # "error"
            "message": str,            # человекочитаемое описание
            "evidence": None,          # reserved
        }

    Схема словаря нарушения Skip-Layer (rule="SLP-001") в поле ``violations``:

    .. code-block:: python

        {
            "rule": str,               # "SLP-001"
            "layer": str,              # слой-источник нарушения
            "tier": int,               # tier(source)
            "dependency": str,         # слой-цель (target)
            "dep_tier": int,           # tier(target)
            "skip_distance": int,      # количество пропущенных тиров
            "severity": str,           # "error" | "warning"
            "message": str,            # человекочитаемое описание
            "evidence": list,          # список пропущенных tier-индексов
        }

    SDP (Stable Dependencies Principle): слой должен зависеть только от слоев,
    которые стабильнее него самого. Формально для ребра source→target:

        I(source) <= I(target) + tolerance

    Нарушение: source зависит от target, но target нестабильнее source.
    Пример-якорь направления условия:
        services (I=0.8) → models (I=0.2): 0.8 <= 0.2 + 0.10? → False → violation
        routers  (I=1.0) → services (I=0.8): 1.0 <= 0.8 + 0.10? → False → violation
        models   (I=0.2) → db_libs  (I=0.0): 0.2 <= 0.0 + 0.10? → False → violation
            (но models→db_libs есть в allowed_dependency_exceptions → пропускается)

    SLP (Skip-Layer Principle): слой не должен зависеть напрямую от слоев,
    отстоящих более чем на 1 тир вниз по иерархии.
    Пример-якорь:
        routers (tier=0) → models (tier=4): skip_distance=3 → error
        routers (tier=0) → infrastructure (tier=2): skip_distance=1 → warning
        services (tier=1) → interfaces (tier=3): skip_distance=1, interfaces в interface_layers → warning

    Семантика utility_layers (crosscutting-слои):
    - ``utility_layers`` (core, schemas) участвуют в графе и метриках Ca/Ce/I.
    - Они НЕ входят в ``layer_order`` → tier=None → оба детектора их пропускают (fail-silent).
    - Это позволяет видеть реальные зависимости без ложных нарушений.
    """

    @property
    def name(self) -> str:
        return "import_graph"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # параметр context требуется интерфейсом IAnalyzer но не используется здесь
        _ = context

        target_path = Path(target_dir).resolve()
        package_name = target_path.name

        layer_config: Dict[str, List[str]] = config.get("layers", {})
        if not layer_config:
            return {
                "nodes": [],
                "edges": [],
                "violations": [],
                "error": "no layer configuration found in solid_config.json",
            }

        # Читаем utility_layers — crosscutting-слои (core, schemas и т.п.)
        # Участвуют в графе и метриках, но не входят в layer_order (нет SDP/SLP-проверки)
        utility_layer_config: Dict[str, List[str]] = config.get("utility_layers", {})
        external_layer_config: Dict[str, List[str]] = config.get("external_layers", {})

        normalized_layers = self._normalize_layer_config(layer_config, package_name)
        normalized_utility_layers = self._normalize_layer_config(utility_layer_config, package_name)

        # Объединяем внутренние и utility-слои для построения единого графа
        # При отсутствии utility_layers поведение идентично предыдущей версии
        combined_internal_layers = {**normalized_layers, **normalized_utility_layers}

        # Извлекаем ignore_dirs из конфига для фильтрации
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = [d.strip() for d in ignore_dirs_cfg if d and d.strip()]

        parent_dir = str(target_path.parent)
        added_to_path = False

        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
            added_to_path = True

        try:
            # grimp строит граф всего пакета (он не поддерживает ignore_dirs из коробки)
            graph = grimp.build_graph(package_name, include_external_packages=True)

            # Передаём combined_internal_layers — включает utility_layers (core, schemas)
            nodes, edges = self._build_layer_graph(
                graph=graph,
                layer_config=combined_internal_layers,
                external_layer_config=external_layer_config,
                ignore_dirs=ignore_dirs,
                package_name=package_name
            )

            # --- Блок 6: SDP violation detection ----------------------------
            # Порядок вызовов строго фиксирован: сначала граф, потом метрики,
            # потом tier-мап, потом детектор

            # Собираем instability_map из уже рассчитанных nodes за O(n)
            instability_map: Dict[str, float] = {
                node["id"]: node["instability"] for node in nodes
            }

            # Строим tier_map: utility_layers не получают tier → fail-silent в обоих детекторах
            tier_map = self._resolve_tier_map(config)

            # Читаем tolerance: дефолт 0.0 (строгая проверка)
            tolerance: float = float(config.get("sdp_tolerance") or 0.0)

            # Читаем allowed_dependency_exceptions: дефолт [] (без исключений)
            exceptions: List[Dict[str, Any]] = config.get("allowed_dependency_exceptions") or []

            # Восстанавливаем layer_edges из edges — используется обоими детекторами
            layer_edges_set: Set[Tuple[str, str]] = {
                (e["source"], e["target"]) for e in edges
            }

            violations = self._detect_sdp_violations(
                layer_edges=layer_edges_set,
                instability_map=instability_map,
                tier_map=tier_map,
                tolerance=tolerance,
                exceptions=exceptions,
            )
            # --- конец Блока 6 ----------------------------------------------

            # --- Блок 7: Skip-layer violation detection ---------------------
            # Выполняется строго после Блока 6: tier_map и layer_edges_set уже готовы
            # _get_interface_layer_names использует тот же config (коммит 4)

            interface_layer_names: List[str] = self._get_interface_layer_names(config)

            skip_violations = self._detect_skip_layer_violations(
                layer_edges=layer_edges_set,
                tier_map=tier_map,
                interface_layer_names=interface_layer_names,
            )

            # Объединяем: SDP-нарушения идут первыми (severity=error, более критичны),
            # skip-layer нарушения добавляются следом
            violations = violations + skip_violations
            # --- конец Блока 7 ----------------------------------------------

            return {
                "nodes": nodes,
                "edges": edges,
                "violations": violations,
                "debug_info": {
                    "package": package_name,
                    "total_modules": len(graph.modules),
                    "layer_prefixes_used": normalized_layers,
                    # utility_layer_prefixes_used: для верификации что utility_layers подхвачены
                    "utility_layer_prefixes_used": normalized_utility_layers,
                    "external_layer_prefixes_used": external_layer_config,
                    "sdp_tolerance_used": tolerance,
                    "sdp_exceptions_count": len(exceptions),
                    # поля коммита 6: счетчик и список interface_layers для трассировки
                    "skip_layer_violations_count": len(skip_violations),
                    "interface_layers_used": interface_layer_names,
                },
            }
        except Exception as exc:
            return {
                "nodes": [],
                "edges": [],
                "violations": [],  # контракт поля сохраняется даже при ошибке
                "error": str(exc),
            }
        finally:
            # Безопасное удаление из sys.path
            if added_to_path and parent_dir in sys.path:
                sys.path.remove(parent_dir)

    # ------------------------------------------------------------------
    # Skip-layer violation detector (коммит 6)
    # ------------------------------------------------------------------

    def _detect_skip_layer_violations(
        self,
        layer_edges: Set[Tuple[str, str]],
        tier_map: Optional[Dict[str, int]],
        interface_layer_names: List[str],
    ) -> List[Dict[str, Any]]:
        """
        Выявляет нарушения Skip-Layer Principle (SLP) по рёбрам графа.

        Слой нарушает SLP если он зависит напрямую от слоя, минуя один или
        несколько промежуточных тиров иерархии. Каждый тир должен
        взаимодействовать с соседними, а не с произвольно удалёнными.

        Условие нарушения для ребра source→target:

            skip_distance = tier(target) - tier(source) - 1 >= 1

        Пример-якорь:

        .. code-block:: text

            routers (tier=0) → models (tier=4): skip_distance=3 → error
            routers (tier=0) → infrastructure (tier=2): skip_distance=1 → warning
            services (tier=1) → interfaces (tier=3): skip_distance=1,
                interfaces в interface_layers → warning (ISP-aware downgrade)
            infrastructure (tier=2) → db_libs (tier=5): skip_distance=2 → error

        Severity-логика:
        - skip_distance >= 2 → ``error``   (прыжок через 2+ тира, явная архитектурная проблема)
        - skip_distance == 1 → ``warning`` (прыжок через 1 тир, допустимо при явном обосновании)
        - ISP-aware downgrade: если target входит в ``interface_layers`` →
          severity понижается до ``warning`` независимо от skip_distance,
          так как прямые зависимости от интерфейсных слоев архитектурно допустимы

        Evidence поле: список пропущенных tier-индексов (не None),
        позволяет быстро найти какие промежуточные слои были обойдены.

        Слои пропускаются (fail-silent) если:
        - source или target отсутствуют в tier_map (utility_layers, неизвестные слои)
        - tier_map равен None (нет layer_order в конфиге)
        - skip_distance < 1 (соседние тиры, нарушения нет)

        :param layer_edges: множество рёбер графа (source, target)
        :param tier_map: {layer_name: tier_index} или None
        :param interface_layer_names: список слоев-интерфейсов для ISP-aware downgrade
        :returns: список violation-словарей с rule="SLP-001"
        """
        violations: List[Dict[str, Any]] = []

        # Если tier_map не построен (нет layer_order) — нечего проверять
        if not tier_map:
            return violations

        # Инверсия tier_map → tier_to_layers для evidence: какие слои в каждом тире
        tier_to_layers: DefaultDict[int, List[str]] = collections.defaultdict(list)
        for layer_name, tier_index in tier_map.items():
            tier_to_layers[tier_index].append(layer_name)

        # Быстрый lookup для ISP-aware downgrade
        interface_set: Set[str] = set(interface_layer_names)

        for source, target in sorted(layer_edges):
            # Пропускаем слои без tier (utility_layers, неизвестные) — fail-silent
            if source not in tier_map or target not in tier_map:
                continue

            t_source = tier_map[source]
            t_target = tier_map[target]

            # skip_distance: сколько тиров пропущено между source и target
            # Пример: source=tier0, target=tier2 → skip_distance=1 (пропущен tier1)
            skip_distance = t_target - t_source - 1

            # Нет пропуска: соседние тиры или обратное ребро → не нарушение
            if skip_distance < 1:
                continue

            # ISP-aware downgrade: интерфейсные слои допускают прямые зависимости
            # (слой может напрямую зависеть от абстракций без проксирования через соседей)
            if target in interface_set:
                severity = "warning"
            elif skip_distance >= 2:
                # Прыжок через 2+ тира — явная архитектурная проблема
                severity = "error"
            else:
                # Прыжок через 1 тир — предупреждение
                severity = "warning"

            # Evidence: конкретные тиры, которые были обойдены
            skipped_tier_indices = list(range(t_source + 1, t_target))
            skipped_layer_names: List[str] = []
            for skipped_tier in skipped_tier_indices:
                skipped_layer_names.extend(tier_to_layers.get(skipped_tier, []))

            violations.append({
                "rule": "SLP-001",
                "layer": source,
                "tier": t_source,
                "dependency": target,
                "dep_tier": t_target,
                "skip_distance": skip_distance,
                "severity": severity,
                "message": (
                    f"{source} (tier={t_source}) depends directly on {target} (tier={t_target}), "
                    f"skipping {skip_distance} tier(s): {skipped_layer_names}"
                ),
                "evidence": skipped_tier_indices,
            })

        return violations

    # ------------------------------------------------------------------
    # SDP violation detector (коммит 5)
    # ------------------------------------------------------------------

    def _detect_sdp_violations(
        self,
        layer_edges: Set[Tuple[str, str]],
        instability_map: Dict[str, float],
        tier_map: Optional[Dict[str, int]],
        tolerance: float,
        exceptions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """
        Выявляет нарушения Stable Dependencies Principle (SDP) по рёбрам графа.

        SDP-условие для ребра source→target:

            I(source) <= I(target) + tolerance

        Нарушение фиксируется когда source зависит от target,
        но target нестабильнее source (с учётом допуска tolerance).

        Пример-якорь направления условия (tolerance=0.10, конфиг проекта):

        .. code-block:: text

            services (I=0.80) → models    (I=0.20): 0.80 <= 0.20+0.10=0.30? NO  → violation
            routers  (I=1.00) → services  (I=0.80): 1.00 <= 0.80+0.10=0.90? NO  → violation
            models   (I=0.20) → db_libs   (I=0.00): 0.20 <= 0.00+0.10=0.10? NO  → violation
                (но models→db_libs в allowed_dependency_exceptions → пропускается)
            infrastructure (I=0.60) → interfaces (I=0.33): 0.60 <= 0.33+0.10=0.43? NO → violation

        Слои пропускаются (fail-silent) если:
        - source или target отсутствуют в tier_map (utility_layers, неизвестные слои)
        - tier_map равен None (нет layer_order в конфиге)

        Исключения из ``allowed_dependency_exceptions`` фильтруются по
        {\"source\": str, \"target\": str} до применения SDP-проверки.

        :param layer_edges: множество рёбер графа (source, target)
        :param instability_map: {layer_name: instability_value}
        :param tier_map: {layer_name: tier_index} или None
        :param tolerance: допустимое превышение I(target) над I(source) (из sdp_tolerance)
        :param exceptions: список разрешённых нарушений из allowed_dependency_exceptions
        :returns: список violation-словарей
        """
        violations: List[Dict[str, Any]] = []

        # Если tier_map не построен (нет layer_order) — нечего проверять
        if not tier_map:
            return violations

        # Строим быстрый lookup для исключений: set of (source, target)
        exception_pairs: Set[Tuple[str, str]] = {
            (exc.get("source", ""), exc.get("target", ""))
            for exc in exceptions
            if exc.get("source") and exc.get("target")
        }

        for source, target in sorted(layer_edges):
            # Пропускаем слои без tier (utility_layers, неизвестные) — fail-silent
            if source not in tier_map or target not in tier_map:
                continue

            # Пропускаем явно разрешённые исключения из конфига
            if (source, target) in exception_pairs:
                continue

            i_source = instability_map.get(source, 0.0)
            i_target = instability_map.get(target, 0.0)

            # SDP-условие: source должен быть не менее стабильным чем target (с допуском)
            # Нарушение: source зависит от чего-то нестабильнее себя
            if i_source > i_target + tolerance:
                violations.append({
                    "rule": "SDP-001",
                    "layer": source,
                    "instability": i_source,
                    "dependency": target,
                    "dep_instability": i_target,
                    "severity": "error",
                    "message": (
                        f"{source} (I={i_source}) depends on {target} (I={i_target}), "
                        f"but {target} is less stable than {source} "
                        f"(violation margin: {round(i_source - i_target, 2)}, tolerance: {tolerance})"
                    ),
                    "evidence": None,  # reserved
                })

        return violations

    # ------------------------------------------------------------------
    # Tier-map resolver
    # ------------------------------------------------------------------

    def _resolve_tier_map(
        self,
        config: Dict[str, Any],
    ) -> Optional[Dict[str, int]]:
        """
        Строит tier-мап из ``solid_config.json``.

        Поддерживает два формата:

        **Формат A (плоский список)** -- порядок слоев от наиболее нестабильного (tier 0) к
        наиболее стабильному (tier N):

        .. code-block:: json

            { "layer_order": ["routers", "services", "infrastructure", "interfaces", "models"] }

        **Формат B (вложенный список групп)** -- слои одного тира сгруппированы внутренней листом;
        индекс внешнего списка задает тир для всех внутренних элементов:

        .. code-block:: json

            {
              "layer_order": [
                ["routers"],
                ["services", "infrastructure"],
                ["interfaces"],
                ["models"]
              ]
            }

        Внешние слои из ``external_layers`` автоматически получают тир ``max_tier + 1``
        (самые стабильные зависимости, на них все опираются внутренние слои).

        ``utility_layers`` намеренно не присваиваются в tier_map —
        они crosscutting и не участвуют в SDP/SLP-проверках.

        Возвращает None если ``layer_order`` отсутствует или пустой (fail-silent).

        :returns: словарь {layer_name: tier_index} или None
        """
        raw_order = config.get("layer_order")

        # Файл-сайлент: если поле отсутствует или некорректного типа - возвращаем None
        if not raw_order or not isinstance(raw_order, list):
            return None

        tier_map: Dict[str, int] = {}

        # Определяем формат через первый элемент: строка = формат A, список = формат B
        first = raw_order[0] if raw_order else None

        if isinstance(first, str):
            # Формат A: плоский список строк, каждая получает свой tier
            for tier_index, layer_name in enumerate(raw_order):
                if isinstance(layer_name, str) and layer_name.strip():
                    tier_map[layer_name.strip()] = tier_index

        elif isinstance(first, list):
            # Формат B: вложенный список, tier = индекс внешнего списка
            for tier_index, group in enumerate(raw_order):
                if not isinstance(group, list):
                    continue
                for layer_name in group:
                    if isinstance(layer_name, str) and layer_name.strip():
                        tier_map[layer_name.strip()] = tier_index

        else:
            # Неизвестный формат: возвращаем None
            return None

        if not tier_map:
            return None

        # Авто-присвоение external_layers на max_tier + 1
        # (внешние библиотеки считаются самыми стабильными: I -> 0)
        # utility_layers НЕ добавляются в tier_map намеренно — они crosscutting
        external_layer_config: Dict[str, Any] = config.get("external_layers") or {}
        if external_layer_config:
            max_tier = max(tier_map.values())
            external_tier = max_tier + 1
            for ext_layer_name in external_layer_config:
                if ext_layer_name not in tier_map:
                    tier_map[ext_layer_name] = external_tier

        return tier_map

    def _get_interface_layer_names(self, config: Dict[str, Any]) -> List[str]:
        """
        Извлекает список слоев-интерфейсов из конфигурации.

        Читает поле ``interface_layers`` из ``solid_config.json``.
        Для этих слоев стабильность I -> 0 является ожидаемой,
        и SAP-детектор будет применять повышенный порог предупреждения.
        В SLP-детекторе эти слои получают ISP-aware downgrade severity до warning.

        Возвращает пустой список если поле отсутствует (fail-silent).

        :returns: список имен слоев-интерфейсов
        """
        raw = config.get("interface_layers")

        # Файл-сайлент: отсутствие поля или некорректный тип не ломают пиплайн
        if not raw or not isinstance(raw, list):
            return []

        # Фильтруем: только непустые строки
        return [name.strip() for name in raw if isinstance(name, str) and name.strip()]

    # ------------------------------------------------------------------
    # Существующие приватные методы
    # ------------------------------------------------------------------

    def _is_ignored(self, module_name: str, ignore_dirs: List[str], package_name: str) -> bool:
        """
        Проверяет, попадает ли модуль в игнорируемые директории.
        Пример: если module_name="app.tests.test_auth" и ignore_dirs=["tests"], вернет True.
        """
        if not ignore_dirs:
            return False

        parts = module_name.split('.')
        # Если модуль начинается с имени нашего пакета, проверяем его внутренние пути
        if parts and parts[0] == package_name:
            for part in parts[1:]:
                if part in ignore_dirs:
                    return True
        return False

    def _normalize_layer_config(
        self,
        layer_config: Dict[str, Any],
        package_name: str,
    ) -> Dict[str, List[str]]:
        """
        Нормализует конфиг внутренних слоев.

        Поддерживает оба формата:
        - "routers": "routers"
        - "routers": ["routers"]

        На выходе всегда возвращает:
        - "routers": ["app.routers"]

        Используется как для layers, так и для utility_layers.
        """
        normalized: Dict[str, List[str]] = {}
        package_prefix = f"{package_name}."

        for layer_name, raw_value in layer_config.items():
            # приводим значение слоя к списку строк
            if isinstance(raw_value, str):
                paths = [raw_value]
            elif isinstance(raw_value, list):
                paths = [p for p in raw_value if isinstance(p, str)]
            else:
                # некорректный тип silently пропускаем,
                # чтобы не ломать весь адаптер
                paths = []

            fixed_paths: List[str] = []

            for path in paths:
                cleaned_path = path.strip()
                if not cleaned_path:
                    continue

                # если путь уже полный, не меняем его
                if (
                    cleaned_path == package_name
                    or cleaned_path.startswith(package_prefix)
                ):
                    fixed_paths.append(cleaned_path)
                else:
                    fixed_paths.append(f"{package_name}.{cleaned_path}")

            normalized[layer_name] = fixed_paths

        return normalized

    def _build_layer_graph(
        self,
        graph: grimp.ImportGraph,
        layer_config: Dict[str, List[str]],
        external_layer_config: Dict[str, List[str]],
        ignore_dirs: List[str],
        package_name: str
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:

        all_layer_names: List[str] = list(layer_config.keys())
        all_layer_names.extend(external_layer_config.keys())

        layer_edges: Set[Tuple[str, str]] = set()

        for module_name in graph.modules:
            # Фильтрация исходящего модуля по ignore_dirs
            if self._is_ignored(module_name, ignore_dirs, package_name):
                continue

            importer_layer = self._resolve_internal_layer(module_name, layer_config)
            if not importer_layer:
                continue

            try:
                imported_modules = graph.find_modules_directly_imported_by(module_name)
            except Exception:
                continue

            for imported_module_name in imported_modules:
                # Фильтрация импортируемого модуля по ignore_dirs
                if self._is_ignored(imported_module_name, ignore_dirs, package_name):
                    continue

                imported_layer = self._resolve_internal_layer(imported_module_name, layer_config)

                if not imported_layer and external_layer_config:
                    imported_layer = self._resolve_external_layer(
                        imported_module_name, external_layer_config
                    )

                if not imported_layer:
                    continue

                if importer_layer != imported_layer:
                    layer_edges.add((importer_layer, imported_layer))

        nodes = self._build_nodes_with_stability(
            layer_names=all_layer_names, layer_edges=layer_edges
        )
        edges = [{"source": source, "target": target} for source, target in sorted(layer_edges)]

        return nodes, edges

    def _build_nodes_with_stability(
        self,
        layer_names: List[str],
        layer_edges: Set[Tuple[str, str]],
    ) -> List[Dict[str, Any]]:
        """
        Считает для каждого слоя метрики stability.

        ca:
            сколько слоев зависит от данного слоя
        ce:
            от скольких слоев зависит данный слой
        instability:
            ce / (ca + ce), диапазон от 0.0 до 1.0
        """
        nodes: List[Dict[str, Any]] = []

        for layer_name in layer_names:
            # ce = количество исходящих зависимостей слоя
            ce = len(
                {
                    target
                    for source, target in layer_edges
                    if source == layer_name
                }
            )

            # ca = количество входящих зависимостей слоя
            ca = len(
                {
                    source
                    for source, target in layer_edges
                    if target == layer_name
                }
            )

            # instability по роберту мартину
            if ca + ce > 0:
                instability = round(ce / (ca + ce), 2)
            else:
                instability = 0.0

            nodes.append(
                {
                    "id": layer_name,
                    "label": layer_name,
                    "ca": ca,
                    "ce": ce,
                    "instability": instability,
                }
            )

        return nodes

    def _resolve_internal_layer(
        self,
        module_name: str,
        layer_config: Dict[str, List[str]],
    ) -> Optional[str]:
        """
        Ищет внутренний слой для модуля.

        Пример:
        - модуль "app.services.user_service"
        - путь слоя "app.services"
        - результат: "services"
        """
        for layer_name, paths in layer_config.items():
            for path in paths:
                if module_name == path or module_name.startswith(f"{path}."):
                    return layer_name
        return None

    def _resolve_external_layer(
        self,
        module_name: str,
        external_layer_config: Dict[str, List[str]],
    ) -> Optional[str]:
        """
        Ищет внешний слой для third-party модуля.

        Пример:
        - модуль "sqlalchemy.orm"
        - внешний слой "db_libs": ["sqlalchemy"]
        - результат: "db_libs"
        """
        for layer_name, package_prefixes in external_layer_config.items():
            for package_prefix in package_prefixes:
                if (
                    module_name == package_prefix
                    or module_name.startswith(f"{package_prefix}.")
                ):
                    return layer_name
        return None
