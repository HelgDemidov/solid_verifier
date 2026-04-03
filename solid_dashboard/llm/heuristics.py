"""
Шаг 1b пайплайна LLM-анализа: эвристический анализ ProjectMap.

Реализует identify_candidates() — чистую функцию от ProjectMap к HeuristicResult.
Не обращается к файловой системе: весь необходимый код уже находится в ProjectMap.

Реализованные эвристики (по нарастанию сложности):
    LSP-H-001 — raise NotImplementedError в переопределенном методе
    LSP-H-002 — пустое тело переопределенного метода (pass или docstring)
    OCP-H-001 — цепочки if/elif с isinstance() >= 3 ветвей
    LSP-H-004 — __init__ без вызова super().__init__()
    OCP-H-002 — match/case на типах
    LSP-H-003 — isinstance в коде с параметром базового типа
    OCP-H-004 — высокая цикломатическая сложность + isinstance

"""

import ast
import logging
from typing import List, cast
from collections import defaultdict

from .types import (
    CandidateType,
    ClassInfo,
    Finding,
    FindingDetails,
    HeuristicResult,
    LlmCandidate,
    ProjectMap,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Приоритеты эвристик при конфликте для одного метода
_FINDING_PRIORITY: dict[str, int] = {
    "OCP-H-001": 2,
    "OCP-H-004": 1,
    "LSP-H-001": 2,
    "LSP-H-002": 1,
}


# ---------------------------------------------------------------------------
# Дефолтные паттерны исключения нерелевантных путей
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDE_PATTERNS = [
    "tests/",
    "test_",
    "_test.py",
    "conftest.py",
    "migrations/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "node_modules/",
    "setup.py",
    "manage.py",
]

# ---------------------------------------------------------------------------
# Вспомогательные функции фильтрации путей
# ---------------------------------------------------------------------------

def _normalize_path_for_matching(path: str) -> str:
    # Приводим путь к единому виду для стабильного substring-matching
    # на Windows/Linux/macOS: слэши унифицируем, регистр понижаем.
    return path.replace("\\", "/").lower()


def _should_exclude_path(
    file_path: str,
    exclude_patterns: list[str] | None,
) -> bool:
    # Если передан None, используем дефолтный набор паттернов из стратегии.
    patterns = _DEFAULT_EXCLUDE_PATTERNS if exclude_patterns is None else exclude_patterns

    # Нормализуем путь один раз, чтобы паттерны вида "tests/" работали и
    # для Windows-путей вроде "C:\\repo\\tests\\test_foo.py".
    normalized_path = _normalize_path_for_matching(file_path)

    # Нормализуем и паттерны, чтобы матчинг был регистронезависимым и
    # не зависел от стиля разделителей.
    for pattern in patterns:
        normalized_pattern = _normalize_path_for_matching(pattern)
        if normalized_pattern and normalized_pattern in normalized_path:
            return True

    return False


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Удаляет дублирующиеся findings для одного и того же метода.

    Ключ группировки: (file, class_name, method_name).
    Внутри группы оставляем finding с максимальным приоритетом.
    При удалении менее приоритетных добавляем их rule в explanation
    winning-finding-a.
    """
    # Группируем по (file, class_name, method_name)
    groups: dict[tuple[str, str, str | None], list[Finding]] = defaultdict(list)
    for f in findings:
            # Безопасно достаем method_name, даже если details == None
            method_name: str | None = None
            if f.details is not None:
                method_name = f.details.method_name

            key = (f.file, f.class_name or "", method_name)
            groups[key].append(f)

    result: list[Finding] = []

    for key, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue

        # Ищем победителя по таблице приоритетов
        def _priority(f: Finding) -> int:
            return _FINDING_PRIORITY.get(f.rule, 0)

        group_sorted = sorted(group, key=_priority, reverse=True)
        winner = group_sorted[0]
        losers = group_sorted[1:]

        # Собираем правила проигравших, чтобы добавить в explanation
        extra_rules = [f.rule for f in losers if f.rule != winner.rule]
        if extra_rules and winner.details is not None:
            suffix = " Also detected: " + ", ".join(sorted(set(extra_rules))) + "."

            # Нормализуем explanation к строке перед конкатенацией
            base_expl = winner.details.explanation or ""
            winner.details.explanation = base_expl + suffix

        result.append(winner)

    return result

def _deduplicate_candidates(candidates: list[LlmCandidate]) -> list[LlmCandidate]:
    """
    Объединяет LlmCandidate для одного и того же класса/файла.

    Ключ: (file_path, class_name). Для каждого ключа остается один кандидат:
    - heuristic_reasons объединяются (set-объединение),
    - priority берется максимальный,
    - candidate_type агрегируется ("ocp"/"lsp" -> "both" при необходимости).
    """
    by_class: dict[tuple[str, str], LlmCandidate] = {}

    for c in candidates:
        key = (c.file_path, c.class_name)
        existing = by_class.get(key)
        if existing is None:
            by_class[key] = c
            continue

        # Объединяем причины
        combined_reasons = sorted(set(existing.heuristic_reasons + c.heuristic_reasons))
        existing.heuristic_reasons = combined_reasons

        # Приоритет: максимальный
        existing.priority = max(existing.priority, c.priority)

        # Тип кандидата: агрегируем "ocp"/"lsp"/"both"
        if existing.candidate_type != c.candidate_type:
            # Если хотя бы один уже "both" -> оставляем "both"
            if existing.candidate_type == "both" or c.candidate_type == "both":
                existing.candidate_type = "both"  # type: ignore[assignment]
            else:
                # Один "ocp", другой "lsp" -> итог "both"
                existing.candidate_type = "both"  # type: ignore[assignment]

    return list(by_class.values())

def _is_abstract_class(class_info: ClassInfo, project_map: ProjectMap) -> bool:
    """
    Определяет, является ли класс абстрактным, по данным ProjectMap.

    Условия (достаточно любого):
      1. Среди parent_classes есть "ABC"
      2. Имя класса присутствует в project_map.interfaces
      3. В классе есть хотя бы один метод с is_abstract=True
    """
    # 1. Явное наследование от ABC (class Foo(ABC))
    if "ABC" in class_info.parent_classes:
        return True

    # 2. Класс объявлен как интерфейс/Protocol в ProjectMap
    if class_info.name in project_map.interfaces:
        return True

    # 3. Есть хотя бы один метод, помеченный @abstractmethod
    if any(m.is_abstract for m in class_info.methods):
        return True

    return False

def _parse_class_ast(source_code: str, class_name: str) -> ast.ClassDef | None:
    """
    Парсит исходный код класса и возвращает его AST-ноду.
    Возвращает None при ошибке синтаксиса или если нода не найдена.
    """
    if not source_code.strip():
        return None
    try:
        tree = ast.parse(source_code)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
    except SyntaxError:
        logger.warning("SyntaxError parsing source for class %s", class_name)
    return None


def _has_isinstance_call(node: ast.expr) -> bool:
    """Проверяет, содержит ли выражение вызов isinstance()."""
    for subnode in ast.walk(node):
        if (
            isinstance(subnode, ast.Call)
            and isinstance(subnode.func, ast.Name)
            and subnode.func.id == "isinstance"
        ):
            return True
    return False


def _count_elif_chain(if_node: ast.If) -> int:
    """
    Считает длину цепочки if/elif.
    Возвращает общее число ветвей (1 для одиночного if, 2 для if+elif и т.д.).
    """
    count = 1
    current = if_node
    # Каждый elif — это ast.If в orelse предыдущего if
    while (
        current.orelse
        and len(current.orelse) == 1
        and isinstance(current.orelse[0], ast.If)
    ):
        count += 1
        current = current.orelse[0]
    return count

# ---------------------------------------------------------------------------
# Вспомогательная функция: цикломатическая сложность метода (по Маккейбу)
# ---------------------------------------------------------------------------

# Типы AST-нод, каждая из которых добавляет +1 к цикломатической сложности.
# Список соответствует формуле Маккейба: каждый узел ветвления — это
# отдельный «путь» исполнения внутри метода.
_CC_NODE_TYPES = (
    ast.If,        # if / elif (elif в AST тоже представлен как ast.If)
    ast.For,       # цикл for
    ast.While,     # цикл while
    ast.ExceptHandler,  # блок except
    ast.With,      # with (каждый with — потенциальный путь)
    ast.Assert,    # assert (может не выполниться)
    ast.comprehension,  # генераторные выражения ([x for x in ...])
)

# Бинарные булевы операторы тоже добавляют к CC:
# `a and b` и `a or b` создают дополнительный путь.
_CC_BOOL_OPS = (ast.And, ast.Or)

# ---------------------------------------------------------------------------
# Корректный обход AST метода: без захода во вложенные функции и классы
# ---------------------------------------------------------------------------

def _iter_method_nodes(func: ast.FunctionDef | ast.AsyncFunctionDef):
    """
    Генератор: обходит все AST-ноды тела метода, НЕ заходя в поддеревья
    вложенных функций, async-функций и вложенных классов.

    Зачем это нужно: стандартный ast.walk() обходит ВСе дерево целиком,
    включая вложенные FunctionDef/ClassDef. Это приводит к тому, что if/for/while
    внутри вложенной функции ошибочно увеличивают CC внешнего метода.

    Пример некорректного подсчета с ast.walk():
        def process(self, x):       # CC должен быть 2 (base=1 + один if)
            if x > 0:               # +1
                pass
            def inner(y):           # вложенная функция — не считаем ее ноды
                if y < 0:           # это if НЕ должен влиять на CC process()
                    pass

    ast.walk() посчитал бы CC=3 (оба if). Наш генератор дает правильное CC=2.

    Алгоритм: итерируем через ast.iter_child_nodes() вместо ast.walk(),
    при встрече вложенного FunctionDef/AsyncFunctionDef/ClassDef — не рекурсируем
    в их детей. Это называется «остановка на границе вложенной области видимости».

    Аргументы:
        func: нода метода верхнего уровня (FunctionDef или AsyncFunctionDef).

    Yields:
        ast.AST: ноды тела метода, исключая поддеревья вложенных функций/классов.
    """
    # Используем явный стек вместо рекурсии — избегаем RecursionError на
    # очень глубоких AST (теоретически возможно в автогенерируемом коде).
    stack: list[ast.AST] = [func]

    while stack:
        node = stack.pop()
        yield node

        for child in ast.iter_child_nodes(node):
            # Встретили вложенную область видимости (функцию или класс) —
            # не рекурсируем внутрь: у нее своя CC, это отдельная единица анализа.
            # Исключение: сам корневой func — он попал в стек изначально и
            # должен быть обработан. Проверяем по identity (node is not func),
            # чтобы не пропустить корень при первой итерации.
            if child is not func and isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                # Саму ноду вложенной функции/класса в стек НЕ кладем —
                # мы вообще не собираемся ее обходить.
                continue
            stack.append(child)


def _compute_method_cc(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """
    Считает цикломатическую сложность (CC) метода по формуле Маккейба.

    CC = 1 + количество узлов ветвления в теле метода.
    Базовое значение 1 соответствует «одному сквозному пути» без ветвлений.

    Использует _iter_method_nodes() вместо ast.walk(), чтобы корректно
    игнорировать вложенные функции и классы — они являются самостоятельными
    единицами анализа и не должны влиять на CC внешнего метода.

    Учитывает:
        - if / elif (оба — ast.If)
        - for, while, with, assert, comprehension, ExceptHandler
        - булевы операторы and/or (каждый оператор в цепочке = +1)
        - тернарный оператор (IfExp): x if cond else y

    Аргументы:
        func: нода метода (FunctionDef или AsyncFunctionDef).

    Возвращает:
        int: цикломатическая сложность >= 1.
    """
    # Базовое значение: один путь «насквозь» при отсутствии ветвлений
    cc = 1

    for node in _iter_method_nodes(func):
        # Узлы ветвления: каждый добавляет один дополнительный путь исполнения
        if isinstance(node, _CC_NODE_TYPES):
            cc += 1

        # BoolOp: цепочка `a and b and c` — это n-1 операторов для n операндов.
        # Каждый and/or создает дополнительный путь (short-circuit evaluation).
        if isinstance(node, ast.BoolOp) and isinstance(node.op, _CC_BOOL_OPS):
            cc += len(node.values) - 1

        # IfExp: тернарный оператор `x if cond else y` — это тоже ветвление
        if isinstance(node, ast.IfExp):
            cc += 1

    return cc

def _make_finding(
    rule: str,
    class_info: ClassInfo,
    message: str,
    principle: str,
    explanation: str,
    suggestion: str,
    method_name: str | None = None,  # NEW
) -> Finding:
    """Фабричная функция для создания эвристического Finding."""
    return Finding(
        rule=rule,
        file=class_info.file_path,
        class_name=class_info.name,
        line=None,           # LLM и эвристики не указывают точную строку
        severity="warning",
        message=message,
        source="heuristic",
        details=FindingDetails(
            principle=principle,
            explanation=explanation,
            suggestion=suggestion,
            method_name=method_name,  # NEW
        ),
    )

# ---------------------------------------------------------------------------
# LSP-H-001: raise NotImplementedError в переопределенном методе
# ---------------------------------------------------------------------------

def _check_lsp_h_001(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
    project_map: ProjectMap,
) -> List[Finding]:
    """
    Ищет переопределенные методы, которые только бросают NotImplementedError.
    Это нарушение LSP: вызывающий код не может полагаться на замену базового
    класса подклассом, если последний не реализует контракт.
    """

        # NEW: абстрактные классы считаются контрактами, а не нарушениями LSP
    if _is_abstract_class(class_info, project_map):
        return []
    
    # Множество имен методов, помеченных is_override при Шаге 2
    override_names = {m.name for m in class_info.methods if m.is_override}
    if not override_names:
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Проверяем только методы, которые переопределяют родительские
        if func.name not in override_names:
            continue

        # Обходим все тело метода в поисках raise NotImplementedError
        for node in ast.walk(func):
            if not isinstance(node, ast.Raise):
                continue
            # Поддерживаем как raise NotImplementedError, так и raise NotImplementedError("msg")
            exc = node.exc
            if exc is None:
                continue
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                findings.append(_make_finding(
                    rule="LSP-H-001",
                    class_info=class_info,
                    message=(
                        f"Overridden method '{func.name}' raises NotImplementedError — "
                        f"substitutability is broken"
                    ),
                    principle="LSP",
                    explanation=(
                        f"Method '{func.name}' overrides a parent method but raises "
                        f"NotImplementedError. Callers using the base type cannot substitute "
                        f"this class without catching unexpected exceptions."
                    ),
                    suggestion=(
                        "Implement the method according to the parent's contract, or "
                        "reconsider whether this class should extend the parent at all."
                    ),
                    method_name=func.name,  # NEW
                ))
                break  # Одного finding на метод достаточно
            if (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Name)
                and exc.func.id == "NotImplementedError"
            ):
                findings.append(_make_finding(
                    rule="LSP-H-001",
                    class_info=class_info,
                    message=(
                        f"Overridden method '{func.name}' raises NotImplementedError — "
                        f"substitutability is broken"
                    ),
                    principle="LSP",
                    explanation=(
                        f"Method '{func.name}' overrides a parent method but raises "
                        f"NotImplementedError with a message."
                    ),
                    suggestion=(
                        "Implement the method according to the parent's contract."
                    ),
                    method_name=func.name,  # NEW
                ))
                break

    return findings


# ---------------------------------------------------------------------------
# LSP-H-002: пустое тело переопределенного метода
# ---------------------------------------------------------------------------

def _check_lsp_h_002(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
    project_map: ProjectMap,
) -> List[Finding]:
    """
    Ищет переопределенные методы с пустым телом (только pass или docstring).
    Пустое тело переопределения — признак того, что подкласс не выполняет
    контракт родителя, нарушая принцип подстановки.
    """

    # NEW: абстрактные классы считаются контрактами, а не нарушениями LSP
    if _is_abstract_class(class_info, project_map):
        return []

    override_names = {m.name for m in class_info.methods if m.is_override}
    if not override_names:
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if func.name not in override_names:
            continue

        body = func.body
        if len(body) != 1:
            continue  # Более одного стейтмента — не пустое тело

        stmt = body[0]
        # pass
        is_pass = isinstance(stmt, ast.Pass)
        # Одиночная строка-docstring (ast.Expr с ast.Constant-строкой)
        is_docstring_only = (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )

        if is_pass or is_docstring_only:
            findings.append(_make_finding(
                rule="LSP-H-002",
                class_info=class_info,
                message=(
                    f"Overridden method '{func.name}' has an empty body "
                    f"({'pass' if is_pass else 'docstring only'})"
                ),
                principle="LSP",
                explanation=(
                    f"Method '{func.name}' overrides a parent method but its body is "
                    f"effectively empty. This silently breaks the parent's contract: "
                    f"callers expecting behavior from the base class will get nothing."
                ),
                suggestion=(
                    "Implement the method's expected behavior, or use NotImplementedError "
                    "explicitly if the override is intentionally unsupported (though this "
                    "also violates LSP and should be reconsidered)."
                ),
                method_name=func.name,  # NEW
            ))

    return findings


# ---------------------------------------------------------------------------
# OCP-H-001: цепочки if/elif с isinstance() >= 3 ветвей ИСПРАВЛЕННАЯ ВЕРСИЯ
# ---------------------------------------------------------------------------

def _count_isinstance_branches(if_node: ast.If) -> int:
    """
    Считает количество ветвей if/elif в одной цепочке, 
    которые содержат вызов isinstance().
    """
    count = 0
    current: ast.If | None = if_node

    while current is not None:
        # Проверяем текущее условие (test) на наличие isinstance()
        if _has_isinstance_call(current.test):
            count += 1

        # Переходим к следующему elif, если он есть
        if (
            current.orelse
            and len(current.orelse) == 1
            and isinstance(current.orelse[0], ast.If)
        ):
            current = current.orelse[0]
        else:
            break

    return count

def _check_ocp_h_001(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет методы с цепочками if/elif, содержащими >= 3 проверок isinstance().
    
    В отличие от старой логики, мы считаем не общую длину цепочки, 
    а именно количество ветвей, в которых реально вызывается isinstance().
    Это устраняет ложные срабатывания (когда isinstance только в одной ветке)
    и ложные пропуски (когда цепочка начинается с обычного guard-условия).
    """
    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Собираем ноды, являющиеся внутренними elif
        elif_nodes: set[int] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.If):
                if (
                    node.orelse
                    and len(node.orelse) == 1
                    and isinstance(node.orelse[0], ast.If)
                ):
                    elif_nodes.add(id(node.orelse[0]))

        # Теперь обходим только корневые if
        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue

            if id(node) in elif_nodes:
                continue

            # Считаем именно ветки с isinstance!
            isinstance_branch_count = _count_isinstance_branches(node)

            # Если в одной цепочке 3 и более веток с isinstance - это OCP-smell
            if isinstance_branch_count >= 3:
                chain_length = _count_elif_chain(node) # Для красивого сообщения оставим
                
                findings.append(_make_finding(
                    rule="OCP-H-001",
                    class_info=class_info,
                    message=(
                        f"Method '{func.name}' contains a type-dispatch chain "
                        f"({isinstance_branch_count} isinstance checks) — potential OCP violation"
                    ),
                    principle="OCP",
                    explanation=(
                        f"'{func.name}' uses an if/elif chain of length {chain_length}, "
                        f"where {isinstance_branch_count} branches check concrete types via isinstance(). "
                        f"Mixed type-dispatch like this means adding a new type requires "
                        f"modifying this method directly, violating Open-Closed Principle."
                    ),
                    suggestion=(
                        "Consider replacing the type-checks with polymorphism: "
                        "extract each branch into a subclass or strategy, and let "
                        "Python's dispatch mechanism handle the routing."
                    ),
                    method_name=func.name,
                ))
                break

    return findings

