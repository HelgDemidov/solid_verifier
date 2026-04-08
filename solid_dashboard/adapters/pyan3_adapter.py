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
# 5. Confidence-маркировка ребер (Вариант B):
#    - "high" — ребро надежное: источник однозначен И цель не является suspicious-узлом
#    - "low"  — ребро ненадежное по одному из двух признаков:
#               (a) блок-источник содержит кратные [U]-вхождения (name collision в источнике)
#               (b) цель является suspicious-узлом (cascaded propagation)
# 6. Защита от высокого collision rate (нестандартные репозитории без __init__.py):
#    - Шаг 2: предупреждение до запуска pyan3, если target_dir не является Python-пакетом
#    - Шаг 1/3: вычисление collision_rate и предупреждение при превышении порога из конфига
#    - Шаг 4: опциональный abort (abort_on_high_collision) при критически высоком rate
# ===================================================================================================

from __future__ import annotations
import warnings
import os       # работа с файловой системой и путями
import re       # регулярки для валидации имен узлов и парсинга вывода pyan3
import subprocess  # запуск pyan3 как CLI-инструмента
from collections import Counter  # подсчет кратных [U]-вхождений для confidence-детектора
from typing import Any, Dict, List, Set, Optional

from solid_dashboard.interfaces.analyzer import IAnalyzer  # общий протокол адаптеров пайплайна

# Паттерн валидного Python qualified name: идентификатор с возможными точками (foo.bar.Baz)
_VALID_PY_NAME = re.compile(r'^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$')

# Префиксы строк, которые Pyan3 может выводить в stdout как диагностику
_PYAN3_DIAG_PREFIXES = ("WARNING:", "ERROR:", "INFO:", "CRITICAL:")

