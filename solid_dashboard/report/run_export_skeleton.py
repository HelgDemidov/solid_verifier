# Скрипт для формирования актуальной "маски" проекта (структура папок и файлов)
# Команда для запуска из корня (scopus_search_code): python run_export_skeleton.py

import sys
import subprocess
from pathlib import Path

if __name__ == "__main__":
    # вычисляем пути относительно корня проекта
    project_root = Path(__file__).resolve().parent
    script_path = project_root / "docs" / "export_skeleton.py"

    cmd = [
        sys.executable,
        str(script_path),
    ]

    raise SystemExit(subprocess.call(cmd))
