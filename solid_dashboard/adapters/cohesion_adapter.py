# ===================================================================================================
# Адаптер связности (Cohesion Adapter)
#
# Ключевая роль: Вычисление метрики связности классов (LCOM4 — Lack of Cohesion of Methods)
# методом полностью самостоятельного статического AST-анализа без внешних зависимостей.
#
# Основные архитектурные задачи:
# 1. Рекурсивный обход целевой директории (target_dir) с соблюдением ignore_dirs.
# 2. AST-парсинг каждого Python-файла: извлечение классов, методов, атрибутов.
# 3. Построение графа связности для каждого класса: методы связаны, если делят атрибут
#    или явно вызывают друг друга (через self.method() или cls.method()).
# 4. Вычисление LCOM4 через DFS (поиск в глубину) — подсчет количества несвязных компонент в графе методов класса.
# 5. Агрегация метрик (mean_cohesion_all, mean_cohesion_multi_method, low_cohesion_count)
#    только для concrete-классов — интерфейсы и абстрактные классы в метрики не включаются.
# ===================================================================================================


import ast
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from solid_dashboard.interfaces.analyzer import IAnalyzer
from solid_dashboard.adapters.class_classifier import classify_class


# явно объявляем публичный API модуля — Pylance и другие линтеры увидят оба символа
__all__ = ["CohesionAdapter", "ClassInfo", "MethodInfo"]


# именованный логгер модуля — используется во всех методах адаптера
logger = logging.getLogger(__name__)


# ================================
# ВСПОМОГАТЕЛЬНЫЕ СТРУКТУРЫ ДАННЫХ
# ================================

@dataclass
class MethodInfo:
    """Информация о методе внутри класса."""
    name: str                     # имя метода (get_user, create, ...)
    lineno: int                   # номер строки начала метода
    is_async: bool                # является ли метод async def
    decorator_kinds: List[str] = field(default_factory=list)
    # decorator_kinds: ["property", "classmethod", "staticmethod"]

    # какие атрибуты класса/экземпляра использует метод
    used_attributes: Set[str] = field(default_factory=set)
    # какие методы этого же класса он вызывает (по имени)
    called_methods: Set[str] = field(default_factory=set)

    is_empty: bool = False
    # is_empty: True если тело метода тривиально — pass / ... / raise NotImplementedError /
    # строка-докстринг или их комбинация; такие методы исключаются из графа LCOM4


@dataclass
class ClassInfo:
    """Информация о классе: основа для расчета LCOM4."""
    name: str                          # имя класса (UserService, ArticleRepository, ...)
    filepath: str                      # абсолютный путь к файлу, где объявлен класс
    lineno: int                        # строка объявления class
    methods: List[MethodInfo] = field(default_factory=list)
    attributes: Set[str] = field(default_factory=set)
    # attributes: множество имен полей класса/экземпляра
    # (уровень класса + self.xxx из __init__ текущего класса + унаследованные self.xxx из __init__ предков)
    kind: str = "concrete"
    # kind: семантический тип класса — "concrete" / "abstract" / "interface" / "dataclass"
    # заполняется функцией classify_class (class_classifier.py) на этапе _build_class_info


# ================================
# ОСНОВНОЙ АДАПТЕР
# ================================

