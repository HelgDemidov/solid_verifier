"""
Шаг 1b пайплайна LLM-анализа: эвристический анализ ProjectMap.

Реализует identify_candidates() — чистую функцию от ProjectMap к HeuristicResult.
Не обращается к файловой системе: весь необходимый код уже находится в ProjectMap.

Реализованные эвристики (первые 4 из 8 по порядку ТЗ):
  LSP-H-001 — raise NotImplementedError в переопределённом методе
  LSP-H-002 — пустое тело переопределённого метода (pass или docstring)
  OCP-H-001 — цепочки if/elif с isinstance() >= 3 ветвей
  LSP-H-004 — __init__ без вызова super().__init__()

Оставшиеся эвристики (следующая итерация):
  OCP-H-002 — match/case на типах
  LSP-H-003 — isinstance в коде с параметром базового типа
  OCP-H-003 — словарь-диспетчер типов
  OCP-H-004 — высокая цикломатическая сложность + isinstance
"""

import ast
import logging
from typing import List, cast

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
# Вспомогательные функции
# ---------------------------------------------------------------------------

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

def _compute_method_cc(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """
    Считает цикломатическую сложность (CC) отдельного метода по формуле Маккейба.

    CC = 1 + количество узлов ветвления в теле метода.
    Базовое значение 1 означает «один сквозной путь» при отсутствии ветвлений.

    Учитываем: if/elif, for, while, except, with, assert, comprehension,
    а также булевые операторы and/or в условиях (каждый добавляет +1).

    Не учитываем вложенные функции/классы внутри метода —
    они считаются самостоятельными единицами (как в radon).

    Аргументы:
        func: AST-нода метода (FunctionDef или AsyncFunctionDef).

    Возвращает:
        int: цикломатическая сложность >= 1.
    """
    # Базовое значение: один путь «насквозь» при отсутствии ветвлений
    cc = 1

    for node in ast.walk(func):
        # Пропускаем вложенные функции и классы — у них своя CC
        # (ast.walk заходит внутрь них, нам это не нужно для текущего метода).
        # Технически: пропускаем саму ноду func.body мы не можем через ast.walk,
        # но вложенные FunctionDef/AsyncFunctionDef/ClassDef — пропускаем.
        # Проверяем: если нода — это вложенная функция (не сам анализируемый метод)
        if node is not func and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            # Не заходим внутрь — ast.walk уже включил их в обход,
            # но мы просто не считаем их ноды
            continue

        # Узлы ветвления: каждый добавляет +1 к CC
        if isinstance(node, _CC_NODE_TYPES):
            cc += 1

        # BoolOp: ast.BoolOp содержит список операндов через and/or.
        # Каждый оператор and/or в цепочке — это дополнительный путь.
        # Пример: `a and b and c` — это BoolOp(op=And, values=[a, b, c]).
        # Добавляем (len(values) - 1), т.к. n операндов = n-1 операторов.
        if isinstance(node, ast.BoolOp) and isinstance(node.op, _CC_BOOL_OPS):
            cc += len(node.values) - 1

        # Тернарный оператор (IfExp): `x if cond else y` — это тоже ветвление
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
        ),
    )


# ---------------------------------------------------------------------------
# LSP-H-001: raise NotImplementedError в переопределённом методе
# ---------------------------------------------------------------------------

def _check_lsp_h_001(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет переопределённые методы, которые только бросают NotImplementedError.
    Это нарушение LSP: вызывающий код не может полагаться на замену базового
    класса подклассом, если последний не реализует контракт.
    """
    # Множество имён методов, помеченных is_override при Шаге 2
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

        # Обходим всё тело метода в поисках raise NotImplementedError
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
                ))
                break

    return findings


# ---------------------------------------------------------------------------
# LSP-H-002: пустое тело переопределённого метода
# ---------------------------------------------------------------------------

def _check_lsp_h_002(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет переопределённые методы с пустым телом (только pass или docstring).
    Пустое тело переопределения — признак того, что подкласс не выполняет
    контракт родителя, нарушая принцип подстановки.
    """
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
            ))

    return findings


