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
# ===================================================================================================


import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from solid_dashboard.interfaces.analyzer import IAnalyzer
import grimp


class ImportGraphAdapter(IAnalyzer):
    """
    Адаптер для построения графа архитектурных слоев на основе grimp.

    Использует тот же движок, что и import-linter. Это снижает риск
    расхождения между визуальным графом и контрактной проверкой.

    Схема словаря нарушения (violation) в поле ``violations``:

    .. code-block:: python

        {
            "rule": str,          # идентификатор правила, например "SDP-001" или "SAP-001"
            "layer": str,         # имя слоя, нарушающего правило
            "instability": float, # фактическое значение I для данного слоя
            "dependency": str,    # имя слоя-зависимости (target), нарушающего отношение
            "dep_instability": float,  # значение I зависимости
            "severity": str,      # "error" | "warning" | "info"
            "message": str,       # человекочитаемое описание нарушения
            "evidence": None,     # TODO: reserved for SDP/SAP evidence payload (dict | None)
        }

    Поле ``evidence`` зарезервировано для будущего слоя доказательств:
    конкретных модулей и рёбер импорта, подтверждающих нарушение метрики.
    До реализации SDP/SAP-детектора всегда равно None.
    """

    @property
    def name(self) -> str:
        return "import_graph"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # комментарий (ru): параметр context требуется интерфейсом IAnalyzer но не используется здесь
        _ = context

        target_path = Path(target_dir).resolve()
        package_name = target_path.name

        layer_config: Dict[str, List[str]] = config.get("layers", {})
        if not layer_config:
            return {
                "nodes": [],
                "edges": [],
                "violations": [],  # scaffold: пустой список до реализации SDP/SAP-детектора
                "error": "no layer configuration found in solid_config.json",
            }

        external_layer_config: Dict[str, List[str]] = config.get("external_layers", {})
        normalized_layers = self._normalize_layer_config(layer_config, package_name)

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

            # Передаем ignore_dirs в сборщик слоев для ручной фильтрации
            nodes, edges = self._build_layer_graph(
                graph=graph,
                layer_config=normalized_layers,
                external_layer_config=external_layer_config,
                ignore_dirs=ignore_dirs,
                package_name=package_name
            )

            return {
                "nodes": nodes,
                "edges": edges,
                # scaffold: violations всегда пустой до реализации SDP/SAP-детектора (коммит 4+)
                "violations": [],
                "debug_info": {
                    "package": package_name,
                    "total_modules": len(graph.modules),
                    "layer_prefixes_used": normalized_layers,
                    "external_layer_prefixes_used": external_layer_config,
                },
            }
        except Exception as exc:
            return {
                "nodes": [],
                "edges": [],
                "violations": [],  # scaffold: контракт поля сохраняется даже при ошибке
                "error": str(exc),
            }
        finally:
            # Безопасное удаление из sys.path
            if added_to_path and parent_dir in sys.path:
                sys.path.remove(parent_dir)

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