# Дефолтные значения для секции pyan3 в конфиге
_DEFAULT_COLLISION_THRESHOLD = 0.35
_DEFAULT_ABORT_ON_HIGH_COLLISION = False


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

        # Читаем настройки collision-guard из секции pyan3 конфига (с дефолтами)
        pyan3_cfg = config.get("pyan3") or {}
        collision_threshold: float = float(
            pyan3_cfg.get("collision_rate_threshold", _DEFAULT_COLLISION_THRESHOLD)
        )
        abort_on_high_collision: bool = bool(
            pyan3_cfg.get("abort_on_high_collision", _DEFAULT_ABORT_ON_HIGH_COLLISION)
        )

        # 1. Извлекаем ignore_dirs
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = [d.strip() for d in ignore_dirs_cfg if d and d.strip()]

        # Шаг 2: ранняя проверка — является ли target_dir корнем Python-пакета
        # Отсутствие __init__.py означает, что pyan3 будет использовать короткие
        # (неквалифицированные) имена узлов, что резко повышает вероятность коллизий
        if not os.path.exists(os.path.join(appdir, "__init__.py")):
            warnings.warn(
                f"Pyan3Adapter: target_dir '{appdir}' has no __init__.py. "
                "Pyan3 will likely use short (unqualified) node names, "
                "increasing name collision risk. "
                "Consider using a properly packaged Python project as target.",
                RuntimeWarning,
                stacklevel=2,
            )

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

        # 5. Первый проход: детектируем блоки с name collision (confidence-детектор Варианта B)
        #
        #    Механика ложных ребер в pyan3 text-режиме:
        #    Когда несколько сущностей с одинаковым коротким именем (например, метод
        #    UserService.login и router-функция login) существуют в анализируемом коде,
        #    pyan3 сливает их в один text-блок под общим именем "login". В результате блок
        #    получает [U]-ребра сразу от нескольких сущностей, и часть ребер становится
        #    ложными (cross-attribution). Признак слияния: одно и то же [U]-имя встречается
        #    в блоке более одного раза (до де-дупликации).
        suspicious_blocks = _detect_suspicious_blocks(raw_output)

        # 6. Второй проход: парсим узлы и ребра, выставляем confidence по источнику
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

            # Фильтрация self-loop ребер (узел ссылается на самого себя)
            if used_name == current_src:
                continue

            nodes.add(used_name)

            # Confidence по источнику: если блок suspicious — ребро ненадежно
            confidence = "low" if current_src in suspicious_blocks else "high"
            edges.append({"from": current_src, "to": used_name, "confidence": confidence})

        # Санити-чек: узлы есть, но ребра не построены — признак сломанного парсера
        if nodes and not edges:
            sample_lines = [ln for ln in raw_output.splitlines() if ln.strip()][:6]
            parser_warning = (
                f"Sanity check: {len(nodes)} nodes parsed but 0 edges. "
                f"Likely parser/format mismatch. "
                f"First lines of raw_output: {sample_lines}"
            )
            warnings.warn(parser_warning, RuntimeWarning, stacklevel=2)

        used_nodes: Set[str] = set()
        for e in edges:
            used_nodes.add(e["from"])
            used_nodes.add(e["to"])

        nodes = used_nodes | nodes

        # Де-дупликация ребер: при совпадении (from, to) — схлопываем по пессимистичной стратегии.
        # При конфликте confidence для одной пары (from, to) побеждает "low" — заражает "high".
        # Обоснование: если хотя бы одно вхождение ребра пришло из suspicious-блока,
        # значит ребро ненадежно по меньшей мере в одном из источников слияния.
        # Повышать его до "high" на основании другого вхождения семантически неверно.
        best_confidence: Dict[tuple[str, str], str] = {}
        for e in edges:
            key = (e["from"], e["to"])
            current_conf = best_confidence.get(key)
            # "low" заражает "high": если новое вхождение "low" — понижаем итоговый confidence
            if current_conf is None or (e["confidence"] == "low" and current_conf == "high"):
                best_confidence[key] = e["confidence"]

        edges = [
            {"from": src, "to": dst, "confidence": conf}
            for (src, dst), conf in best_confidence.items()
        ]

        # 7. Cascaded propagation: понижаем confidence до "low" если ЦЕЛЬ является suspicious-узлом
        #
        #    Обоснование: если цель ребра является suspicious-узлом (name collision), то
        #    downstream-потребитель, прибыв по этому ребру, попадает в амбигуозный узел
        #    и может считать результат достоверным, тогда как это не так.
        #    Операция монотонна: только понижает confidence, никогда не повышает.
        #    Применяется ПОСЛЕ де-дупликации, чтобы не нарушать ее логику.
        for e in edges:
            if e["confidence"] == "high" and e["to"] in suspicious_blocks:
                e["confidence"] = "low"

        # Разбивка по confidence для итоговой статистики
        high_edges = [e for e in edges if e["confidence"] == "high"]
        low_edges  = [e for e in edges if e["confidence"] == "low"]

        # Разделение узлов на root_nodes и dead_nodes
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

        # Шаги 1, 3, 4: вычисляем collision_rate и применяем защитные меры
        total_nodes = len(node_list)
        collision_rate: float = (
            len(suspicious_blocks) / total_nodes if total_nodes > 0 else 0.0
        )

        if collision_rate > collision_threshold:
            warn_msg = (
                f"Pyan3Adapter: high collision rate detected — "
                f"{collision_rate:.0%} ({len(suspicious_blocks)}/{total_nodes} nodes suspicious). "
                f"Threshold: {collision_threshold:.0%}. "
                "Target project may lack proper __init__.py structure. "
                "Many edges are marked low-confidence and may be unreliable."
            )
            warnings.warn(warn_msg, RuntimeWarning, stacklevel=2)

            # Шаг 4: при abort_on_high_collision=true возвращаем ошибку вместо ненадежных данных
            if abort_on_high_collision:
                return self._error(
                    f"Aborted: collision_rate {collision_rate:.0%} exceeds threshold "
                    f"{collision_threshold:.0%} and abort_on_high_collision is enabled. "
                    "Set abort_on_high_collision=false in solid_config.json to receive "
                    "low-confidence results instead.",
                    raw_output=raw_output,
                )

        return {
            "is_success": True,
            "node_count": len(node_list),
            "edge_count": len(edges),
            "edge_count_high": len(high_edges),
            "edge_count_low": len(low_edges),
            "nodes": node_list,
            "edges": edges,
            "dead_node_count": len(dead_nodes),
            "dead_nodes": dead_nodes,
            "root_node_count": len(root_nodes),
            "root_nodes": root_nodes,
            "suspicious_blocks": sorted(suspicious_blocks),
            # Шаг 1: collision_rate для downstream-потребителей (LLM-слой, отчет)
            "collision_rate": round(collision_rate, 4),
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
            "edge_count_high": 0,
            "edge_count_low": 0,
            "nodes": [],
            "edges": [],
            "dead_node_count": 0,
            "dead_nodes": [],
            "root_node_count": 0,
            "root_nodes": [],
            "suspicious_blocks": [],
            "collision_rate": 0.0,
            "raw_output": raw_output,
        }


def _detect_suspicious_blocks(raw_output: str) -> Set[str]:
    """Первый проход по raw_output: возвращает множество имен блоков,
    в которых хотя бы одно [U]-имя встречается более одного раза (до де-дупликации).
    Это признак слияния нескольких сущностей в один text-блок (name collision pyan3).
    Self-loop имена (used == block_name) намеренно исключены из проверки —
    они фильтруются отдельно и не являются признаком cross-attribution.
    """
    suspicious: Set[str] = set()
    current_block: Optional[str] = None
    block_used_counts: Counter = Counter()

    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        if not line.startswith(" "):
            # Завершаем предыдущий блок: проверяем накопленные счетчики
            if current_block is not None:
                for name, cnt in block_used_counts.items():
                    # Кратные вхождения НЕ-self имени — признак cross-attribution
                    if cnt > 1 and name != current_block:
                        suspicious.add(current_block)
                        break

            # Начинаем новый блок
            if _VALID_PY_NAME.match(stripped) and not stripped.upper().startswith(_PYAN3_DIAG_PREFIXES):
                current_block = stripped
                block_used_counts = Counter()
            else:
                current_block = None
                block_used_counts = Counter()
            continue

        if stripped.startswith("[U]") and current_block is not None:
            used = stripped[3:].strip()
            if used and _VALID_PY_NAME.match(used):
                block_used_counts[used] += 1

    # Обрабатываем последний блок в файле
    if current_block is not None:
        for name, cnt in block_used_counts.items():
            if cnt > 1 and name != current_block:
                suspicious.add(current_block)
                break

    return suspicious