# ---------------------------------------------------------------------------
# LSP-H-004: __init__ без вызова super().__init__()
# ---------------------------------------------------------------------------

def _has_dataclass_decorator(class_node: ast.ClassDef) -> bool:
    """
    Проверяет, помечен ли класс декоратором @dataclass или @dataclasses.dataclass.
    На уровне AST декораторы лежат в списке decorator_list класса.
    """
    for dec in class_node.decorator_list:
        # Простой случай: @dataclass
        if isinstance(dec, ast.Name) and dec.id == "dataclass":
            return True
        
        # Случай с атрибутом: @dataclasses.dataclass
        if isinstance(dec, ast.Attribute) and dec.attr == "dataclass":
            return True
            
        # Случай с вызовом (когда передают аргументы): @dataclass(frozen=True)
        if isinstance(dec, ast.Call):
            # Если вызываемая функция — просто имя: @dataclass(...)
            if isinstance(dec.func, ast.Name) and dec.func.id == "dataclass":
                return True
            # Если вызываемая функция — атрибут: @dataclasses.dataclass(...)
            if isinstance(dec.func, ast.Attribute) and dec.func.attr == "dataclass":
                return True

    return False


# Родительские классы, для которых отсутствие super().__init__() в подклассе
# не считается нарушением LSP-H-004

