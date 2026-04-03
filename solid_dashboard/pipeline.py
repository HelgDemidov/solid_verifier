# ===================================================================================================
# Оркестратор пайплайна (Pipeline Orchestrator)
# 
# Ключевая роль: Единая точка управления (Single Source of Truth) процессом анализа
# Этот модуль связывает все компоненты SOLID-верификатора воедино и управляет потоком данных
# 
# Основные архитектурные задачи:
# 1. Централизованное вычисление путей: на основе package_root из solid_config.json 
#    вычисляет точную целевую директорию (analysis_root), гарантируя, что ни один 
#    статический адаптер не выйдет за пределы бизнес-логики проекта (например, в .venv).
# 2. Последовательный запуск статических адаптеров с передачей им строго изолированного пути.
# 3. Маршрутизация контекста: передает результаты эвристик (ProjectMap и список кандидатов) 
#    на вход LLM-адаптеру, избегая жесткой связности между модулями.
# 4. Агрегация результатов всех уровней (статика, эвристики, LLM) для генерации отчета.
# ===================================================================================================

import logging
from typing import Dict, Any, List
from pathlib import Path
from .interfaces.analyzer import IAnalyzer

# Импорты для LLM
from solid_dashboard.llm.factory import create_llm_adapter
from solid_dashboard.llm.types import (
    LlmAnalysisInput,
)
# LlmConfig, ProjectMap, LlmCandidate больше напрямую не используем здесь,
# чтобы не дублировать контракт; LlmConfig собирается централизованно в config.load_llm_config.
from solid_dashboard.config import load_llm_config  # type: ignore[import]


logger = logging.getLogger(__name__)


