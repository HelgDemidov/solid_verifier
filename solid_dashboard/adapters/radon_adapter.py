# ===================================================================================================
# Адаптер Radon & Lizard (Radon & Lizard Adapter)
#
# Ключевая роль: Сбор метрик цикломатической сложности (CC) и индекса поддерживаемости (MI)
# для оценки поддерживаемости кода, а также подсчет параметров функций для диагностики ISP.
#
# Основные архитектурные задачи:
# 1. Запуск `radon cc` через subprocess — плоский список функций/методов с CC и рангом.
# 2. Запуск `radon mi` через subprocess — поуфайловый MI (0–100) с рангом A/B/C.
# 3. Применение ignore_dirs через CLI-флаг -i для обоих вызовов;
#    пустые строки и пробелы фильтруются до передачи в команду.
# 4. Интеграция Lizard как дополнения: только parameter_count (ISP-кандидаты).
#    Дублирующиеся метрики (CC) игнорируются — приоритет у radon.
# 5. Сбой MI изолирован: не обрушивает CC-результат, возвращает пустой dict.
# 6. Безопасная обработка отсутствия CLI-утилит и нормализация JSON-вывода.
# ===================================================================================================

import subprocess
import json
import warnings
from pathlib import Path
from typing import Dict, Any, List
from solid_dashboard.interfaces.analyzer import IAnalyzer  # явный импорт интерфейса

lizard = None

try:
    import lizard as _lizard  # type: ignore[import]
    lizard = _lizard
    LIZARD_AVAILABLE = True
except ImportError:
    LIZARD_AVAILABLE = False


class RadonAdapter(IAnalyzer):
    @property
    def name(self) -> str:
        return "radon"

    # ------------------------------------------------------------------
    # Публичный метод интерфейса
    # ------------------------------------------------------------------

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # параметр context требуется интерфейсом IAnalyzer но не используется здесь
        _ = context

        # 1. Извлекаем и нормализуем ignore_dirs из конфига
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = [d.strip() for d in ignore_dirs_cfg if d and d.strip()]

        # 2. Формируем команду radon cc с флагом -i (--ignore) для папок
        cmd = ["radon", "cc", "--json", target_dir]
        if ignore_dirs:
            cmd.extend(["-i", ",".join(ignore_dirs)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            raw_data = json.loads(result.stdout)
        except FileNotFoundError:
            # защита от неустановленного пакета radon
            return {"error": "Radon executable not found. Please install radon."}
        except subprocess.CalledProcessError as e:
            # e.stderr может быть None если subprocess поднят без capture_output
            stderr_msg = e.stderr or "(no stderr)"
            return {"error": f"Radon execution failed: {stderr_msg}"}
        except json.JSONDecodeError:
            return {"error": "Failed to parse Radon JSON output"}

        items: List[Dict[str, Any]] = []
        high_complexity_count = 0
        total_cc = 0

        for filepath, blocks in raw_data.items():
            if isinstance(blocks, str):
                # radon записывает строку-ошибку (SyntaxError и т.п.) вместо списка блоков
                continue

            for block in blocks:
                if block.get("type") in ("function", "method"):
                    complexity = block.get("complexity", 0)
                    total_cc += complexity
                    if complexity > 10:
                        high_complexity_count += 1

                    items.append({
                        "name": block.get("name"),
                        "type": block.get("type"),
                        "complexity": complexity,
                        "rank": block.get("rank", "A"),
                        "lineno": block.get("lineno", 0),
                        "filepath": filepath
                    })

        # 3. Обогащение parameter_count через Lizard (только если lizard доступен)
        if lizard is not None and items:
            lizard_excludes = [f"*/{d}/*" for d in ignore_dirs]
            lizard_results = lizard.analyze([target_dir], exclude_pattern=lizard_excludes)

            lizard_index: Dict[str, Dict[int, Any]] = {}
            for fileinfo in lizard_results:
                try:
                    abspath = str(Path(fileinfo.filename).resolve())
                    if abspath not in lizard_index:
                        lizard_index[abspath] = {}
                    for func in fileinfo.function_list:
                        lizard_index[abspath][func.start_line] = func
                except Exception as exc:
                    warnings.warn(
                        f"[radon_adapter] lizard failed to index file "
                        f"'{getattr(fileinfo, 'filename', '?')}': {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue

            for item in items:
                fp = item.get("filepath")
                lineno = item.get("lineno")
                if fp and lineno:
                    try:
                        abspath = str(Path(fp).resolve())
                        liz_func = lizard_index.get(abspath, {}).get(lineno)
                        if liz_func:
                            item["parameter_count"] = liz_func.parameter_count
                    except Exception as exc:
                        warnings.warn(
                            f"[radon_adapter] parameter_count enrichment failed "
                            f"for '{fp}':{lineno} — {exc}",
                            RuntimeWarning,
                            stacklevel=2,
                        )

        total_items = len(items)
        mean_cc = round(total_cc / total_items, 2) if total_items > 0 else 0.0

        # 4. Вычисляем MI (сбой изолирован — не ломает CC-результат)
        mi_result = self._run_mi(target_dir, ignore_dirs)

        return {
            "total_items": total_items,
            "mean_cc": mean_cc,
            "high_complexity_count": high_complexity_count,
            "items": sorted(items, key=lambda x: x["complexity"], reverse=True),
            "maintainability": mi_result,
            "lizard_used": LIZARD_AVAILABLE,
        }

    # ------------------------------------------------------------------
    # Приватный метод: Maintainability Index (radon mi)
    # ------------------------------------------------------------------

    def _run_mi(
        self,
        target_dir: str,
        ignore_dirs: List[str],
    ) -> Dict[str, Any]:
        """Запускает `radon mi --json` и возвращает поуфайловый MI.

        radon mi --json выдаёт:
            {"path/to/file.py": {"mi": 72.3, "rank": "A"}, ...}

        Возвращаемый dict:
            {
                "total_files": int,
                "mean_mi": float,
                "low_mi_count": int,   # файлы с rank C (MI < 10)
                "files": [
                    {"filepath": str, "mi": float, "rank": str},
                    ...  # отсортировано по mi ASC — худшие первыми
                ]
            }

        При любом сбое (FileNotFoundError, CalledProcessError, JSONDecodeError)
        возвращает пустой dict {} — сбой MI не прерывает CC-результат.
        """
        # собираем команду radon mi — те же ignore_dirs что и у cc
        cmd = ["radon", "mi", "--json", target_dir]
        if ignore_dirs:
            cmd.extend(["-i", ",".join(ignore_dirs)])

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            raw = json.loads(result.stdout)
        except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError):
            # сбой MI изолирован: не ломает CC-результат
            return {}

        files: List[Dict[str, Any]] = []
        total_mi = 0.0
        low_mi_count = 0  # rank C — MI < 10, трудноподдерживаемые файлы

        for filepath, data in raw.items():
            if not isinstance(data, dict):
                continue
            mi_val = float(data.get("mi", 0.0))
            rank = str(data.get("rank", "A"))
            total_mi += mi_val
            if rank == "C":
                low_mi_count += 1
            files.append({
                "filepath": filepath,
                "mi": round(mi_val, 2),
                "rank": rank,
            })

        count = len(files)
        return {
            "total_files": count,
            "mean_mi": round(total_mi / count, 2) if count > 0 else 0.0,
            "low_mi_count": low_mi_count,
            # худшие (наименее поддерживаемые) файлы первыми
            "files": sorted(files, key=lambda x: x["mi"]),
        }
