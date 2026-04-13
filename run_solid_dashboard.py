# Запуск пайплайна из корня solid-verifier: python run_solid_dashboard.py

import subprocess
import sys
from pathlib import Path

if __name__ == "__main__":
    # Корень репо solid-verifier — там где лежит этот скрипт
    project_root = Path(__file__).resolve().parent

    # target_dir и config — в корне репо
    target_dir = project_root
    config_path = project_root / "solid_config.json"

    # solid_dashboard лежит прямо в корне (не в tools/solid_verifier)
    verifier_dir = project_root / "solid_dashboard"

    cmd = [
        sys.executable,
        "-m", "solid_dashboard",
        "--target-dir", str(target_dir),
        "--config", str(config_path),
    ]

    # cwd — корень репо (solid_dashboard виден как пакет)
    raise SystemExit(subprocess.call(cmd, cwd=project_root))