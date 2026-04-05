# ===================================================================================================
# Адаптер Radon & Lizard (Radon & Lizard Adapter)
# 
# Ключевая роль: Сбор метрик цикломатической сложности (Cyclomatic Complexity) и параметров функций для оценки поддерживаемости кода.
# 
# Основные архитектурные задачи:
# 1. Запуск утилиты Radon через subprocess с жестким ограничением области анализа (target_dir).
# 2. Применение параметров игнорирования (ignore_dirs) через CLI-флаги для исключения "шума".
# 3. Интеграция библиотеки Lizard как дополнения для подсчета аргументов функций 
# (поиск кандидатов на нарушение ISP - Interface Segregation Principle).
# 4. Безопасная обработка отсутствия CLI-утилит и нормализация JSON-вывода.
# ===================================================================================================

import subprocess
import json
import os
import warnings
from pathlib import Path
from typing import Dict, Any
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

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # параметр context требуется интерфейсом IAnalyzer но не используется здесь
        _ = context

        # 1. Извлекаем ignore_dirs из конфига
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = [d.strip() for d in ignore_dirs_cfg if d and d.strip()]

        # 2. Формируем команду Radon с флагом -i (--ignore) для папок
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
            return {"error": f"Radon execution failed: {e.stderr}"}
        except json.JSONDecodeError:
            return {"error": "Failed to parse Radon JSON output"}

        items = []
        high_complexity_count = 0
        total_cc = 0

        for filepath, blocks in raw_data.items():
            if isinstance(blocks, str):
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

        if lizard is not None and items:
            # 3. Транслируем ignore_dirs в формат глобов для Lizard
            lizard_excludes = [f"*/{d}/*" for d in ignore_dirs]
            lizard_results = lizard.analyze([target_dir], exclude_pattern=lizard_excludes)
            
            lizard_index = {}
            for fileinfo in lizard_results:
                try:
                    abspath = str(Path(fileinfo.filename).resolve())
                    if abspath not in lizard_index:
                        lizard_index[abspath] = {}
                    for func in fileinfo.function_list:
                        lizard_index[abspath][func.start_line] = func
                except Exception as exc:
                    # сбой индексации конкретного файла lizard не должен остановить пайплайн,
                    # но должен быть виден в solid_pipeline.log для диагностики
                    warnings.warn(
                        f"[radon_adapter] lizard failed to index file "
                        f"'{getattr(fileinfo, 'filename', '?')}': {exc}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    continue

            for item in items:
                filepath = item.get("filepath")
                lineno = item.get("lineno")
                if filepath and lineno:
                    try:
                        abspath = str(Path(filepath).resolve())
                        liz_func = lizard_index.get(abspath, {}).get(lineno)
                        if liz_func:
                            item["parameter_count"] = liz_func.parameter_count
                    except Exception as exc:
                        # сбой обогащения parameter_count для одной функции не критичен,
                        # но фиксируем в лог чтобы отследить паттерн (например, match/case)
                        warnings.warn(
                            f"[radon_adapter] parameter_count enrichment failed "
                            f"for '{filepath}':{lineno} — {exc}",
                            RuntimeWarning,
                            stacklevel=2,
                        )

        total_items = len(items)
        mean_cc = round(total_cc / total_items, 2) if total_items > 0 else 0.0

        return {
            "total_items": total_items,
            "mean_cc": mean_cc,
            "high_complexity_count": high_complexity_count,
            "items": sorted(items, key=lambda x: x["complexity"], reverse=True),
            "lizard_used": LIZARD_AVAILABLE
        }
