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
# 5. Агрегация метрик (meanCohesionAll, meanCohesionMultimethod, lowCohesionCount) для формирования сводки отчета пайплайна.
# ===================================================================================================


import ast
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from solid_dashboard.interfaces.analyzer import IAnalyzer

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


@dataclass
class ClassInfo:
    """Информация о классе: основа для расчета LCOM4."""
    name: str                          # имя класса (UserService, ArticleRepository, ...)
    filepath: str                      # абсолютный путь к файлу, где объявлен класс
    lineno: int                        # строка объявления class
    methods: List[MethodInfo] = field(default_factory=list)
    attributes: Set[str] = field(default_factory=set)
    # attributes: множество имен полей класса/экземпляра
    # (уровень класса + self.xxx из __init__)
    kind: str = "concrete"
    # kind: семантический тип класса — "concrete" / "abstract" / "interface" / "dataclass"
    # заполняется методом _classify_class на этапе _build_class_info


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

        # передаем ignore_dirs в сборщик
        classes_info: List[ClassInfo] = self._collect_classes(target_path, ignore_dirs)

        # считаем LCOM4 для каждого класса и формируем результирующий список
        class_results: List[Dict[str, Any]] = []

        # сырые значения LCOM4 по всем классам, где methods_count > 0
        cohesion_values_all: List[float] = []

        # значения LCOM4 только по классам с methods_count >= 2
        cohesion_values_multi_method: List[float] = []
        analyzed_classes_multi_method = 0

        low_cohesion_count = 0

        for class_info in classes_info:
            lcom4, methods_count = self._compute_lcom4(class_info)
            if methods_count == 0:
                # классы без методов для метрики не интересны
                continue

            cohesion_score = float(lcom4)

            class_results.append({
                "name": class_info.name,
                "methods_count": methods_count,
                "cohesion_score": cohesion_score,
                "filepath": class_info.filepath,
                "lineno": class_info.lineno,
            })

            # добавляем в "сырую" выборку всех классов с методами
            cohesion_values_all.append(cohesion_score)

            # отдельно собираем многометодные классы (methods_count >= 2)
            if methods_count >= 2:
                cohesion_values_multi_method.append(cohesion_score)
                analyzed_classes_multi_method += 1

            # используем конфигурируемый порог вместо захардкоженного 1
            if lcom4 > low_cohesion_threshold:
                low_cohesion_count += 1

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
            # среднее по всем классам с хотя бы одним методом
            "mean_cohesion_all": round(mean_cohesion_all, 2),
            # среднее только по классам с methods_count >= 2
            "mean_cohesion_multi_method": round(mean_cohesion_multi, 2),
            # сколько классов реально попало во второе среднее
            "analyzed_classes_count": analyzed_classes_multi_method,
            "low_cohesion_count": low_cohesion_count,
            # порог, использованный при подсчете low_cohesion_count — для прозрачности отчета
            "low_cohesion_threshold": low_cohesion_threshold,
            "classes": class_results,
        }

    # ================================
    # СБОР КЛАССОВ, МЕТОДОВ И АТРИБУТОВ
    # ================================

    def _collect_classes(self, target_path: Path, ignore_dirs: set) -> List[ClassInfo]:
        classes: List[ClassInfo] = []

        for root, dirs, files in os.walk(target_path):
            # архитектурный трюк: in-place модификация dirs запрещает
            # os.walk спускаться в игнорируемые директории
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

                # проходим по всем классам в файле
                for node in ast.walk(tree):
                    if isinstance(node, ast.ClassDef):
                        class_info = self._build_class_info(node, file_path)
                        # сначала собираем self.xxx из __init__, чтобы добавить в attributes
                        self._collect_instance_attributes_from_init(class_info, node)
                        # затем наполняем used_attributes / called_methods
                        self._populate_method_usage(class_info, node)
                        classes.append(class_info)

        return classes

    def _build_class_info(self, class_node: ast.ClassDef, file_path: Path) -> ClassInfo:
        """
        Строит ClassInfo для одного ast.ClassDef:
        - имя класса, путь к файлу, строка объявления
        - список методов
        - множество атрибутов класса (уровень class body)
        - kind: семантический тип класса (определяется через _classify_class)
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

        # определяем семантический тип класса по AST — class_node доступен здесь напрямую
        class_info.kind = self._classify_class(class_node)

        return class_info

    def _classify_class(self, class_node: ast.ClassDef) -> str:
        """
        Определяет семантический тип класса по AST-узлу.

        Возвращает одно из четырех значений:
          "interface"  — все non-dunder методы абстрактны (только @abstractmethod / pass / raise)
                         И класс наследуется от ABC или Protocol
          "abstract"   — наследуется от ABC/Protocol, но есть хотя бы один конкретный метод
          "dataclass"  — декоратор @dataclass ИЛИ базовый класс BaseModel / declarative Base
          "concrete"   — всё остальное (дефолт)

        Порядок проверок важен: dataclass проверяется первым, затем ABC/Protocol-иерархия.
        """
        # --- вспомогательные множества имен для быстрой проверки ---

        # имена базовых классов (только простые Name и Attribute.attr, без полных путей)
        base_names: Set[str] = set()
        for base in class_node.bases:
            if isinstance(base, ast.Name):
                base_names.add(base.id)
            elif isinstance(base, ast.Attribute):
                # учитываем последний компонент: models.Model -> "Model"
                base_names.add(base.attr)

        # имена декораторов класса (только простые Name)
        class_decorator_names: Set[str] = set()
        for dec in class_node.decorator_list:
            if isinstance(dec, ast.Name):
                class_decorator_names.add(dec.id)
            elif isinstance(dec, ast.Attribute):
                class_decorator_names.add(dec.attr)

        # --- 1. dataclass: @dataclass ИЛИ BaseModel / Base в bases ---
        _DATACLASS_BASES = {"BaseModel", "Base", "DeclarativeBase", "DeclarativeBaseNoMeta"}
        if "dataclass" in class_decorator_names or base_names & _DATACLASS_BASES:
            return "dataclass"

        # --- 2. проверяем ABC/Protocol в иерархии ---
        _ABSTRACT_BASES = {"ABC", "Protocol", "ABCMeta"}
        is_abc_derived = bool(base_names & _ABSTRACT_BASES)

        if not is_abc_derived:
            # нет ABC/Protocol в bases — класс конкретный
            return "concrete"

        # --- 3. считаем abstractmethod-методы среди non-dunder методов ---
        # non-dunder: все методы, кроме __xxx__ (магические методы не участвуют в классификации)
        non_dunder_count = 0
        abstract_method_count = 0

        for node in class_node.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            method_name: str = node.name
            if method_name.startswith("__") and method_name.endswith("__"):
                continue
            non_dunder_count += 1  # просто считаем, узел не храним

            for dec in node.decorator_list:
                if (...):
                    abstract_method_count += 1
                    break

        if non_dunder_count == 0:
            return "interface"
        if abstract_method_count == non_dunder_count:
            return "interface"
        return "abstract"

    def _build_method_info(self, func_node: ast.AST) -> MethodInfo:
        """
        Строит MethodInfo для ast.FunctionDef или ast.AsyncFunctionDef.
        Определяет:
        - имя метода, async/sync
        - типы декораторов: property, classmethod, staticmethod
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

        return MethodInfo(
            name=name,
            lineno=lineno,
            is_async=is_async,
            decorator_kinds=decorator_kinds,
            used_attributes=set(),
            called_methods=set(),
        )

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

            visitor = _MethodUsageVisitor(
                class_attributes=class_info.attributes,
                method_names=method_names,
            )
            visitor.visit(node)

            method_info.used_attributes = visitor.used_attributes
            method_info.called_methods = visitor.called_methods

    # ================================
    # РАСЧЕТ МЕТРИКИ LCOM4
    # ================================

    def _compute_lcom4(self, class_info: ClassInfo) -> tuple[int, int]:
        """
        Считает LCOM4 для данного класса.

        Алгоритм:
        - берем только методы, кроме __init__ (LCOM4-конвенция)
          и property-методов (они не участвуют в связности классов)
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
            if m.name != "__init__" and "property" not in m.decorator_kinds
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
    - called_methods: вызовы методов класса (self.method(), cls.method(), method())
    """

    def __init__(self, class_attributes: Set[str], method_names: Set[str]) -> None:
        self.class_attributes = class_attributes
        self.method_names = method_names

        self.used_attributes: Set[str] = set()
        self.called_methods: Set[str] = set()

        # имена, играющие роль self/cls (первый аргумент метода)
        self._self_like_names: Set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._register_self_like_names(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._register_self_like_names(node)
        self.generic_visit(node)

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
        Вызовы функций/методов: self.method(), cls.method(), method().
        Если имя совпадает с методом класса, считаем это вызовом метода класса.
        """
        # self.method(...) / cls.method(...)
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
            base_name = node.func.value.id
            method_name = node.func.attr
            if base_name in self._self_like_names and method_name in self.method_names:
                self.called_methods.add(method_name)

        # прямой вызов method(...)
        if isinstance(node.func, ast.Name):
            method_name = node.func.id
            if method_name in self.method_names:
                self.called_methods.add(method_name)

        self.generic_visit(node)
