# Скрипт для формирования файла-маски проекта (скелет структуры без логики реализации)
# Запуск из корня репозитория solid_verifier (PowerShell):
#   python solid_dashboard/report/project_mask/export_skeleton.py

import ast
from pathlib import Path

# Директории, которые нужно полностью пропустить при обходе
SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", ".solid-cache", ".grimp_cache",
    ".tmp_heuristics_tests", "alembic", "build", "dist",
}

# Имя самого скрипта — исключаем из маски, чтобы не попасть в рекурсию
_SELF = Path(__file__).name


class SkeletonTransformer(ast.NodeTransformer):
    """AST-трансформер, который удаляет тела функций и методов, оставляя только сигнатуры."""

    def _clear_body(self, node: ast.AST) -> ast.AST:
        doc = ast.get_docstring(node)  # type: ignore[arg-type]
        # Если есть docstring — оставляем его, иначе ставим pass
        if doc:
            node.body = [ast.Expr(value=ast.Constant(value=doc))]  # type: ignore[attr-defined]
        else:
            node.body = [ast.Pass()]  # type: ignore[attr-defined]
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        self.generic_visit(node)
        return self._clear_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        self.generic_visit(node)
        return self._clear_body(node)


def generate_project_mask(root_dir: Path, output_file: Path) -> None:
    """Обходит root_dir рекурсивно, парсит каждый .py-файл через AST
    и записывает в output_file скелет (только сигнатуры)."""

    processed, skipped = 0, 0

    with open(output_file, "w", encoding="utf-8") as out:
        for py_file in sorted(root_dir.rglob("*.py")):
            # Пропускаем файлы внутри служебных директорий
            if any(part in SKIP_DIRS for part in py_file.parts):
                continue
            # Пропускаем сам скрипт
            if py_file.name == _SELF:
                continue
            # Пропускаем уже сформированный выходной файл (на случай .py-расширения)
            if py_file == output_file:
                continue

            try:
                code = py_file.read_text(encoding="utf-8")
                tree = ast.parse(code)
                SkeletonTransformer().visit(tree)
                skeleton_code = ast.unparse(tree)

                out.write(f"\n{'=' * 60}\n")
                out.write(f"FILE: {py_file.relative_to(root_dir)}\n")
                out.write(f"{'=' * 60}\n")
                out.write(skeleton_code)
                out.write("\n")
                processed += 1

            except Exception as exc:
                out.write(f"\n# Parsing error — {py_file.relative_to(root_dir)}: {exc}\n")
                skipped += 1

    print(f"Done. Processed: {processed} files, skipped: {skipped}.")
    print(f"Mask saved to: {output_file}")


if __name__ == "__main__":
    # Скрипт лежит в solid_dashboard/report/project_mask/ — поднимаемся до корня репозитория
    # project_mask -> report -> solid_dashboard -> <repo_root>
    repo_root = Path(__file__).resolve().parents[3]
    script_dir = Path(__file__).resolve().parent

    generate_project_mask(
        root_dir=repo_root,
        output_file=script_dir / "solid_project_mask.txt",
    )
