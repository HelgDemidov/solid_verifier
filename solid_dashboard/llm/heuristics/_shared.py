# Общие утилиты для эвристик LSP/OCP.
#
# Содержит только то, что используется в ДВУХ и более файлах пакета.
# Конфигурация конкретной эвристики остается в её собственном файле.
#
# Экспортируемые имена:
#   _DEFAULT_EXCLUDE_PATTERNS   — _runner.py
#   _normalize_path_for_matching — _runner.py
#   _should_exclude_path        — _runner.py
#   _parse_class_ast            — _runner.py
#   _make_finding               — все 7 файлов эвристик
#   _is_abstract_class          — lsp_h_001.py, lsp_h_002.py
#   _has_isinstance_call        — ocp_h_001.py, ocp_h_004.py
#   _count_elif_chain           — ocp_h_001.py
#   _iter_method_nodes          — ocp_h_004.py, _compute_method_cc (здесь же)
#   _compute_method_cc          — ocp_h_004.py

import ast
import logging
from typing import Generator

from ..types import (
    ClassInfo,
    Finding,
    FindingDetails,
    ProjectMap,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Дефолтные паттерны исключения нерелевантных путей
# ---------------------------------------------------------------------------

_DEFAULT_EXCLUDE_PATTERNS: list[str] = [
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
# Константы для вычисления цикломатической сложности (нужны только внутри модуля)
# ---------------------------------------------------------------------------

# AST-ноды, каждая из которых добавляет +1 к CC
_CC_NODE_TYPES = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.Assert,
    ast.comprehension,
)

# Булевы операторы тоже создают дополнительные пути исполнения
_CC_BOOL_OPS = (ast.And, ast.Or)

# ---------------------------------------------------------------------------
# Фильтрация путей
# ---------------------------------------------------------------------------

def _normalize_path_for_matching(path: str) -> str:
    # Приводим к единому виду для substring-matching на Windows/Linux/macOS:
    # слэши унифицируем, регистр понижаем
    return path.replace("\\", "/").lower()


def _should_exclude_path(
    file_path: str,
    exclude_patterns: list[str] | None,
) -> bool:
    # Если передан None, используем дефолтный набор паттернов
    patterns = _DEFAULT_EXCLUDE_PATTERNS if exclude_patterns is None else exclude_patterns
    normalized_path = _normalize_path_for_matching(file_path)
    for pattern in patterns:
        normalized_pattern = _normalize_path_for_matching(pattern)
        if normalized_pattern and normalized_pattern in normalized_path:
            return True
    return False

# ---------------------------------------------------------------------------
# Парсинг AST нужного класса из исходного кода
# ---------------------------------------------------------------------------

def _parse_class_ast(source_code: str, class_name: str) -> ast.ClassDef | None:
    # Парсит source_code и возвращает ClassDef нужного класса.
    # Возвращает None при пустом коде, SyntaxError или отсутствии класса
    if not source_code.strip():
        return None
    try:
        tree = ast.parse(source_code)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node
    except SyntaxError:
        logger.warning("SyntaxError при парсинге исходника класса %s", class_name)
    return None

# ---------------------------------------------------------------------------
# Фабрика Finding — единственная точка создания Finding в пакете heuristics
# ---------------------------------------------------------------------------

def _make_finding(
    rule: str,
    class_info: ClassInfo,
    message: str,
    principle: str,
    explanation: str,
    suggestion: str,
    method_name: str | None = None,
) -> Finding:
    # Все поля Finding фиксированы здесь, чтобы эвристики не дублировали
    # логику заполнения source, line, severity
    return Finding(
        rule=rule,
        file=class_info.file_path,
        class_name=class_info.name,
        line=None,
        severity="warning",
        message=message,
        source="heuristic",
        details=FindingDetails(
            principle=principle,
            explanation=explanation,
            suggestion=suggestion,
            method_name=method_name,
        ),
    )

# ---------------------------------------------------------------------------
# Определение абстрактного класса — нужна LSP-H-001 и LSP-H-002
# ---------------------------------------------------------------------------

def _is_abstract_class(class_info: ClassInfo, project_map: ProjectMap) -> bool:
    # Класс считается абстрактным при выполнении хотя бы одного условия:
    # 1) явно наследует ABC, 2) зарегистрирован как интерфейс в ProjectMap,
    # 3) содержит хотя бы один абстрактный метод
    if "ABC" in class_info.parent_classes:
        return True
    if class_info.name in project_map.interfaces:
        return True
    if any(m.is_abstract for m in class_info.methods):
        return True
    return False

# ---------------------------------------------------------------------------
# Проверка наличия isinstance() в выражении — нужна OCP-H-001 и OCP-H-004
# ---------------------------------------------------------------------------

def _has_isinstance_call(node: ast.expr) -> bool:
    # Проверяет, содержит ли выражение вызов isinstance() на любой глубине
    for subnode in ast.walk(node):
        if (
            isinstance(subnode, ast.Call)
            and isinstance(subnode.func, ast.Name)
            and subnode.func.id == "isinstance"
        ):
            return True
    return False

# ---------------------------------------------------------------------------
# Подсчёт длины if/elif цепочки — нужна OCP-H-001 для explanation
# ---------------------------------------------------------------------------

def _count_elif_chain(if_node: ast.If) -> int:
    # Считает общее число ветвей в цепочке if/elif (включая первый if).
    # Используется только для формирования текста explanation в OCP-H-001,
    # не для самого триггера
    count = 1
    current = if_node
    while (
        current.orelse
        and len(current.orelse) == 1
        and isinstance(current.orelse[0], ast.If)
    ):
        count += 1
        current = current.orelse[0]
    return count

# ---------------------------------------------------------------------------
# Обход тела метода без захода во вложенные функции/классы
# ---------------------------------------------------------------------------

def _iter_method_nodes(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
) -> Generator[ast.AST, None, None]:
    # ast.walk() обходит всё дерево целиком — в т.ч. вложенные функции.
    # Это ошибочно увеличивает CC и создаёт ложные isinstance-хиты.
    # Генератор обходит только узлы тела метода, пропуская поддеревья
    # FunctionDef, AsyncFunctionDef и ClassDef как независимые единицы
    stack = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
            ):
                stack.append(child)

# ---------------------------------------------------------------------------
# Вычисление цикломатической сложности метода — нужна OCP-H-004
# ---------------------------------------------------------------------------

def _compute_method_cc(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    # CC по Маккейбу: CC = 1 + количество узлов ветвления.
    # Использует _iter_method_nodes, чтобы не учитывать вложенные функции
    cc = 1
    for node in _iter_method_nodes(func):
        if isinstance(node, _CC_NODE_TYPES):
            cc += 1
        # BoolOp: каждый оператор в цепочке (a and b and c) даёт +len-1
        if isinstance(node, ast.BoolOp) and isinstance(node.op, _CC_BOOL_OPS):
            cc += len(node.values) - 1
        # Тернарный оператор: x if cond else y — ещё один путь
        if isinstance(node, ast.IfExp):
            cc += 1
    return cc
