"""
Построение ProjectMap — Шаг 0 пайплайна LLM-анализа.
Использует только стандартный ast.parse(), никаких внешних зависимостей.
"""

import ast
import logging
from pathlib import Path
from typing import List, Set, Union
from collections.abc import Sequence

from .types import ClassInfo, InterfaceInfo, MethodSignature, ProjectMap

logger = logging.getLogger(__name__)

# Имена, по которым определяем ABC/Protocol в базах класса
_INTERFACE_MARKERS = frozenset({"ABC", "Protocol"})


# ---------------------------------------------------------------------------
# Вспомогательные функции извлечения AST-данных
# ---------------------------------------------------------------------------

def _extract_class_source(source: str, node: ast.ClassDef) -> str:
    """Возвращает полный текст блока class, включая тело."""
    segment = ast.get_source_segment(source, node)
    return segment or ""


def _extract_bases(node: ast.ClassDef) -> List[str]:
    """
    Извлекает имена базовых классов.
    Динамические выражения (get_base()) → '<dynamic>'.
    """
    bases: List[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            bases.append(base.id)
        elif isinstance(base, ast.Attribute):
            # Для typing.Protocol, abc.ABC и т.п.
            bases.append(base.attr)
        else:
            # Динамическое выражение — помечаем, эвристики пропустят
            bases.append("<dynamic>")
    return bases


def _is_abstract_method(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """
    Определяет, помечен ли метод как абстрактный через @abstractmethod.

    Поддерживаем:
      - @abstractmethod
      - @abc.abstractmethod
      - @something.abstractmethod
    """
    for dec in func.decorator_list:
        # @abstractmethod
        if isinstance(dec, ast.Name) and dec.id == "abstractmethod":
            return True
        # @abc.abstractmethod или @foo.abstractmethod
        if isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod":
            return True
    return False


def _extract_method_signatures(
    node: ast.ClassDef,
    parent_method_names: Set[str],
) -> List[MethodSignature]:
    signatures: List[MethodSignature] = []

    for item in node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # --- Параметры ---
        params: List[str] = []
        for arg in item.args.args:
            arg_str = arg.arg
            if arg.annotation:
                ann = _annotation_to_str(arg.annotation)
                arg_str += f": {ann}"
            params.append(arg_str)

        if item.args.vararg:
            params.append(f"*{item.args.vararg.arg}")
        if item.args.kwarg:
            params.append(f"**{item.args.kwarg.arg}")

        # --- Тип возврата ---
        return_type = _annotation_to_str(item.returns) if item.returns else "Any"

        # ВАЖНО: флаг абстрактного метода
        is_abstract = _is_abstract_method(item)

        signatures.append(
            MethodSignature(
                name=item.name,
                parameters=", ".join(params),
                return_type=return_type,
                is_override=item.name in parent_method_names,
                is_abstract=is_abstract,  # ← этот аргумент должен быть
            )
        )

    return signatures


def _annotation_to_str(node: ast.expr | None) -> str:
    """Преобразует AST-ноду аннотации типа в строку (best-effort)."""
    if node is None:
        return "Any"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_annotation_to_str(node.value)}.{node.attr}"
    if isinstance(node, ast.Subscript):
        return f"{_annotation_to_str(node.value)}[{_annotation_to_str(node.slice)}]"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        # X | Y — union syntax Python 3.10+
        return f"{_annotation_to_str(node.left)} | {_annotation_to_str(node.right)}"
    if isinstance(node, ast.Constant):
        return repr(node.value)
    if isinstance(node, ast.Tuple):
        return ", ".join(_annotation_to_str(e) for e in node.elts)
    return "Any"


def _extract_top_level_imports(tree: ast.Module) -> List[str]:
    """
    Собирает имена модулей из top-level import/from-import.
    Используется для заполнения ClassInfo.dependencies.
    """
    deps: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                deps.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                deps.append(node.module.split(".")[0])
    return list(dict.fromkeys(deps))  # убираем дубликаты, сохраняя порядок


def _is_interface(bases: List[str]) -> bool:
    """Класс считается интерфейсом, если среди его баз есть ABC или Protocol."""
    return any(b in _INTERFACE_MARKERS for b in bases)


# ---------------------------------------------------------------------------
# Публичная функция
# ---------------------------------------------------------------------------

def build_project_map(files: Sequence[Union[str, Path]]) -> ProjectMap:
    """
    Строит ProjectMap по списку Python-файлов или директорий.
    Директории рекурсивно сканируются на *.py файлы.
    """
    project_map = ProjectMap()

    # Разворачиваем директории в список файлов
    resolved_files: List[Path] = []
    for entry in files:
        p = Path(entry)
        if p.is_dir():
            # Рекурсивно собираем все .py файлы из директории
            resolved_files.extend(sorted(p.rglob("*.py")))
        elif p.is_file():
            resolved_files.append(p)
        else:
            logger.warning("Path not found, skipping: %s", entry)

    # -----------------------------------------------------------------------
    # Проход 1: сбор всех классов и интерфейсов
    # -----------------------------------------------------------------------
    for path in resolved_files:
        if path.suffix != ".py":
            continue

        file_path_str = str(path)

        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file_path_str)
        except SyntaxError as exc:
            logger.warning("Syntax error in %s (%s), skipping.", file_path_str, exc.msg)
            continue
        except UnicodeDecodeError:
            logger.warning("Cannot decode %s, skipping.", file_path_str)
            continue

        file_deps = _extract_top_level_imports(tree)

        # Обрабатываем только top-level классы (вложенные игнорируем)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue

            bases = _extract_bases(node)
            methods = _extract_method_signatures(node, parent_method_names=set())

            class_info = ClassInfo(
                name=node.name,
                file_path=file_path_str,
                source_code=_extract_class_source(source, node),
                parent_classes=bases,
                implemented_interfaces=[],  # заполняется во 2-м проходе
                methods=methods,
                dependencies=file_deps,
            )
            project_map.classes[node.name] = class_info

            if _is_interface(bases):
                project_map.interfaces[node.name] = InterfaceInfo(
                    name=node.name,
                    file_path=file_path_str,
                    methods=methods,
                    implementations=[],
                )

    # -----------------------------------------------------------------------
    # Проход 2: обратные связи и is_override
    # -----------------------------------------------------------------------
    for class_name, class_info in project_map.classes.items():

        # 2a. Заполняем implemented_interfaces и список implementations
        for base in class_info.parent_classes:
            if base in project_map.interfaces:
                if base not in class_info.implemented_interfaces:
                    class_info.implemented_interfaces.append(base)
                if class_name not in project_map.interfaces[base].implementations:
                    project_map.interfaces[base].implementations.append(class_name)

        # 2b. Пересчитываем is_override для методов
        # Собираем полное множество имен методов всех родителей
        parent_method_names: Set[str] = set()
        for base in class_info.parent_classes:
            parent_info = project_map.classes.get(base)
            if parent_info:
                parent_method_names.update(m.name for m in parent_info.methods)

        if parent_method_names:
            for method in class_info.methods:
                method.is_override = method.name in parent_method_names

    return project_map