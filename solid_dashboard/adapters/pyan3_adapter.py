# ===================================================================================================
# Адаптер графа вызовов (Pyan3 Adapter)
#
# Ключевая роль: Построение статического графа вызовов (Call Graph) и выявление "мертвого" кода.
#
# Основные архитектурные задачи:
# 1. Изолированный запуск утилиты pyan3 через subprocess (через параметр cwd) без изменения
#    глобального состояния процесса (без os.chdir).
# 2. Ручной сбор Python-файлов перед запуском с фильтрацией через ignore_dirs.
# 3. Парсинг текстового вывода pyan3 для извлечения узлов (функций/классов) и ребер (вызовов).
# 4. Разделение узлов на три категории:
#    - root_nodes  — точки входа (нет входящих, есть исходящие ребра; это ожидаемо)
#    - dead_nodes  — подлинно неиспользуемые узлы (нет ни входящих, ни исходящих ребер)
#    - остальные   — нормально связанные узлы графа
# ===================================================================================================

from __future__ import annotations
import warnings
import os       # работа с файловой системой и путями
import re       # регулярки для валидации имен узлов и парсинга вывода pyan3
import subprocess  # запуск pyan3 как CLI-инструмента
from typing import Any, Dict, List, Set, Optional

from solid_dashboard.interfaces.analyzer import IAnalyzer  # общий протокол адаптеров пайплайна

# Паттерн валидного Python qualified name: идентификатор с возможными точками (foo.bar.Baz)
_VALID_PY_NAME = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$')

# Префиксы строк, которые Pyan3 может выводить в stdout как диагностику
_PYAN3_DIAG_PREFIXES = ("WARNING:", "ERROR:", "INFO:", "CRITICAL:")


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
            return self._error(
                "pyan3 executable not found. Make sure pyan3 is installed in the virtual environment."
            )

        if completed.returncode != 0:
            stderr = completed.stderr.strip() or "Unknown pyan3 error."
            return self._error(
                f"pyan3 failed with exit code {completed.returncode}: {stderr}",
                raw_output=completed.stdout
            )

        raw_output = completed.stdout
        nodes: Set[str] = set()
        edges: List[Dict[str, str]] = []

        current_src: Optional[str] = None

        for line in raw_output.splitlines():
            stripped = line.strip()

            # Пропускаем пустые строки
            if not stripped:
                continue

            # Пропускаем диагностические строки pyan3 (могут попадать в stdout при --quiet)
            if stripped.upper().startswith(_PYAN3_DIAG_PREFIXES):
                continue

            if not line.startswith(" "):
                # Строка без отступа — новый source-блок
                # Принимаем только валидные Python qualified names чтобы отсечь артефакты вывода
                if _VALID_PY_NAME.match(stripped):
                    current_src = stripped
                    nodes.add(current_src)
                continue

            # Строки с отступом — ребра текущего source-блока
            if not stripped.startswith("[U]"):
                continue

            used_name = stripped[len("[U]"):].strip()

            # Пропускаем пустые или невалидные имена и висячие ребра без source
            if not used_name or current_src is None:
                continue
            if not _VALID_PY_NAME.match(used_name):
                continue

            # Исправление 4: фильтрация self-loop ребер (узел ссылается на самого себя)
            if used_name == current_src:
                continue

            nodes.add(used_name)
            edges.append({"from": current_src, "to": used_name})

        # Санити-чек вынесен из цикла: узлы есть, но ребра не построены — признак сломанного парсера
        if nodes and not edges:
            sample_lines = [ln for ln in raw_output.splitlines() if ln.strip()][:6]
            parser_warning = (
                f"Sanity check: {len(nodes)} nodes parsed but 0 edges. "
                f"Likely parser/format mismatch. "
                f"First lines of raw_output: {sample_lines}"
            )
            warnings.warn(parser_warning, RuntimeWarning, stacklevel=2)

        # Грязный хардкод "app.routers" удален.
        # Адаптер возвращает чистый граф вызовов оставляя фильтрацию потребителю.

        used_nodes: Set[str] = set()
        for e in edges:
            used_nodes.add(e["from"])
            used_nodes.add(e["to"])

        nodes = used_nodes | nodes

        # Де-дупликация ребер
        unique_edges: Set[tuple[str, str]] = set()
        for e in edges:
            unique_edges.add((e["from"], e["to"]))

        edges = [{"from": src, "to": dst} for src, dst in unique_edges]

        # Исправление 1: разделение узлов на root_nodes и dead_nodes
        incoming_count: Dict[str, int] = {n: 0 for n in nodes}
        for e in edges:
            dst = e["to"]
            if dst in incoming_count:
                incoming_count[dst] += 1

        outgoing_nodes: Set[str] = {e["from"] for e in edges}

        # root_nodes — нет входящих ребер, но есть исходящие (entry points, ожидаемое поведение)
        root_nodes = sorted([
            n for n in nodes
            if incoming_count[n] == 0 and n in outgoing_nodes
        ])

        # dead_nodes — нет ни входящих, ни исходящих ребер (подлинно неиспользуемый код)
        dead_nodes = sorted([
            n for n in nodes
            if incoming_count[n] == 0 and n not in outgoing_nodes
        ])

        node_list = sorted(list(nodes))

        # Исправление 2: возвращаемый словарь дополнен root_nodes-полями
        return {
            "is_success": True,
            "node_count": len(node_list),
            "edge_count": len(edges),
            "nodes": node_list,
            "edges": edges,
            "dead_node_count": len(dead_nodes),
            "dead_nodes": dead_nodes,
            "root_node_count": len(root_nodes),
            "root_nodes": root_nodes,
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
            "root_node_count": 0,
            "root_nodes": [],
            "raw_output": raw_output,
        }