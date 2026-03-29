# tools/solid_verifier/solid_dashboard/pipeline.py

import logging
from typing import Dict, Any, List
from .interfaces.analyzer import IAnalyzer

# Импорты для LLM
from tools.solid_verifier.solid_dashboard.llm.factory import create_llm_adapter
from tools.solid_verifier.solid_dashboard.llm.types import (
    LlmAnalysisInput, 
    LlmConfig, 
    ProjectMap, 
    LlmCandidate
)

logger = logging.getLogger(__name__)

def run_pipeline(
    target_dir: str,
    config: Dict[str, Any],
    adapters: List[IAnalyzer],
) -> Dict[str, Any]:
    """
    Запускает пайплайн анализа: сначала статические адаптеры, затем опционально LLM.
    """
    # context хранит результаты предыдущих адаптеров
    context: Dict[str, Any] = {}
    results: Dict[str, Any] = {}

    # 1. Запуск статических адаптеров (включая Шаг 0 и Шаг 1a/1b)
    for adapter in adapters:
        logger.info(f"Running static adapter: {adapter.name}")
        result = adapter.run(target_dir, context, config)
        results[adapter.name] = result
        context[adapter.name] = result

    # 2. Интеграция LLM (Шаг 2 из архитектуры v13_FINAL)
    # Проверяем, включен ли LLM в конфигурации (по умолчанию выключен)
    llm_config_dict = config.get("llm", {})
    is_llm_enabled = llm_config_dict.get("enabled", False)
    
    if is_llm_enabled:
        logger.info("LLM analysis is enabled. Initializing LLM layer...")
        
        # Извлекаем ProjectMap и кандидатов из контекста статических адаптеров.
        # Ожидается, что адаптер, реализующий эвристики (например, HeuristicsAdapter),
        # сохранил их в контекст.
        # Если их пока нет, ставим заглушки, чтобы не ломать пайплайн.
        heuristics_result = context.get("heuristics", {})
        
        project_map_data = heuristics_result.get("project_map")
        candidates_data = heuristics_result.get("candidates", [])
        
        if not project_map_data or not candidates_data:
            logger.info("No ProjectMap or candidates found for LLM analysis. Skipping LLM step.")
            results["llm"] = {
                "status": "skipped", 
                "reason": "No OCPLSP candidates identified",
                "findings": []
            }
        else:
            try:
                # Конвертируем dict из конфига в датакласс LlmConfig
                llm_config = LlmConfig(
                    provider=llm_config_dict.get("provider", "openai"),
                    model=llm_config_dict.get("model", "gpt-4o-mini"),
                    api_key=llm_config_dict.get("api_key", ""),
                    endpoint=llm_config_dict.get("endpoint"),
                    max_tokens_per_run=llm_config_dict.get("max_tokens_per_run", 50000),
                    cache_dir=llm_config_dict.get("cache_dir", ".solid-cache/llm"),
                    prompts_dir=llm_config_dict.get("prompts_dir", "prompts")
                )
                
                # Создаем Input
                llm_input = LlmAnalysisInput(
                    project_map=project_map_data, # Ожидается инстанс ProjectMap
                    candidates=candidates_data,   # Ожидается List[LlmCandidate]
                    config=llm_config
                )
                
                # Собираем LLM стек через фабрику (Dependency Injection)
                llm_adapter = create_llm_adapter(llm_config)
                
                # Запускаем анализ
                llm_output = llm_adapter.analyze(llm_input)
                
                # Сохраняем результаты в общий словарь
                results["llm"] = {
                    "status": "success",
                    "findings": llm_output.findings,
                    "metadata": llm_output.metadata
                }
                
            except Exception as e:
                logger.error(f"LLM analysis failed: {e}")
                results["llm"] = {
                    "status": "error",
                    "reason": str(e),
                    "findings": []
                }
    else:
        logger.info("LLM analysis is disabled in config.")
        results["llm"] = {
            "status": "disabled",
            "findings": []
        }

    return results