class CohesionAdapter(IAnalyzer):
    @property
    def name(self) -> str:
        return "cohesion"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # параметр context требуется интерфейсом IAnalyzer, но не используется здесь
        _ = context

        target_path = Path(target_dir).resolve()

        # читаем порог low-cohesion из конфига; дефолт = 1 (LCOM4 > 1 означает несвязный класс)
        low_cohesion_threshold: int = int(config.get("cohesion_threshold", 1))

        # достаем список игнорируемых папок из конфига
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = set(name.strip() for name in ignore_dirs_cfg if name and name.strip())

        # передаем ignore_dirs в сборщик; двухпроходная реализация обогащает атрибуты предков
        classes_info: List[ClassInfo] = self._collect_classes(target_path, ignore_dirs)

        # считаем LCOM4 для каждого класса и формируем результирующий список
        class_results: List[Dict[str, Any]] = []

        # сырые значения LCOM4 — только concrete-классы, где methods_count > 0
        cohesion_values_all: List[float] = []

        # значения LCOM4 только по concrete-классам с methods_count >= 2
        cohesion_values_multi_method: List[float] = []
        analyzed_classes_multi_method = 0

        low_cohesion_count = 0
        concrete_classes_count = 0       # счетчик concrete-классов, попавших в агрегаты
        low_cohesion_excluded_count = 0  # non-concrete классы, превысившие порог (но исключенные из агрегатов)

        # краткие записи non-concrete нарушителей для информативности отчета
        low_cohesion_excluded_classes: List[Dict[str, Any]] = []

        for class_info in classes_info:
            lcom4, methods_count = self._compute_lcom4(class_info)
            if methods_count == 0:
                # классы без методов для метрики не интересны
                continue

            cohesion_score = float(lcom4)

            # добавляем class_kind и excluded_from_aggregation в каждый элемент выходного списка классов
            class_results.append({
                "name": class_info.name,
                "methods_count": methods_count,
                "cohesion_score": cohesion_score,
                # нормализованная связность: 1.0 = идеально связный, < 1.0 = несвязный
                "cohesion_score_norm": round(1.0 / cohesion_score, 4) if cohesion_score > 1.0 else 1.0,
                "filepath": class_info.filepath,
                "lineno": class_info.lineno,
                "class_kind": class_info.kind,
                # True для interface/abstract/dataclass — класс виден в списке, но не входит в агрегаты
                "excluded_from_aggregation": class_info.kind != "concrete",
            })

            if class_info.kind == "concrete":
                # агрегаты считаются только по concrete-классам — интерфейсы не засоряют метрики
                concrete_classes_count += 1

                cohesion_values_all.append(cohesion_score)

                if methods_count >= 2:
                    cohesion_values_multi_method.append(cohesion_score)
                    analyzed_classes_multi_method += 1

                if lcom4 > low_cohesion_threshold:
                    low_cohesion_count += 1
            else:
                # для non-concrete классов (abstract / interface / dataclass) фиксируем
                # нарушение порога отдельно — они не влияют на агрегаты, но сигнал полезен
                if lcom4 > low_cohesion_threshold:
                    low_cohesion_excluded_count += 1
                    # краткая запись нарушителя: не дублируем полный class_results,
                    # только поля, необходимые для диагностики
                    low_cohesion_excluded_classes.append({
                        "name": class_info.name,
                        "filepath": class_info.filepath,
                        "lineno": class_info.lineno,
                        "cohesion_score": cohesion_score,
                        "class_kind": class_info.kind,
                    })

        # total_classes_analyzed — полный счетчик всех kinds для информативности
        total_classes_analyzed = len(class_results)

        if cohesion_values_all:
            mean_cohesion_all = float(sum(cohesion_values_all) / len(cohesion_values_all))
        else:
            mean_cohesion_all = 0.0

        if cohesion_values_multi_method:
            mean_cohesion_multi = float(sum(cohesion_values_multi_method) / len(cohesion_values_multi_method))
        else:
            mean_cohesion_multi = 0.0

        return {
            "total_classes_analyzed": total_classes_analyzed,
            # сколько concrete-классов вошло в агрегаты
            "concrete_classes_count": concrete_classes_count,
            # среднее по concrete-классам с хотя бы одним методом
            "mean_cohesion_all": round(mean_cohesion_all, 2),
            # среднее только по concrete-классам с methods_count >= 2
            "mean_cohesion_multi_method": round(mean_cohesion_multi, 2),
            # сколько concrete-классов реально попало во второе среднее
            "analyzed_classes_count": analyzed_classes_multi_method,
            "low_cohesion_count": low_cohesion_count,
            # сколько non-concrete классов превысили порог, но были исключены из агрегатов
            "low_cohesion_excluded_count": low_cohesion_excluded_count,
            # краткие записи non-concrete нарушителей — name/filepath/lineno/cohesion_score/class_kind
            # полная информация по каждому доступна в секции "classes" по полю excluded_from_aggregation
            "low_cohesion_excluded_classes": low_cohesion_excluded_classes,
            # порог, использованный при подсчете low_cohesion_count — для прозрачности отчета
            "low_cohesion_threshold": low_cohesion_threshold,
            "classes": class_results,
        }

    # ================================
    # СБОР КЛАССОВ, МЕТОДОВ И АТРИБУТОВ
    # ================================

    def _collect_classes(self, target_path: Path, ignore_dirs: set) -> List[ClassInfo]:
        """
        Двухпроходный сбор ClassInfo по всем Python-файлам в target_path.

        Pass 1 — парсинг файлов: для каждого ast.ClassDef создаем ClassInfo,
                 собираем атрибуты уровня класса и методы, строим глобальный
                 ClassDef-индекс (имя класса -> список (filepath, ast.ClassDef)).

                 Индекс хранит список, а не одно значение: если несколько файлов
                 содержат класс с одним именем, все варианты сохраняются.
                 Разрешение неоднозначности делегируется _resolve_classdef().

        Pass 2 — обогащение атрибутами предков: для каждого ClassInfo
                 вызываем _enrich_with_ancestor_attributes, которая рекурсивно
                 обходит MRO-цепочку по индексу и добавляет self.xxx из __init__
                 всех найденных предков.
        """
        # промежуточный контейнер для первого прохода:
        # список (ClassInfo, ast.ClassDef) — ClassDef нужен на Pass 2 для извлечения bases
        raw_items: List[Tuple[ClassInfo, ast.ClassDef]] = []

        # глобальный индекс: имя класса -> список (filepath_str, ast.ClassDef)
        # хранит все определения с таким именем — устраняет last-write-wins коллизию
        classdef_index: Dict[str, List[Tuple[str, ast.ClassDef]]] = {}

        # ------------------------------------------------------------------
        # Pass 1: парсим файлы, строим ClassInfo и индекс
        # ------------------------------------------------------------------
        for root, dirs, files in os.walk(target_path):
            # in-place модификация запрещает os.walk спускаться в игнорируемые директории
            dirs[:] = [d for d in dirs if d not in ignore_dirs]

            for filename in files:
                if not filename.endswith(".py"):
                    continue

                file_path = Path(root) / filename
                try:
                    source = file_path.read_text(encoding="utf-8")
                except OSError as e:
                    logger.warning("CohesionAdapter: cannot read %s: %s", file_path, e)
                    continue
                try:
                    tree = ast.parse(source, filename=str(file_path))
                except SyntaxError as e:
                    logger.warning("CohesionAdapter: syntax error in %s: %s", file_path, e)
                    continue

                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        class_info = self._build_class_info(node, file_path)
                        # собираем self.xxx из __init__ самого класса (предки — на Pass 2)
                        self._collect_instance_attributes_from_init(class_info, node)
                        # наполняем used_attributes / called_methods по текущим атрибутам
                        self._populate_method_usage(class_info, node)

                        raw_items.append((class_info, node))
                        # добавляем (filepath, ClassDef) в список для данного имени;
                        # все определения сохраняются — дубликаты разрешит _resolve_classdef
                        filepath_str = str(file_path.resolve())
                        classdef_index.setdefault(node.name, []).append((filepath_str, node))

        # ------------------------------------------------------------------
        # Pass 2: обогащаем атрибуты каждого класса из __init__ его предков,
        # затем перезапускаем Visitor для методов, у которых появились новые атрибуты
        # ------------------------------------------------------------------
        for class_info, class_node in raw_items:
            # фиксируем размер до обогащения — нужен для проверки на расширение
            attrs_before = len(class_info.attributes)

            self._enrich_with_ancestor_attributes(
                class_info, class_node, classdef_index, class_info.filepath
            )

            # перезапускаем Visitor только если атрибуты реально расширились
            if len(class_info.attributes) > attrs_before:
                self._repopulate_method_usage(class_info, class_node)

        return [ci for ci, _ in raw_items]

    def _resolve_classdef(
        self,
        base_name: str,
        classdef_index: Dict[str, List[Tuple[str, ast.ClassDef]]],
        caller_filepath: str,
    ) -> Optional[ast.ClassDef]:
        """
        Разрешает имя базового класса в единственный ast.ClassDef по индексу.

        Логика разрешения (по убыванию приоритета):
        1. Имя не найдено в индексе — возвращает None (предок внешний, не наш код).
        2. Ровно одно определение — возвращает его безусловно.
        3. Несколько определений — пытается выбрать то, что лежит
           в том же файле, что и вызывающий класс (caller_filepath).
           Это корректно для случая, когда base и derived определены
           в одном модуле (наиболее частый сценарий).
        4. Если в том же файле нет подходящего — неоднозначность неразрешима;
           логируем WARNING и возвращаем None (graceful degradation).

        Параметры:
            base_name       — короткое имя базового класса (из ast.Name.id)
            classdef_index  — индекс имя -> [(filepath, ClassDef), ...]
            caller_filepath — абсолютный путь файла текущего (дочернего) класса
        """
        entries = classdef_index.get(base_name)
        if not entries:
            # предок не в нашем коде — внешняя зависимость, молча пропускаем
            return None

        if len(entries) == 1:
            # единственное определение — никакой неоднозначности
            return entries[0][1]

        # несколько определений с одним именем — пробуем разрешить по файлу вызывающего класса
        same_file_entries = [classdef for fp, classdef in entries if fp == caller_filepath]

        if len(same_file_entries) == 1:
            # ровно одно определение в том же файле — однозначно
            return same_file_entries[0]

        # неоднозначность неразрешима: несколько файлов содержат класс с таким именем,
        # и ни один не совпадает однозначно с caller_filepath;
        # пропускаем предка, чтобы не внести ложные атрибуты в граф LCOM4
        filepaths_str = ", ".join(fp for fp, _ in entries)
        logger.warning(
            "CohesionAdapter: ambiguous base class '%s' found in %d files (%s); "
            "skipping MRO enrichment for caller '%s'",
            base_name, len(entries), filepaths_str, caller_filepath,
        )
        return None

    def _enrich_with_ancestor_attributes(
        self,
        class_info: ClassInfo,
        class_node: ast.ClassDef,
        classdef_index: Dict[str, List[Tuple[str, ast.ClassDef]]],
        caller_filepath: str,
        _visited: Optional[Set[str]] = None,
    ) -> None:
        """
        Рекурсивно обходит MRO-цепочку класса по AST-индексу и добавляет
        в class_info.attributes все self.xxx, присвоенные в __init__ предков.

        Алгоритм:
        1. Извлечь имена базовых классов из class_node.bases.
        2. Для каждого имени вызвать _resolve_classdef: получить однозначный ClassDef
           или None (внешняя зависимость / неразрешимая коллизия имен).
        3. Для найденного предка рекурсивно вызвать себя, затем собрать self.xxx
           из __init__ этого предка.
        4. _visited предотвращает бесконечные циклы при diamond-наследовании.

        Параметр caller_filepath передается в _resolve_classdef для разрешения
        коллизий имен: предпочтение отдается определению из того же файла.
        """
        if _visited is None:
            # инициализируем множество посещенных имен; стартуем с текущего класса
            _visited = {class_node.name}

        for base in class_node.bases:
            # извлекаем короткое имя базового класса (последний компонент)
            if isinstance(base, ast.Name):
                base_name = base.id
            elif isinstance(base, ast.Attribute):
                # учитываем последний компонент: models.Model -> "Model"
                base_name = base.attr
            else:
                continue

            # защита от циклов и повторных обходов одного предка
            if base_name in _visited:
                continue
            _visited.add(base_name)

            # разрешаем имя предка в ClassDef через новый метод
            ancestor_node = self._resolve_classdef(base_name, classdef_index, caller_filepath)
            if ancestor_node is None:
                # внешняя зависимость или неразрешимая коллизия — пропускаем
                continue

            # сначала рекурсивно поднимаемся по MRO предка (глубина-первый)
            self._enrich_with_ancestor_attributes(
                class_info, ancestor_node, classdef_index, caller_filepath, _visited
            )

            # затем собираем self.xxx из __init__ этого предка в атрибуты текущего класса
            self._collect_instance_attributes_from_init(class_info, ancestor_node)

    def _build_class_info(self, class_node: ast.ClassDef, file_path: Path) -> ClassInfo:
        """
        Строит ClassInfo для одного ast.ClassDef:
        - имя класса, путь к файлу, строка объявления
        - список методов
        - множество атрибутов класса (уровень class body)
        - kind: семантический тип класса (определяется через classify_class из class_classifier)
        """
        class_info = ClassInfo(
            name=class_node.name,
            filepath=str(file_path.resolve()),
            lineno=class_node.lineno,
            methods=[],
            attributes=set(),
        )

        # проходим только по прямым потомкам class_node.body
        for node in class_node.body:
            # 1) методы класса (обычные и async)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                method_info = self._build_method_info(node)
                class_info.methods.append(method_info)

            # 2) присваивания на уровне класса (атрибуты)
            if isinstance(node, ast.Assign):
                attr_names = self._extract_names_from_assign(node)
                class_info.attributes.update(attr_names)

            # 3) аннотированные присваивания (Pydantic/BaseModel, dataclasses)
            if isinstance(node, ast.AnnAssign):
                attr_name = self._extract_name_from_ann_assign(node)
                if attr_name is not None:
                    class_info.attributes.add(attr_name)

        # определяем семантический тип класса — публичная функция из class_classifier
        class_info.kind = classify_class(class_node)

        return class_info

    def _build_method_info(self, func_node: ast.AST) -> MethodInfo:
        """
        Строит MethodInfo для ast.FunctionDef или ast.AsyncFunctionDef.
        Определяет:
        - имя метода, async/sync
        - типы декораторов: property, classmethod, staticmethod
        - is_empty: True если тело метода тривиально (pass / ... / raise NotImplementedError / docstring)
        """
        is_async = isinstance(func_node, ast.AsyncFunctionDef)
        name = getattr(func_node, "name", "<unknown>")
        lineno = getattr(func_node, "lineno", 0)

        decorator_kinds: List[str] = []

        # разбираем список декораторов: @property, @classmethod, @staticmethod
        for dec in getattr(func_node, "decorator_list", []):
            kind = self._classify_decorator(dec)
            if kind is not None:
                decorator_kinds.append(kind)

        # определяем, является ли метод тривиальным (пустым для целей LCOM4)
        is_empty = self._is_empty_method(func_node)

        return MethodInfo(
            name=name,
            lineno=lineno,
            is_async=is_async,
            decorator_kinds=decorator_kinds,
            used_attributes=set(),
            called_methods=set(),
            is_empty=is_empty,
        )

    @staticmethod
    def _is_empty_method(func_node: ast.AST) -> bool:
        """
        Определяет, является ли метод тривиальным — не несущим реальной логики.

        Тривиальные тела (в любой комбинации):
          - pass
          - ... (Ellipsis — типичная заглушка в интерфейсах/Protocol)
          - raise NotImplementedError(...) или raise NotImplementedError
          - строка-докстринг (ast.Expr с ast.Constant[str])

        Если тело состоит исключительно из таких инструкций — метод считается пустым
        и исключается из графа LCOM4, чтобы не искажать метрику связности.
        """
        body: list = getattr(func_node, "body", [])
        if not body:
            return True

        for stmt in body:
            # pass
            if isinstance(stmt, ast.Pass):
                continue

            # ... (Ellipsis как выражение-заглушка)
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is ...
            ):
                continue

            # строка-докстринг: первый ast.Expr с ast.Constant[str]
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue

            # raise NotImplementedError / raise NotImplementedError(...)
            if isinstance(stmt, ast.Raise) and stmt.exc is not None:
                exc = stmt.exc
                # raise NotImplementedError()  или  raise NotImplementedError("msg")
                is_nie_call = (
                    isinstance(exc, ast.Call)
                    and isinstance(exc.func, ast.Name)
                    and exc.func.id == "NotImplementedError"
                )
                # raise NotImplementedError  (без скобок)
                is_nie_name = (
                    isinstance(exc, ast.Name)
                    and exc.id == "NotImplementedError"
                )
                if is_nie_call or is_nie_name:
                    continue

            # встретили нетривиальную инструкцию — метод не пустой
            return False

        return True

    def _classify_decorator(self, dec: ast.AST) -> Optional[str]:
        """
        Классифицирует декоратор в одно из значений:
        "property", "classmethod", "staticmethod".
        Если это другой декоратор, возвращает None.
        """
        # @property
        if isinstance(dec, ast.Name) and dec.id == "property":
            return "property"

        # @classmethod / @staticmethod
        if isinstance(dec, ast.Name) and dec.id in ("classmethod", "staticmethod"):
            return dec.id

        # варианты вида @something.classmethod
        if isinstance(dec, ast.Attribute) and isinstance(dec.value, ast.Name):
            full_name = f"{dec.value.id}.{dec.attr}"
            if full_name.endswith(".classmethod"):
                return "classmethod"
            if full_name.endswith(".staticmethod"):
                return "staticmethod"

        return None

    # ================================
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ДЛЯ АТРИБУТОВ
    # ================================

    def _extract_names_from_assign(self, node: ast.Assign) -> List[str]:
        """
        Извлекает имена атрибутов из простого присваивания на уровне класса.
        Примеры:
          foo = 1           -> ["foo"]
          x, y = 1, 2       -> ["x", "y"]
        Все, что не является простым Name, игнорируется.
        """
        names: List[str] = []

        for target in node.targets:
            # простое имя: foo = 1
            if isinstance(target, ast.Name):
                names.append(target.id)
            # список имен: x, y = 1, 2
            elif isinstance(target, ast.Tuple):
                for elt in target.elts:
                    if isinstance(elt, ast.Name):
                        names.append(elt.id)

        return names

    def _extract_name_from_ann_assign(self, node: ast.AnnAssign) -> Optional[str]:
        """
        Извлекает имя атрибута из аннотированного присваивания на уровне класса.
        Примеры:
          name: str           -> "name"
          age: int = 0        -> "age"
          score: int = Field() -> "score"
        Если target не является ast.Name, возвращаем None.
        """
        target = node.target

        if isinstance(target, ast.Name):
            return target.id

        return None

    # ================================
    # СБОР self.xxx ИЗ __init__
    # ================================

    def _collect_instance_attributes_from_init(self, class_info: ClassInfo, class_node: ast.ClassDef) -> None:
        """
        Находит в __init__ присваивания вида self.xxx = ... и добавляет имена xxx
        в class_info.attributes, чтобы их можно было учитывать как поля экземпляра.

        Используется как для текущего класса (Pass 1), так и для предков (Pass 2 / _enrich_with_ancestor_attributes).
        """
        for node in class_node.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != "__init__":
                continue

            # ищем в теле __init__ присваивания self.xxx = ...
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        attr_name = self._extract_instance_attr_from_target(target)
                        if attr_name is not None:
                            class_info.attributes.add(attr_name)
                elif isinstance(stmt, ast.AnnAssign):
                    # случаи вроде self.xxx: Type = value
                    attr_name = self._extract_instance_attr_from_target(stmt.target)
                    if attr_name is not None:
                        class_info.attributes.add(attr_name)

    def _extract_instance_attr_from_target(self, target: ast.AST) -> Optional[str]:
        """
        Извлекает имя атрибута экземпляра из левой части присваивания:
          self.xxx = ...
          self.xxx: Type = ...
        Возвращает 'xxx' или None, если это не self.xxx/cls.xxx.
        """
        # self.xxx или cls.xxx
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            if target.value.id in ("self", "cls"):
                return target.attr
        return None

    # ================================
    # ЗАПОЛНЕНИЕ used_attributes И called_methods
    # ================================

    def _populate_method_usage(self, class_info: ClassInfo, class_node: ast.ClassDef) -> None:
        """
        Для каждого метода класса:
        - обходит тело метода;
        - заполняет used_attributes (какие атрибуты класса/экземпляра используются);
        - заполняет called_methods (какие методы данного класса вызываются).

        Вызывается на Pass 1 — до обогащения атрибутами предков.
        После Pass 2 (_enrich_with_ancestor_attributes) унаследованные атрибуты
        добавляются в class_info.attributes, и _MethodUsageVisitor корректно
        их находит при следующем обходе через _repopulate_method_usage.
        """
        methods_by_name: Dict[str, MethodInfo] = {m.name: m for m in class_info.methods}
        method_names: Set[str] = set(methods_by_name.keys())

        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            method_name = node.name
            method_info = methods_by_name.get(method_name)
            if method_info is None:
                continue

            # флаг: является ли текущий метод @staticmethod
            is_static = "staticmethod" in method_info.decorator_kinds

            visitor = _MethodUsageVisitor(
                class_attributes=class_info.attributes,
                method_names=method_names,
                is_static=is_static,
            )
            visitor.visit(node)

            method_info.used_attributes = visitor.used_attributes
            method_info.called_methods = visitor.called_methods

    def _repopulate_method_usage(self, class_info: ClassInfo, class_node: ast.ClassDef) -> None:
        """
        Повторный проход _MethodUsageVisitor после Pass 2 (обогащения атрибутами предков).

        На Pass 1 _populate_method_usage уже запустился, но class_info.attributes тогда
        содержал только атрибуты самого класса — унаследованные self.xxx ещё не были добавлены.
        После _enrich_with_ancestor_attributes атрибуты предков попали в class_info.attributes,
        поэтому нужно перезапустить Visitor, чтобы used_attributes методов дочернего класса
        включили унаследованные поля как реальные связи в графе LCOM4.

        Вызывается из _collect_classes в конце Pass 2 — только для классов,
        у которых атрибуты действительно расширились (attrs_before != attrs_after).
        """
        methods_by_name: Dict[str, MethodInfo] = {m.name: m for m in class_info.methods}
        method_names: Set[str] = set(methods_by_name.keys())

        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            method_name = node.name
            method_info = methods_by_name.get(method_name)
            if method_info is None:
                continue

            # флаг: является ли текущий метод @staticmethod
            is_static = "staticmethod" in method_info.decorator_kinds

            visitor = _MethodUsageVisitor(
                class_attributes=class_info.attributes,
                method_names=method_names,
                is_static=is_static,
            )
            visitor.visit(node)

            # перезаписываем used_attributes и called_methods обновлёнными значениями
            method_info.used_attributes = visitor.used_attributes
            method_info.called_methods = visitor.called_methods

    # ================================
    # РАСЧЕТ МЕТРИКИ LCOM4
    # ================================

    def _compute_lcom4(self, class_info: ClassInfo) -> tuple[int, int]:
        """
        Считает LCOM4 для данного класса.

        Алгоритм:
        - берем только методы, исключая:
            * __init__ (LCOM4-конвенция)
            * property-методы (не участвуют в связности классов)
            * пустые методы (is_empty=True: pass / ... / raise NotImplementedError / docstring)
              — они не несут логики и не образуют реальных связей в графе
        - строим неориентированный граф:
          вершины = имена методов;
          ребра между A и B, если:
            * used_attributes(A) ∩ used_attributes(B) != ∅, или
            * A вызывает B или B вызывает A
        - LCOM4 = число связных компонент в этом графе
        - если после фильтрации нет методов, возвращаем (0, 0)
        """
        methods = [
            m for m in class_info.methods
            if m.name != "__init__"
            and "property" not in m.decorator_kinds
            and not m.is_empty  # исключаем тривиальные заглушки из графа
        ]

        methods_count = len(methods)
        if methods_count == 0:
            return 0, 0

        # инициализируем граф: вершины -> множество соседей
        adjacency: Dict[str, Set[str]] = {m.name: set() for m in methods}

        # 1. ребра по общим атрибутам
        for i, m1 in enumerate(methods):
            for j in range(i + 1, len(methods)):
                m2 = methods[j]
                # пересечение используемых атрибутов
                if m1.used_attributes and m2.used_attributes:
                    if m1.used_attributes.intersection(m2.used_attributes):
                        adjacency[m1.name].add(m2.name)
                        adjacency[m2.name].add(m1.name)

        # 2. ребра по вызовам методов
        name_to_method: Dict[str, MethodInfo] = {m.name: m for m in methods}
        for m in methods:
            for called in m.called_methods:
                if called in name_to_method:
                    adjacency[m.name].add(called)
                    adjacency[called].add(m.name)

        # 3. подсчет связных компонент через итеративный DFS
        visited: Set[str] = set()
        components = 0

        def dfs(node_name: str) -> None:
            stack = [node_name]
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                for neighbor in adjacency[current]:
                    if neighbor not in visited:
                        stack.append(neighbor)

        for method_name in adjacency.keys():
            if method_name not in visited:
                components += 1
                dfs(method_name)

        return components, methods_count