_LSP_H004_EXCLUDED_PARENTS: set[str] = {
    "object",
    "ABC",
    "Protocol",
    "TypedDict",
    "NamedTuple",
    "BaseModel",
}

def _check_lsp_h_004(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет __init__ в подклассах без вызова super().__init__().
    """
    # NEW: Если это dataclass, отсутствие super().__init__ — это норма,
    # не считаем это нарушением LSP.
    if _has_dataclass_decorator(class_node):
        return []

    # Только для реальных подклассов
    real_parents = [p for p in class_info.parent_classes if p != ""]
    if not real_parents:
        return []

    # NEW: если все реальные родители входят в список исключений,
    # не считаем отсутствие super().__init__() нарушением.
    if all(parent in _LSP_H004_EXCLUDED_PARENTS for parent in real_parents):
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        # Ищем __init__, только синхронный (async __init__ не существует в Python)
        if not isinstance(func, ast.FunctionDef) or func.name != "__init__":
            continue

        has_super_init = False

        # Обходим все тело __init__ в поисках вызова super().__init__() или super().__init__
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue

            # Паттерн: super().__init__(...)
            # AST: Call(func=Attribute(value=Call(func=Name(id='super')), attr='__init__'))
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "__init__"
                and isinstance(node.func.value, ast.Call)
                and isinstance(node.func.value.func, ast.Name)
                and node.func.value.func.id == "super"
            ):
                has_super_init = True
                break

        if not has_super_init:
            findings.append(_make_finding(
                rule="LSP-H-004",
                class_info=class_info,
                message=(
                    f"__init__ in '{class_info.name}' does not call super().__init__() "
                    f"— parent state may be uninitialized"
                ),
                principle="LSP",
                explanation=(
                    f"'{class_info.name}' inherits from {real_parents} but its __init__ "
                    f"does not call super().__init__(). If the parent sets instance "
                    f"attributes in __init__, they will be missing in this subclass, "
                    f"potentially breaking Liskov Substitution."
                ),
                suggestion=(
                    "Add super().__init__() (with appropriate arguments) as the first "
                    "statement in __init__, unless you intentionally want to skip "
                    "parent initialization (document this explicitly if so)."
                ),
                method_name=func.name,  # NEW
            ))

    return findings

# ---------------------------------------------------------------------------
# OCP-H-002: match/case на типах (Python 3.10+)
# ---------------------------------------------------------------------------

def _check_ocp_h_002(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет конструкции match/case, где ветви разбирают конкретные типы.
    См. подробный комментарий в предыдущей версии.
    """
    findings: List[Finding] = []

    # Получаем классы паттернов match из ast, если они доступны (Python 3.10+)
    match_cls = getattr(ast, "Match", None)
    match_class_cls = getattr(ast, "MatchClass", None)
    match_or_cls = getattr(ast, "MatchOr", None)

    # Если наш Python не поддерживает match/case, эвристика ничего не делает
    if match_cls is None or match_class_cls is None:
        return findings

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for node in ast.walk(func):
            # Статический анализатор знает только, что node: ast.AST.
            # После isinstance(node, match_cls) мы можем явно подсказать тип.
            if not isinstance(node, match_cls):
                continue

            match_node = cast(ast.AST, node)   # тип: AST на уровне Pylance
            # Ниже мы все равно обращаемся к .cases, так что уточним тип аккуратнее:
            # Pylance не знает про ast.Match, поэтому мы не можем указать точный тип,
            # но cast нужен в основном для подавления общей ошибки "у AST нет cases".

            type_case_count = 0

            # getattr с default [] гарантирует отсутствие AttributeError даже
            # в гипотетических будущих изменениях AST-API
            for case in getattr(match_node, "cases", []):
                # Паттерн вида case MyClass(): ...
                if isinstance(case.pattern, match_class_cls):
                    type_case_count += 1

                # Паттерн вида case A() | B(): ...
                if match_or_cls is not None and isinstance(case.pattern, match_or_cls):
                    for sub in case.pattern.patterns:
                        if isinstance(sub, match_class_cls):
                            type_case_count += 1

            if type_case_count >= 3:
                findings.append(_make_finding(
                    rule="OCP-H-002",
                    class_info=class_info,
                    message=(
                        f"Method '{func.name}' uses match/case with {type_case_count} "
                        f"type branches — potential OCP violation"
                    ),
                    principle="OCP",
                    explanation=(
                        f"'{func.name}' dispatches behavior via a match/case statement "
                        f"with {type_case_count} type-specific branches. Adding a new "
                        f"type requires modifying this method directly — a violation of OCP."
                    ),
                    suggestion=(
                        "Consider replacing type-dispatch match/case with polymorphism "
                        "or a registration/strategy pattern."
                    ),
                    method_name=func.name,  # NEW
                ))
                break

    return findings


# ---------------------------------------------------------------------------
# LSP-H-003: isinstance в коде, принимающем параметр базового типа
# ---------------------------------------------------------------------------

def _check_lsp_h_003(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
    project_map: ProjectMap,
) -> List[Finding]:
    """
    Ищет методы, у которых параметр аннотирован типом из ProjectMap (т.е. базовым
    классом или интерфейсом нашего проекта), а в теле метода есть isinstance()
    с тем же базовым типом или его подтипами.

    Пример нарушения LSP:
        def process(self, animal: Animal) -> None:
            if isinstance(animal, Dog):     # <-- нарушение!
                animal.bark()
            elif isinstance(animal, Cat):
                animal.meow()

    Код обещает работать с любым Animal, но на деле зависит от конкретного подтипа.
    Это нарушение LSP со стороны потребителя: подтипы не взаимозаменяемы для него.

    Алгоритм:
    1. Для каждого метода класса — смотрим аннотации параметров.
    2. Если аннотация — имя класса из нашего ProjectMap (базовый тип) →
       этот метод "обещает" работать с подтипами.
    3. Ищем isinstance(param, ...) в теле метода.
    4. Если нашли — это нарушение.

    Ограничение: проверяем только простые аннотации (ast.Name), не Union/Optional.
    Это сознательный выбор — сложные аннотации редки в реальных нарушениях и
    требуют нетривиального разворачивания типов.
    """
    # Собираем все известные имена базовых классов из ProjectMap
    # Это классы и интерфейсы нашего проекта — именно они могут быть "базовым типом"
    known_base_names: set[str] = set(project_map.classes.keys()) | set(project_map.interfaces.keys())

    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # --- Шаг 1: Собираем имена параметров с аннотацией базового типа ---
        # args.args — позиционные параметры, включая self
        # Пропускаем 'self' и 'cls' — они всегда первые и не несут типовой аннотации
        annotated_base_params: set[str] = set()

        for arg in func.args.args:
            if arg.arg in ("self", "cls"):
                continue
            if arg.annotation is None:
                continue
            # Простая аннотация: def foo(self, x: Animal)
            # AST: arg.annotation = ast.Name(id='Animal')
            if isinstance(arg.annotation, ast.Name):
                param_type = arg.annotation.id
                if param_type in known_base_names:
                    annotated_base_params.add(arg.arg)

        if not annotated_base_params:
            continue  # Нет параметров с базовым типом — нечего проверять

        # --- Шаг 2: Ищем isinstance(param, ...) в теле метода ---
        for node in ast.walk(func):
            if not isinstance(node, ast.Call):
                continue
            # Проверяем, что это вызов isinstance()
            if not (
                isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                continue
            # isinstance принимает минимум 2 аргумента: isinstance(obj, Type)
            if len(node.args) < 2:
                continue

            # Первый аргумент isinstance — проверяемый объект
            # Нас интересует случай, когда это имя параметра с базовым типом
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Name):
                continue
            if first_arg.id not in annotated_base_params:
                continue

            # Нашли isinstance(base_param, ...) — это нарушение
            # Определяем имя параметра и его аннотированный тип для сообщения
            param_name = first_arg.id
            # Ищем аннотацию этого параметра обратно
            param_annotation = next(
                (
                    arg.annotation.id
                    for arg in func.args.args
                    if arg.arg == param_name
                    and arg.annotation is not None
                    and isinstance(arg.annotation, ast.Name)
                ),
                "base type",
            )

            findings.append(_make_finding(
                rule="LSP-H-003",
                class_info=class_info,
                message=(
                    f"Method '{func.name}' accepts '{param_name}: {param_annotation}' "
                    f"but uses isinstance() to check its concrete type"
                ),
                principle="LSP",
                explanation=(
                    f"'{func.name}' declares parameter '{param_name}' as '{param_annotation}' "
                    f"(a base type from this project), implying it should work with any subtype. "
                    f"However, it uses isinstance() to branch on concrete subtypes, "
                    f"meaning subtypes are not truly substitutable here."
                ),
                suggestion=(
                    f"Move the type-specific logic into the '{param_annotation}' subtype itself "
                    f"(polymorphic dispatch), so '{func.name}' can call a unified interface "
                    f"without knowing the concrete type."
                ),
                method_name=func.name,  # NEW
            ))
            break  # Одного finding на метод достаточно

    return findings


# ---------------------------------------------------------------------------
# OCP-H-004: высокая цикломатическая сложность + isinstance
# ---------------------------------------------------------------------------

# Минимальная CC, при которой метод считается «сложным» для этой эвристики.
# Значение 5 — стандартный «предупредительный» порог по шкале Маккейба:
#   1-4: просто, легко тестировать
#   5-7: умеренно сложно (здесь мы начинаем смотреть)
#   8+:  высокий риск, сложно поддерживать
# Вынесено в константу — легко изменить при конфигурации.
_OCP_H004_CC_THRESHOLD = 5


def _check_ocp_h_004(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет методы с высокой цикломатической сложностью (CC >= порога) И
    хотя бы одним вызовом isinstance() внутри.

    Это «двойной сигнал» OCP-нарушения:
    - высокая CC указывает на разветвленную логику,
    - isinstance() указывает, что ветвление зависит от конкретных типов.

    В отличие от OCP-H-001 (цепочка if/elif с isinstance), здесь
    isinstance() может быть единственным — но сам метод уже достаточно
    сложен, чтобы тип-зависимое ветвление стало запахом.

    Пример нарушения:
        def process(self, item):
            if item.status == "new":
                ...
            elif item.priority > 5:
                ...
            elif item.retry_count < 3:
                ...
            if isinstance(item, SpecialItem):   # ← type-dispatch внутри сложного метода
                self._handle_special(item)

    Порог CC: _OCP_H004_CC_THRESHOLD (по умолчанию 5).
    """
    findings: List[Finding] = []

    for func in class_node.body:
        # Проверяем только обычные и async-методы, не вложенные классы
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # --- Шаг 1: Считаем цикломатическую сложность метода ---
        cc = _compute_method_cc(func)

        # Если CC ниже порога — метод недостаточно сложен, пропускаем
        if cc < _OCP_H004_CC_THRESHOLD:
            continue

        # --- Шаг 2: Проверяем наличие isinstance() в теле метода ---
        # Ищем хотя бы один вызов isinstance() в любом месте тела.
        # Используем _iter_method_nodes, чтобы игнорировать вложенные функции.
        has_isinstance = False
        for node in _iter_method_nodes(func):  # <--- ИСПРАВЛЕНО
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "isinstance"
            ):
                has_isinstance = True
                break

        if not has_isinstance:
            continue

        # --- Оба условия выполнены: высокая CC + isinstance ---
        findings.append(_make_finding(
            rule="OCP-H-004",
            class_info=class_info,
            message=(
                f"Method '{func.name}' has cyclomatic complexity {cc} "
                f"and uses isinstance() — mixed type-dispatch in complex method"
            ),
            principle="OCP",
            explanation=(
                f"'{func.name}' has a cyclomatic complexity of {cc} "
                f"(threshold: {_OCP_H004_CC_THRESHOLD}) and contains isinstance() "
                f"checks. High complexity combined with type-based branching "
                f"suggests this method handles multiple responsibilities, "
                f"each specific to a concrete type — a potential OCP violation."
            ),
            suggestion=(
                f"Consider extracting type-specific behavior into subclasses "
                f"or strategy objects. The '{func.name}' method should ideally "
                f"work with an abstraction, not branch on concrete types."
            ),
            method_name=func.name,  # NEW
        ))
        # Одного finding на метод достаточно

    return findings

# ---------------------------------------------------------------------------
# Вычисление приоритета и типа кандидата
# ---------------------------------------------------------------------------

def _compute_priority(
    reasons: List[str],
    inheritance_depth: int,
    interface_count: int,
) -> int:
    """
    Формула из архитектурного плана:
    priority = (кол-во heuristic_reasons) * 2 + (глубина наследования) + (кол-во реализаций интерфейса)
    Чем выше приоритет — тем раньше кандидат обрабатывается LLM при ограниченном бюджете.
    """
    return (len(reasons) * 2) + inheritance_depth + interface_count


def _determine_candidate_type(
    has_ocp_reasons: bool,
    has_lsp_reasons: bool,
    has_hierarchy: bool,
) -> CandidateType:
    """
    Определяет тип кандидата по логике из ТЗ:
    - есть наследование/интерфейсы + только LSP-поводы → 'lsp'
    - есть только OCP-поводы → 'ocp'
    - есть и те и другие, или иерархия без конкретных хитов → 'both'
    """
    if has_ocp_reasons and has_lsp_reasons:
        return "both"
    if has_lsp_reasons:
        return "lsp"
    if has_ocp_reasons:
        return "ocp"
    # Класс в иерархии, но без конкретных хитов — отправляем LLM смотреть обе стороны
    return "both" if has_hierarchy else "ocp"

# ---------------------------------------------------------------------------
# Главная публичная функция
# ---------------------------------------------------------------------------

def identify_candidates(
    project_map: ProjectMap,
    exclude_patterns: list[str] | None = None,
) -> HeuristicResult:
    """
    Прогоняет все реализованные эвристики по всем классам в ProjectMap.

    Параметры:
      project_map:
        Полная карта проекта (классы, иерархия, интерфейсы), собранная
        на предыдущем шаге пайплайна.
      exclude_patterns:
        Список подстрок для исключения путей файлов из эвристического анализа.
        Если None, используется дефолтный набор _DEFAULT_EXCLUDE_PATTERNS
        (tests/, migrations/, venv, node_modules и т.п.).

    Возвращает HeuristicResult:
      - findings: список Finding с source='heuristic', идут напрямую в отчет
      - candidates: список LlmCandidate, отсортированный по приоритету (убывание)

    Функция остается чистой: не обращается к файловой системе и не имеет
    побочных эффектов. Вся информация берется из ProjectMap и параметров.
    """
    # Собираем все эвристические находки по проекту
    all_findings: List[Finding] = []
    # Собираем кандидатов для LLM, будем сортировать и дедуплицировать в конце
    candidates: List[LlmCandidate] = []

    for class_name, class_info in project_map.classes.items():
        # --- Грубая фильтрация нерелевантных путей (Решение 6) ---
        # На этом шаге отсекаем тесты, миграции, venv, node_modules и т.д.
        # Важно делать это ДО AST-парсинга и ДО запуска эвристик, чтобы
        # не тратить время на заведомо нецелевые файлы и не генерировать шум.
        if _should_exclude_path(class_info.file_path, exclude_patterns):
            continue

        # Классы с динамическими базами пропускаем — эвристики на них ненадежны
        if "" in class_info.parent_classes:
            continue

        # Парсим AST из source_code, который был сохранен при Шаге 0 (buildProjectMap)
        class_node = _parse_class_ast(class_info.source_code, class_name)
        if class_node is None:
            # Если по какой-то причине парсинг не удался, просто пропускаем класс
            continue

        # --- Прогон эвристик для одного класса ---
        class_findings: List[Finding] = []

        # LSP-эвритстики:
        # LSP-H-001 и LSP-H-002 теперь используют project_map, чтобы
        # отличать абстрактные классы от конкретных (Решение 2).
        class_findings.extend(_check_lsp_h_001(class_node, class_info, project_map))
        class_findings.extend(_check_lsp_h_002(class_node, class_info, project_map))
        class_findings.extend(_check_lsp_h_004(class_node, class_info))

        # OCP-эвристики без доступа к ProjectMap
        class_findings.extend(_check_ocp_h_001(class_node, class_info))
        class_findings.extend(_check_ocp_h_002(class_node, class_info))

        # OCP-H-004: высокая CC + isinstance (двойной сигнал)
        # Регистрируем последней среди OCP-эвристик: она «шире» остальных —
        # срабатывает на методы, которые OCP-H-001/002 могли пропустить,
        # потому что isinstance там не в цепочке, а в смешанном сложном методе.
        class_findings.extend(_check_ocp_h_004(class_node, class_info))

        # Эвристики, которым нужен ProjectMap для проверки типов (LSP-H-003)
        class_findings.extend(_check_lsp_h_003(class_node, class_info, project_map))

        # Копим все findings по проекту для последующей дедупликации (Решение 3)
        all_findings.extend(class_findings)

        # --- Определение: является ли класс кандидатом для LLM ---
        has_hierarchy = (
            len(class_info.parent_classes) > 0
            or len(class_info.implemented_interfaces) > 0
        )

        # Кандидат — это класс с хотя бы одним эвристическим попаданием
        # ИЛИ любой класс, участвующий в иерархии (чтобы LLM посмотрел на контекст).
        is_candidate = bool(class_findings) or has_hierarchy
        if not is_candidate:
            continue

        # --- Формирование LlmCandidate ---
        reasons = [f.rule for f in class_findings]

        has_ocp = any("OCP" in r for r in reasons)
        has_lsp = any("LSP" in r for r in reasons)
        candidate_type = _determine_candidate_type(has_ocp, has_lsp, has_hierarchy)

        # Глубина наследования и количество интерфейсов влияют на приоритет
        depth = len([p for p in class_info.parent_classes if p != ""])
        interface_count = len(class_info.implemented_interfaces)
        priority = _compute_priority(reasons, depth, interface_count)

        candidates.append(
            LlmCandidate(
                class_name=class_name,
                file_path=class_info.file_path,
                source_code=class_info.source_code,
                candidate_type=candidate_type,
                heuristic_reasons=reasons,
                priority=priority,
            )
        )

    # NEW: дедупликация кандидатов по (file_path, class_name) (Решение 3)
    # Объединяем heuristic_reasons и оставляем кандидат с максимальным приоритетом.
    candidates = _deduplicate_candidates(candidates)

    # Сортируем кандидатов: наибольший приоритет — первым
    candidates.sort(key=lambda c: c.priority, reverse=True)

    # NEW: дедупликация findings по (file, class, method) (Решение 3)
    # При конфликте OCP-H-001 vs OCP-H-004 и LSP-H-001 vs LSP-H-002
    # оставляем более специфичное правило и дописываем explanation.
    deduped_findings = _deduplicate_findings(all_findings)

    return HeuristicResult(
        findings=deduped_findings,
        candidates=candidates,
    )