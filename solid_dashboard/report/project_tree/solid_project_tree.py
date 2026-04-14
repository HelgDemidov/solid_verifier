# Скрипт для формирования текстовой схемы-дерева директорий проекта
# Запуск из корня репозитория solid_verifier (PowerShell):
#   python solid_dashboard/report/project_tree/solid_project_tree.py

from pathlib import Path

# Директории, которые не нужно показывать в дереве
IGNORE_DIRS = {
    ".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".idea", ".vscode", ".solid-cache", ".grimp_cache",
    ".tmp_heuristics_tests", "build", "dist",
}

# Файлы, которые не нужно показывать в дереве
IGNORE_FILES = {".DS_Store"}


def print_tree(root: Path, file_obj, prefix: str = "") -> None:
    """Рекурсивно обходит директорию root и записывает дерево в file_obj."""
    # Собираем записи: пропускаем скрытые/служебные директории и игнорируемые файлы
    entries = [
        p for p in root.iterdir()
        if p.name not in IGNORE_FILES
        and not (p.is_dir() and p.name in IGNORE_DIRS)
    ]
    # Сначала директории, потом файлы; внутри каждой группы — по имени
    entries.sort(key=lambda p: (p.is_file(), p.name.lower()))

    for index, path in enumerate(entries):
        is_last = index == len(entries) - 1
        connector = "└── " if is_last else "├── "
        # Пишем сразу в файл, чтобы обойти проблему кодировки консоли Windows
        file_obj.write(f"{prefix}{connector}{path.name}\n")

        if path.is_dir():
            extension = "    " if is_last else "│   "
            print_tree(path, file_obj, prefix + extension)


if __name__ == "__main__":
    # Скрипт лежит в solid_dashboard/report/project_tree/ — поднимаемся до корня репозитория
    # project_tree -> report -> solid_dashboard -> <repo_root>
    repo_root = Path(__file__).resolve().parents[3]
    script_dir = Path(__file__).resolve().parent

    output_file = script_dir / "solid_project_tree.txt"

    # Открываем файл с явной кодировкой utf-8, чтобы корректно записать символы дерева
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"{repo_root.name}/\n")
        print_tree(repo_root, f)

    print(f"Project tree for '{repo_root.name}' saved to: {output_file}")