def run_pipeline(target_dir: str, config: Dict[str, Any], adapters: List[IAnalyzer]) -> Dict[str, Any]:
    
    # 1. Централизованно вычисляем корень анализа на основе конфига
    base_path = Path(target_dir).resolve()
    package_root_name = config.get("package_root")
    
    if package_root_name:
        analysis_root = base_path / package_root_name
        if not analysis_root.exists():
            logger.warning(f"Pipeline: package_root '{package_root_name}' not found under {base_path}. Falling back to base.")
            analysis_root = base_path
    else:
        analysis_root = base_path

    logger.info(f"Pipeline: Starting analysis. Target root set to: {analysis_root}")

    context: Dict[str, Any] = {}
    results: Dict[str, Any] = {}

    for adapter in adapters:
        logger.info(f"Running static adapter {adapter.name}")
        try:
            # Передаем адаптерам уточненный корень (analysis_root) вместо корня проекта
            result = adapter.run(str(analysis_root), context, config)
        except Exception as e:
            logger.exception(f"Adapter {adapter.name} failed: {e}")
            # Инициализируем result словарем с ошибкой чтобы избежать UnboundLocalError
            result = {"error": str(e)}

        # Сохраняем результат в локальный словарь и в общий контекст пайплайна
        results[adapter.name] = result
        if adapter.name not in context:
            context[adapter.name] = result

    # 2. Интеграция LLM (Шаг 2 из архитектуры v15)
    # проверяем, включен ли LLM в конфигурации (по умолчанию выключен)
    llm_config_dict = config.get("llm", {})
    is_llm_enabled = llm_config_dict.get("enabled", False)

    if is_llm_enabled:
        logger.info("LLM analysis is enabled. Initializing LLM layer...")

        # -------------------------------------------------------------------
        # LLM-STEP PRECHECK (Диагностика причин status="skipped")
        # -------------------------------------------------------------------
        # извлекаем ProjectMap и кандидатов из КОНТЕКСТА
        # статических адаптеров, а не из JSON-результатов.
        #
        # Важно:
        # - adapter.run(...) может:
        #     * записать "богатый" runtime-объект в context[adapter.name](например, ProjectMap, LlmCandidate и т.д.);
        #     * вернуть "облегченный" dict для JSON-отчета (summary).
        # - цикл выше в run_pipeline больше не перезаписывает context[adapter.name], если адаптер сам его заполнил.
        #
        # Для heuristics ожидается, что:
        #   context["heuristics"]["project_map"]  -> ProjectMap
        #   context["heuristics"]["candidates"]   -> List[LlmCandidate]
        heuristics_result = context.get("heuristics", {})
        project_map = heuristics_result.get("project_map")
        candidates = heuristics_result.get("candidates", [])

        # -------------------------------------------------------------------
        # Временный расширенный precheck-лог для дебага LLM-шага
        # -------------------------------------------------------------------
        # нужен для диагностики причин status="skipped":
        # отсутствует ли heuristics-контекст, пуст ли ProjectMap, или просто нет кандидатов для LLM
        classes_count = (
            len(project_map.classes)
            if project_map is not None and hasattr(project_map, "classes")
            else -1
        )
        interfaces_count = (
            len(project_map.interfaces)
            if project_map is not None and hasattr(project_map, "interfaces")
            else -1
        )
        candidates_count = len(candidates) if candidates is not None else -1

        logger.info(
            "Pipeline LLM precheck: heuristics_present=%s, "
            "project_map_present=%s, classes=%d, interfaces=%d, "
            "candidates_present=%s, candidates_count=%d",
            "heuristics" in context,
            project_map is not None,
            classes_count,
            interfaces_count,
            candidates is not None,
            candidates_count,
        )

        # временный лог-проба, чтобы увидеть,
        # какие именно классы попали в кандидаты. Можно удалить
        # после стабилизации пайплайна.
        if candidates:
            logger.info(
                "Pipeline LLM candidates sample: %s",
                [getattr(c, "class_name", "<unknown>") for c in candidates[:10]],
            )

        # -------------------------------------------------------------------
        # Уточненная логика skip + детализированный reason
        # -------------------------------------------------------------------
        if project_map is None or not candidates:
            # различаем причины skip,
            # чтобы JSON-отчет и консольный лог не вводили в заблуждение.
            skip_reason = (
                "Heuristics context missing"
                if "heuristics" not in context
                else "ProjectMap missing"
                if project_map is None
                else "No OCPLSP candidates identified"
            )

            # расширенный лог для понимания,
            # какие именно данные были доступны в момент skip.
            logger.info(
                "LLM skipped: reason=%s, classes=%d, interfaces=%d, candidates_count=%d",
                skip_reason,
                classes_count,
                interfaces_count,
                candidates_count,
            )

            results["llm"] = {
                "status": "skipped",
                "reason": skip_reason,
                "findings": [],
            }
        else:
            try:
                # строим LlmConfig из общего JSON-конфига через helper.
                # Здесь же подхватывается модель из config["llm"]["model"], которую можно
                # менять без правки кода (в том числе на разные модели OpenRouter).
                llm_config = load_llm_config(config)

                # формируем вход для LlmSolidAdapter.
                # Предполагается, что heuristics_result уже содержит
                # инстансы ProjectMap и List[LlmCandidate], а не сырые dict.
                llm_input = LlmAnalysisInput(
                    project_map=project_map,
                    candidates=candidates,
                )

                # собираем LLM-стек через фабрику (Gateway + Provider + Cache + Budget)
                llm_adapter = create_llm_adapter(llm_config)

                # запускаем LLM-анализ OCP/LSP-кандидатов
                llm_output = llm_adapter.analyze(llm_input)

                # сохраняем результаты и метаданные в общий словарь пайплайна
                results["llm"] = {
                    "status": "success",
                    "findings": llm_output.findings,
                    "metadata": {
                        "candidates_processed": llm_output.metadata.candidates_processed,
                        "candidates_skipped": llm_output.metadata.candidates_skipped,
                        "tokens_used": llm_output.metadata.tokens_used,
                        "cache_hits": llm_output.metadata.cache_hits,
                        "parse_failures": llm_output.metadata.parse_failures,   # NEW
                        "parse_partials": llm_output.metadata.parse_partials,   # NEW
                        "parse_warnings": llm_output.metadata.parse_warnings,   # NEW
                    },
                }

            except Exception as e:
                # любая непредвиденная ошибка LLM-слоя не должна ломать
                # весь пайплайн — логируем и помечаем статусом "error".
                logger.error("LLM analysis failed: %s", e)
                results["llm"] = {
                    "status": "error",
                    "reason": str(e),
                    "findings": [],
                }
    else:
        logger.info("LLM analysis is disabled in config.")
        results["llm"] = {
            "status": "disabled",
            "findings": [],
        }

    return results