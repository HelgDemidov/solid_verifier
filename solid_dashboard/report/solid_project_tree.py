# Скрипт для формирования текстовой схемы-лерева директорий проекта
# Команда для запуска (из tools\solid_verifier): python solid_dashboard\report\solid_project_tree.py

from pathlib import Path

# директории и файлы, которые не нужно показывать в дереве
IGNORE_DIRS = {".git", ".venv", "__pycache__", ".idea", ".mypy_cache",
               ".pytest_cache", ".solid-cache", ".grimp_cache", ".tmp_heuristics_tests"}
IGNORE_FILES = {".DS_Store", ".win-amd64"}

def print_tree(root: Path, file_obj, prefix: str = "") -> None:
    # собираем директории и файлы по отдельности
    entries = [p for p in root.iterdir() if p.name not in IGNORE_FILES]
    entries = [
        p for p in entries
        if (p.is_dir() and p.name not in IGNORE_DIRS) or p.is_file()
    ]
    # сначала директории, потом файлы, по имени
    entries.sort(key=lambda p: (p.is_file(), p.name))

    for index, path in enumerate(entries):
        is_last = index == len(entries) - 1
        connector = "└── " if is_last else "├── "
        # пишем сразу в файл, чтобы обойти кодировку консоли Windows
        file_obj.write(f"{prefix}{connector}{path.name}\n")
        
        if path.is_dir():
            extension = "    " if is_last else "│   "
            print_tree(path, file_obj, prefix + extension)

if __name__ == "__main__":
    # определяем пути
    # Этот скрипт лежит в tools/solid_verifier/solid_dashboard/report/
    report_dir = Path(__file__).resolve().parent
    
    # Поднимаемся на 3 уровня вверх до директории tools
    # report -> solid_dashboard -> solid_verifier -> tools
    tools_dir = report_dir.parents[2]
    
    # Формируем путь для выходного файла в папке report
    output_file = report_dir / "project_tree.txt"

    # открываем файл с явной кодировкой utf-8
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"{tools_dir.name}/\n")
        print_tree(tools_dir, f)
        
    print(f"Project tree for '{tools_dir}' has been saved to: {output_file}")

