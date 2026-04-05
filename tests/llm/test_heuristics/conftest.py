# ---------------------------------------------------------------------------
# Общие фабрики для unit-тестов эвристик
# Доступны всем модулям пакета test_heuristics/ без явного импорта
# ---------------------------------------------------------------------------

import textwrap
import ast
from pathlib import Path

from solid_dashboard.llm.ast_parser import build_project_map
from solid_dashboard.llm.types import (
    ClassInfo,
    MethodSignature,
    ProjectMap,
)


def _pm_from_source(
    source: str,
    class_name: str,
    parent_classes: list[str] | None = None,
    override_methods: list[str] | None = None,
) -> ProjectMap:
    """
    Строит ProjectMap с одним ClassInfo по исходнику.

    parent_classes: список имен базовых классов для ClassInfo.parent_classes.
    override_methods: список методов, помечаемых как is_override=True.
    """
    # Нормализуем отступы, записываем во временный файл
    source_dedented = textwrap.dedent(source)

    tmp_dir = Path.cwd() / ".tmp_heuristics_tests"
    tmp_dir.mkdir(exist_ok=True)
    tmp_file = tmp_dir / "tmp_module.py"
    tmp_file.write_text(source_dedented, encoding="utf-8")

    # Строим ProjectMap по пути файла
    project_map = build_project_map([str(tmp_file)])

    class_info = project_map.classes[class_name]

    # Переопределяем parent_classes, если явно переданы
    if parent_classes is not None:
        class_info = ClassInfo(
            name=class_info.name,
            file_path=class_info.file_path,
            source_code=class_info.source_code,
            parent_classes=parent_classes,
            implemented_interfaces=class_info.implemented_interfaces,
            methods=class_info.methods,
            dependencies=class_info.dependencies,
        )
        project_map.classes[class_name] = class_info

    # Отмечаем is_override для заданных методов
    if override_methods:
        new_methods: list[MethodSignature] = []
        for m in class_info.methods:
            if m.name in override_methods:
                new_methods.append(
                    MethodSignature(
                        name=m.name,
                        parameters=m.parameters,
                        return_type=m.return_type,
                        is_override=True,
                        is_abstract=m.is_abstract,
                    )
                )
            else:
                new_methods.append(m)
        class_info = ClassInfo(
            name=class_info.name,
            file_path=class_info.file_path,
            source_code=class_info.source_code,
            parent_classes=class_info.parent_classes,
            implemented_interfaces=class_info.implemented_interfaces,
            methods=new_methods,
            dependencies=class_info.dependencies,
        )
        project_map.classes[class_name] = class_info

    return project_map


def _class_info_node_and_project_map_from_source(
    source: str,
    class_name: str,
    parent_classes: list[str] | None = None,
    override_methods: list[str] | None = None,
) -> tuple[ClassInfo, ast.ClassDef, ProjectMap]:
    """
    Возвращает ClassInfo, AST-ноду класса и ProjectMap.

    Используется для прямого тестирования lsp_h_001.check() / lsp_h_002.check()
    без запуска оркестратора identify_candidates().
    """
    pm = _pm_from_source(
        source=source,
        class_name=class_name,
        parent_classes=parent_classes,
        override_methods=override_methods,
    )

    class_info = pm.classes[class_name]

    # Получаем ast.ClassDef из source_code класса
    tree = ast.parse(textwrap.dedent(class_info.source_code))
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)

    return class_info, class_node, pm
