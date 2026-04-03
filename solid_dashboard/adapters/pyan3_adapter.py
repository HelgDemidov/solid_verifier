# ===================================================================================================
# Адаптер графа вызовов (Pyan3 Adapter)
# 
# Ключевая роль: Построение статического графа вызовов (Call Graph) и выявление "мертвого" кода.
# 
# Основные архитектурные задачи:
# 1. Изолированный запуск утилиты pyan3 через subprocess (через параметр cwd) без изменения глобального состояния процесса (без os.chdir).
# 2. Ручной сбор Python-файлов перед запуском с фильтрацией через ignore_dirs.
# 3. Парсинг текстового вывода pyan3 для извлечения узлов (функций/классов) и ребер (вызовов).
# 4. Расчет входящих вызовов для выявления "повисших" (неиспользуемых) узлов.
# ===================================================================================================

from __future__ import annotations

import os  # работа с файловой системой и путями
import re  # регулярки для парсинга вывода pyan3
import subprocess  # запуск pyan3 как CLI-инструмента
from typing import Any, Dict, List, Set

from solid_dashboard.interfaces.analyzer import IAnalyzer  # общий протокол адаптеров пайплайна 

class Pyan3Adapter(IAnalyzer):
    # Адаптер для pyan3: строит граф вызовов на уровне функций/методов
    @property
    def name(self) -> str:
        # ключ, под которым результат адаптера попадает в итоговый JSON
        return "pyan3"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # параметр context требуется интерфейсом IAnalyzer но не используется здесь
        _ = context

        project_root = os.path.dirname(os.path.abspath(target_dir))
        appdir = os.path.abspath(target_dir)

        # 1. Извлекаем ignore_dirs
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = [d.strip() for d in ignore_dirs_cfg if d and d.strip()]

        # 2. Безопасно собираем список python-файлов обходя ignore_dirs
        py_files = []
        for root, dirs, files in os.walk(appdir):
            # In-place очистка директорий
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for f in files:
                if f.endswith(".py"):
                    # Используем относительные пути чтобы не превысить лимит длины CLI-команды ОС
                    rel_path = os.path.relpath(os.path.join(root, f), project_root)
                    py_files.append(rel_path)

        if not py_files:
            return self._error("No python files found for pyan3 analysis.")

        # 3. Формируем команду передавая конкретный чистый список файлов
        cmd = ["pyan3"] + py_files + ["--uses", "--no-defines", "--text", "--quiet"]

        try:
            # 4. Используем параметр cwd вместо глобального и опасного os.chdir()
            completed = subprocess.run(
                cmd, 
                cwd=project_root, 
                check=False, 
                capture_output=True, 
                text=True
            )
        except FileNotFoundError:
            return self._error("pyan3 executable not found. Make sure pyan3 is installed in the virtual environment.")

        if completed.returncode != 0:
            stderr = completed.stderr.strip() or "Unknown pyan3 error."
            return self._error(f"pyan3 failed with exit code {completed.returncode}: {stderr}", raw_output=completed.stdout)

        raw_output = completed.stdout
        nodes: Set[str] = set()
        edges: List[Dict[str, str]] = []

        current_src: str | None = None

        for line in raw_output.splitlines():
            if not line.strip():
                continue

            if not line.startswith(" "):
                current_src = line.strip()
                nodes.add(current_src)
                continue

            stripped = line.strip()
            if not stripped.startswith("- U"):
                continue

            used_name = stripped[len("- U"):].strip()
            if not used_name or current_src is None:
                continue

            nodes.add(used_name)
            edges.append({"from": current_src, "to": used_name})

        # грязный хардкод "app.routers" удален. 
        # Адаптер возвращает чистый граф вызовов оставляя фильтрацию потребителю.

        used_nodes: Set[str] = set()
        for e in edges:
            used_nodes.add(e["from"])
            used_nodes.add(e["to"])

        nodes = used_nodes | nodes

        unique_edges: Set[tuple[str, str]] = set()
        for e in edges:
            unique_edges.add((e["from"], e["to"]))

        edges = [{"from": src, "to": dst} for src, dst in unique_edges]

        incoming_count: Dict[str, int] = {n: 0 for n in nodes}
        for e in edges:
            dst = e["to"]
            if dst in incoming_count:
                incoming_count[dst] += 1

        dead_nodes = sorted([n for n, cnt in incoming_count.items() if cnt == 0])
        node_list = sorted(list(nodes))

        return {
            "is_success": True,
            "node_count": len(node_list),
            "edge_count": len(edges),
            "nodes": node_list,
            "edges": edges,
            "dead_node_count": len(dead_nodes),
            "dead_nodes": dead_nodes,
            "raw_output": raw_output,
        }

    @staticmethod
    def _error(message: str, raw_output: str = "") -> Dict[str, Any]:
    # Утилитный метод для формирования стандартного ответа с ошибкой
        return {
            "is_success": False,
            "error": message,
            "node_count": 0,
            "edge_count": 0,
            "nodes": [],
            "edges": [],
            "dead_node_count": 0,
            "dead_nodes": [],
            "raw_output": raw_output,
        }