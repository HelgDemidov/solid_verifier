# ===================================================================================================
# Эвристический адаптер (Heuristics Adapter)
# 
# Ключевая роль: Мост между быстрым детерминированным парсингом кода и глубоким семантическим 
# анализом LLM. Действует как "фильтр", отбирающий только подозрительные участки кода.
# 
# Основные архитектурные задачи:
# 1. Рекурсивное сканирование целевой директории с жестким соблюдением ignore_dirs.
# 2. Построение глобальной карты проекта (ProjectMap) на основе AST-деревьев, фиксирующей 
#    все классы, методы, интерфейсы и их взаимосвязи.
# 3. Применение легковесных AST-эвристик (OCP-H-*, LSP-H-*) для выявления кандидатов на 
#    нарушение принципов SOLID (в первую очередь OCP и LSP).
# 4. Формирование изолированного контекста для оркестратора, который затем будет передан 
#    LLM-адаптеру для финальной верификации (что экономит токены и время).
# ===================================================================================================


import os  # комментарий: нужен для рекурсивного обхода директорий через os.walk
import logging
from pathlib import Path
from typing import Any, Dict, List


from ..interfaces.analyzer import IAnalyzer  # комментарий: общий протокол адаптеров
from ..llm.ast_parser import build_project_map  # комментарий: Шаг 0 — ProjectMap
from ..llm.heuristics import identify_candidates  # комментарий: Шаг 1b — эвристики OCPLSP


logger = logging.getLogger(__name__)


class HeuristicsAdapter(IAnalyzer):
    # комментарий: человекочитаемое имя адаптера для отчета и логов
    @property
    def name(self) -> str:
        return "heuristics"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # target_dir уже указывает на нужную папку (например app) так как pipeline.py вычислил ее заранее
        # Нам остается только резолвить этот путь в абсолютный для безопасных файловых операций
        analysis_root = Path(target_dir).resolve()

        # Инициализируем базовый контекст для хранения промежуточных результатов работы эвристик
        heuristics_context: Dict[str, Any] = {
            "project_map": None,
            "project_map_summary": {"classes": 0, "interfaces": 0},
            "candidates": [],
            "findings": [],
            "warning": None,
        }

        # Если целевая папка не найдена логируем предупреждение и возвращаем пустой контекст без падения пайплайна
        if not analysis_root.exists():
            warning_msg = f"Analysis root not found: {analysis_root}"
            logger.warning(f"HeuristicsAdapter: {warning_msg}")
            
            heuristics_context["warning"] = warning_msg
            context["heuristics"] = heuristics_context
            
            return {
                "project_map_summary": heuristics_context["project_map_summary"],
                "heuristic_findings": [],
                "ocp_lsp_candidates": [],
                "warning": warning_msg
            }

        # Извлекаем список папок для игнорирования из конфига и очищаем их от пустых строк
        ignore_dirs_cfg = config.get("ignore_dirs") or []
        ignore_dirs = [name.strip() for name in ignore_dirs_cfg if name and name.strip()]

        input_paths: List[Path] = []
        
        # Обходим директорию рекурсивно для сбора всех python-файлов в проекте
        for dirpath, dirnames, filenames in os.walk(analysis_root):
            current = Path(dirpath)
            
            # Модифицируем список dirnames in-place чтобы функция os.walk не спускалась в игнорируемые папки
            dirnames[:] = [d for d in dirnames if d not in ignore_dirs]
            
            # Фильтруем файлы оставляя только исходники на Python
            for f in filenames:
                if f.endswith(".py"):
                    input_paths.append(current / f)

        # --- Запуск AST-парсера и эвристик ---
        project_map = build_project_map(input_paths)

        # -------------------------------------------------------------------
        # 6. Прогоняем эвристики OCPLSP по ProjectMap
        # -------------------------------------------------------------------
        heuristic_result = identify_candidates(project_map)

        # -------------------------------------------------------------------
        # 7. Считаем summary и обновляем стабильный context
        # -------------------------------------------------------------------
        classes_count = len(project_map.classes)
        interfaces_count = len(project_map.interfaces)
        candidates_count = len(heuristic_result.candidates)

        project_map_summary = {
            "classes": classes_count,
            "interfaces": interfaces_count,
        }

        heuristics_context.update(
            {
                "project_map": project_map,
                "project_map_summary": project_map_summary,
                "candidates": heuristic_result.candidates,
                "findings": heuristic_result.findings,
            }
        )

        # комментарий: кладем в context полный объект ProjectMap и сырые доменные
        # сущности для downstream-LLM-логики. Это отдельный runtime-канал, не JSON-report.
        context["heuristics"] = heuristics_context

        # -------------------------------------------------------------------
        # 8. Логируем итоги для наглядности в консоли
        # -------------------------------------------------------------------
        logger.info(
            "HeuristicsAdapter: ProjectMap built with %d classes, %d interfaces; "
            "%d OCPLSP candidates identified",
            classes_count,
            interfaces_count,
            candidates_count,
        )

        # логируем итоговую директорию анализа и количество найденных файлов
        logger.info(
            "HeuristicsAdapter: analysis_root=%s, ignore_dirs=%s, input_paths_count=%d",
            analysis_root,
            ignore_dirs,
            len(input_paths),
        )

        logger.info(
            "HeuristicsAdapter: sample input_paths=%s",
            [str(p) for p in input_paths[:10]],
        )

        logger.info(
            "HeuristicsAdapter: ProjectMap classes=%d interfaces=%d",
            len(project_map.classes),
            len(project_map.interfaces),
        )

        logger.info(
            "HeuristicsAdapter: findings=%d candidates=%d",
            len(heuristic_result.findings),
            len(heuristic_result.candidates),
        )

        logger.info(
            "HeuristicsAdapter: sample classes=%s",
            list(project_map.classes.keys())[:15],
        )

        logger.info(
            "HeuristicsAdapter: sample candidates=%s",
            [c.class_name for c in heuristic_result.candidates[:10]],
        )

        logger.info(
            "HeuristicsAdapter: context['heuristics'] prepared with keys=%s",
            list(heuristics_context.keys()),
        )

        # -------------------------------------------------------------------
        # 9. Возвращаем компактную JSON-friendly сводку для отчета
        # -------------------------------------------------------------------
        # комментарий: наружу возвращаем summary + findings/candidates.
        # Сам ProjectMap остается в context как runtime-объект для LLM-слоя.
        return {
            "project_map_summary": project_map_summary,
            "heuristic_findings": heuristic_result.findings,
            "ocplsp_candidates": heuristic_result.candidates,
        }