class _MethodUsageVisitor(ast.NodeVisitor):
    """
    AST-посетитель для одного метода класса.
    Собирает:
    - used_attributes: обращения к атрибутам класса/экземпляра
      (self.field, cls.field), если field есть в class_attributes
    - called_methods: вызовы методов класса (self.method(), cls.method(), method(),
      super().method(), super(ClassName, self).method())

    Параметр is_static управляет регистрацией self-подобных имен:
    - False (по умолчанию): первый параметр метода регистрируется как self/cls-подобное имя
    - True (@staticmethod): регистрация пропускается — первый параметр не является
      self/cls, его ложная регистрация приводила бы к spurious ребрам в графе LCOM4

    Важно: вложенные def/async def внутри метода намеренно НЕ обходятся.
    Это предотвращает загрязнение графа LCOM4 атрибутами и вызовами из замыканий,
    где первый аргумент (self/cls) принадлежит вложенной функции, а не классу.
    """

    def __init__(
        self,
        class_attributes: Set[str],
        method_names: Set[str],
        is_static: bool = False,
    ) -> None:
        self.class_attributes = class_attributes
        self.method_names = method_names
        # флаг staticmethod: при True _register_self_like_names не вызывается
        self._is_static = is_static

        self.used_attributes: Set[str] = set()
        self.called_methods: Set[str] = set()

        # имена, играющие роль self/cls (первый аргумент метода)
        self._self_like_names: Set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        # для @staticmethod первый параметр — обычный аргумент, не self/cls;
        # регистрируем self-подобное имя только для instance/class-методов
        if not self._is_static:
            self._register_self_like_names(node)
        # обходим только прямые потомки тела — вложенные FunctionDef пропускаются,
        # чтобы их 'self' не загрязнял граф LCOM4 внешнего метода
        for stmt in node.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(stmt)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        # аналогично для async def — guard на staticmethod применяется одинаково
        if not self._is_static:
            self._register_self_like_names(node)
        for stmt in node.body:
            if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(stmt)

    def _register_self_like_names(self, node: ast.AST) -> None:
        """Регистрируем имена, которые играют роль self/cls (первый параметр метода)."""
        args = getattr(node, "args", None)
        if args and getattr(args, "args", None):
            first_arg = args.args[0]
            if isinstance(first_arg, ast.arg) and first_arg.arg:
                self._self_like_names.add(first_arg.arg)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        """
        Обращения к атрибутам: self.field, cls.field.
        Если field есть в class_attributes и base_name является self/cls,
        считаем это использованием атрибута.
        """
        if isinstance(node.value, ast.Name):
            base_name = node.value.id
            attr_name = node.attr

            if base_name in self._self_like_names and attr_name in self.class_attributes:
                self.used_attributes.add(attr_name)

        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        """
        Вызовы функций/методов: self.method(), cls.method(), method(),
        super().method(), super(ClassName, self).method().
        Если имя совпадает с методом класса, считаем это вызовом метода класса.
        """
        # self.method(...) / cls.method(...)
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base_name = node.func.value.id
            method_name = node.func.attr
            if base_name in self._self_like_names and method_name in self.method_names:
                self.called_methods.add(method_name)

        # super().method(...) / super(ClassName, self).method(...)
        elif (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "super"
        ):
            # node.func.attr — имя вызываемого метода из родительского класса
            method_name = node.func.attr
            if method_name in self.method_names:
                self.called_methods.add(method_name)

        # прямой вызов method(...) — работает и для @staticmethod
        elif isinstance(node.func, ast.Name):
            method_name = node.func.id
            if method_name in self.method_names:
                self.called_methods.add(method_name)

        self.generic_visit(node)