# ---------------------------------------------------------------------------
# OCP-H-001: цепочки if/elif с isinstance() >= 3 ветвей ИСПРАВЛЕННАЯ ВЕРСИЯ
# ---------------------------------------------------------------------------

def _check_ocp_h_001(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет методы с цепочками if/elif, где условие содержит isinstance().
    Порог: >= 3 ветвей (if + 2 elif).

    Исправление: ast.walk обходит ВСЕ if-ноды в теле метода, включая
    внутренние elif, которые в AST тоже представлены как ast.If.
    Чтобы считать длину цепочки только от КОРНЕВОГО if (не от elif внутри),
    проверяем, что текущая нода не является orelse-дочерней предыдущего if.
    Реализуем это сбором "вторичных" нод: собираем все if-ноды, которые
    являются частью orelse — и пропускаем их при обходе.
    """
    findings: List[Finding] = []

    for func in class_node.body:
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Собираем множество нод, которые являются частью elif-цепочки
        # (т.е. дочерними ast.If внутри orelse другого ast.If).
        # Эти ноды не являются "корневыми" if — мы их пропускаем.
        elif_nodes: set[int] = set()
        for node in ast.walk(func):
            if isinstance(node, ast.If):
                # Если orelse содержит ровно один If — это elif
                if (
                    node.orelse
                    and len(node.orelse) == 1
                    and isinstance(node.orelse[0], ast.If)
                ):
                    # id() объекта AST-ноды — стабильный идентификатор в рамках одного дерева
                    elif_nodes.add(id(node.orelse[0]))

        for node in ast.walk(func):
            if not isinstance(node, ast.If):
                continue

            # Пропускаем ноды, которые являются частью elif (не корневой if)
            if id(node) in elif_nodes:
                continue

            # Проверяем, что хотя бы первая ветвь содержит isinstance()
            if not _has_isinstance_call(node.test):
                continue

            chain_length = _count_elif_chain(node)

            if chain_length >= 3:
                findings.append(_make_finding(
                    rule="OCP-H-001",
                    class_info=class_info,
                    message=(
                        f"Method '{func.name}' contains an isinstance() chain "
                        f"with {chain_length} branches — potential OCP violation"
                    ),
                    principle="OCP",
                    explanation=(
                        f"'{func.name}' uses a {chain_length}-branch if/elif chain "
                        f"with isinstance() checks. Every new type requires modifying "
                        f"this method directly, violating Open-Closed Principle."
                    ),
                    suggestion=(
                        "Consider replacing the isinstance chain with polymorphism: "
                        "extract each branch into a subclass or strategy, and let "
                        "Python's dispatch mechanism handle the routing."
                    ),
                ))
                # Одного finding на метод достаточно
                break

    return findings


# ---------------------------------------------------------------------------
# LSP-H-004: __init__ без вызова super().__init__()
# ---------------------------------------------------------------------------

def _check_lsp_h_004(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
) -> List[Finding]:
    """
    Ищет __init__ в подклассах без вызова super().__init__().
    Отсутствие super().__init__() может нарушить инварианты родителя:
    поля, которые родитель устанавливает при инициализации, не будут
    инициализированы — нарушение LSP на уровне состояния объекта.

    Проверяем только классы с реальными родителями (не <dynamic>).
    """
    # Только для реальных подклассов (не standalone и не с динамическими базами)
    real_parents = [p for p in class_info.parent_classes if p != "<dynamic>"]
    if not real_parents:
        return []

    findings: List[Finding] = []

    for func in class_node.body:
        # Ищем __init__, только синхронный (async __init__ не существует в Python)
        if not isinstance(func, ast.FunctionDef) or func.name != "__init__":
            continue

        has_super_init = False

        # Обходим всё тело __init__ в поисках вызова super().__init__() или super().__init__
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
            # Ниже мы всё равно обращаемся к .cases, так что уточним тип аккуратнее:
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
            ))
            break  # Одного finding на метод достаточно

    return findings


# ---------------------------------------------------------------------------
# OCP-H-003: словарь-диспетчер типов
# ---------------------------------------------------------------------------

def _check_ocp_h_003(
    class_node: ast.ClassDef,
    class_info: ClassInfo,
    project_map: ProjectMap,
) -> List[Finding]:
    """
    Ищет словари, где ключи — это имена классов из ProjectMap (строки или сами типы),
    а значения — callable (функции/методы/лямбды).

    Пример нарушения OCP:
        HANDLERS = {
            "circle": draw_circle,
            "square": draw_square,
            "triangle": draw_triangle,
        }

    или в виде атрибута класса:
        self._handlers = {
            CircleEvent: self._handle_circle,
            SquareEvent: self._handle_square,
        }

    Словарь-диспетчер — это структурно та же проблема, что и isinstance-цепочка:
    добавление нового типа требует модификации существующего кода.

    Алгоритм (консервативный):
    1. Ищем ast.Dict в методах (включая __init__) и в теле класса.
    2. Проверяем ключи: если >= 3 ключей являются именами классов из ProjectMap
       (ast.Name с id в known_base_names) — это подозрительный диспетчер.
    3. Проверяем значения: хотя бы одно должно выглядеть как callable
       (ast.Name, ast.Attribute, ast.Lambda — не строки, не числа).

    Консервативный порог (>= 3 ключей-типов) снижает ложные срабатывания:
    словари из 1-2 типов — обычная практика, не запах.
    """
    # Собираем все известные имена классов и интерфейсов проекта
    known_names: set[str] = set(project_map.classes.keys()) | set(project_map.interfaces.keys())

    findings: List[Finding] = []

    # Проверяем как методы, так и тело класса (атрибуты уровня класса)
    # В тело класса попадают, например, CLASS_HANDLERS = {...}
    nodes_to_check: list[tuple[str, ast.AST]] = []

    for item in class_node.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Для методов — ищем dict внутри тела
            nodes_to_check.append((item.name, item))
        elif isinstance(item, ast.Assign):
            # Для атрибутов класса — ищем dict в правой части присваивания
            nodes_to_check.append(("<class_body>", item))

    for context_name, context_node in nodes_to_check:
        for node in ast.walk(context_node):
            if not isinstance(node, ast.Dict):
                continue

            # --- Считаем ключи, которые являются именами классов из проекта ---
            type_key_count = 0
            for key in node.keys:
                if key is None:
                    # None как ключ словаря — это **kwargs распаковка, пропускаем
                    continue
                # Ключ как имя типа: {Circle: ..., Square: ...}
                if isinstance(key, ast.Name) and key.id in known_names:
                    type_key_count += 1
                # Ключ как строка с именем типа: {"Circle": ..., "Square": ...}
                # Строковые ключи более спорны, но часто используются в реестрах
                elif isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value in known_names:
                        type_key_count += 1

            if type_key_count < 3:
                continue  # Недостаточно типовых ключей — не считаем нарушением

            # --- Проверяем, что значения похожи на callable, не на данные ---
            callable_value_count = sum(
                1 for v in node.values
                if v is not None and isinstance(v, (ast.Name, ast.Attribute, ast.Lambda))
            )

            # Нужно хотя бы 50% callable-значений, чтобы это выглядело как диспетчер
            if callable_value_count < max(1, type_key_count // 2):
                continue

            findings.append(_make_finding(
                rule="OCP-H-003",
                class_info=class_info,
                message=(
                    f"{'Method' if context_name != '<class_body>' else 'Class attribute'} "
                    f"'{context_name}' contains a type-dispatch dictionary "
                    f"with {type_key_count} type keys — potential OCP violation"
                ),
                principle="OCP",
                explanation=(
                    f"A dictionary with {type_key_count} keys that are class names from "
                    f"this project is used as a type dispatcher. This is structurally "
                    f"equivalent to an isinstance chain: adding a new type requires "
                    f"modifying this dictionary."
                ),
                suggestion=(
                    "Consider replacing the dispatch dictionary with a registration "
                    "mechanism: each type registers its own handler, so the dispatcher "
                    "never needs to be modified when new types are added."
                ),
            ))
            break  # Одного finding на контекст достаточно

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
    - высокая CC указывает на разветвлённую логику,
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
        # В отличие от OCP-H-001, нас не интересует структура цепочки —
        # достаточно факта присутствия type-check в сложном методе.
        has_isinstance = False
        for node in ast.walk(func):
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

def identify_candidates(project_map: ProjectMap) -> HeuristicResult:
    """
    Прогоняет все реализованные эвристики по всем классам в ProjectMap.

    Возвращает HeuristicResult:
    - findings: список Finding с source='heuristic', идут напрямую в отчёт
    - candidates: список LlmCandidate, отсортированный по приоритету (убывание)

    Является чистой функцией: не обращается к FS, не имеет побочных эффектов.
    """
    all_findings: List[Finding] = []
    candidates: List[LlmCandidate] = []

    for class_name, class_info in project_map.classes.items():

        # Классы с динамическими базами пропускаем — эвристики на них ненадёжны
        if "<dynamic>" in class_info.parent_classes:
            continue

        # Парсим AST из source_code, который был сохранён при Шаге 0 (buildProjectMap)
        class_node = _parse_class_ast(class_info.source_code, class_name)
        if class_node is None:
            continue

        # --- Прогон эвристик ---
        class_findings: List[Finding] = []

        # LSP-эвристики (требуют is_override-информации из Шага 2)
        class_findings.extend(_check_lsp_h_001(class_node, class_info))
        class_findings.extend(_check_lsp_h_002(class_node, class_info))
        class_findings.extend(_check_lsp_h_004(class_node, class_info))

        # OCP-эвристики без доступа к ProjectMap
        class_findings.extend(_check_ocp_h_001(class_node, class_info))
        class_findings.extend(_check_ocp_h_002(class_node, class_info))

        # OCP-H-004: высокая CC + isinstance (двойной сигнал)
        # Регистрируем последней среди OCP-эвристик: она «шире» остальных —
        # срабатывает на методы, которые OCP-H-001/002 могли пропустить,
        # потому что isinstance там не в цепочке, а в смешанном сложном методе.
        class_findings.extend(_check_ocp_h_004(class_node, class_info))

        # Эвристики, которым нужен ProjectMap для проверки типов
        class_findings.extend(_check_lsp_h_003(class_node, class_info, project_map))
        class_findings.extend(_check_ocp_h_003(class_node, class_info, project_map))

        # ИСПРАВЛЕНИЕ: добавляем findings текущего класса в общий список
        # Эта строка отсутствовала — из-за чего result.findings всегда был []
        all_findings.extend(class_findings)

        # --- Определение: является ли класс кандидатом для LLM ---
        has_hierarchy = (
            len(class_info.parent_classes) > 0
            or len(class_info.implemented_interfaces) > 0
        )

        # Кандидат — класс с хоть одним finding ИЛИ в иерархии
        is_candidate = bool(class_findings) or has_hierarchy

        if not is_candidate:
            continue

        # --- Формирование LlmCandidate ---
        reasons = [f.rule for f in class_findings]

        has_ocp = any("OCP" in r for r in reasons)
        has_lsp = any("LSP" in r for r in reasons)
        candidate_type = _determine_candidate_type(has_ocp, has_lsp, has_hierarchy)

        depth = len([p for p in class_info.parent_classes if p != ""])
        interface_count = len(class_info.implemented_interfaces)
        priority = _compute_priority(reasons, depth, interface_count)

        candidates.append(LlmCandidate(
            class_name=class_name,
            file_path=class_info.file_path,
            source_code=class_info.source_code,
            candidate_type=candidate_type,
            heuristic_reasons=reasons,
            priority=priority,
        ))

    # Сортируем кандидатов: наибольший приоритет — первым
    candidates.sort(key=lambda c: c.priority, reverse=True)

    return HeuristicResult(
        findings=all_findings,
        candidates=candidates,
    )
