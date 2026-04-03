# ---------------------------------------------------------------------------
# LLM Layer Factory & Dependency Injection
# ---------------------------------------------------------------------------
# Фабрика сборки LLM-слоя:
# Изолирует логику создания графа объектов (Provider -> Cache -> Budget -> Gateway -> Adapter) от основного пайплайна
# Принимает конфигурацию (LlmConfig), валидирует ее по принципу fail-fast и возвращает готовый фасад (LlmSolidAdapter) 
# Гарантирует, что ни пайплайн, ни сам адаптер не занимаются инстанцированием инфраструктурных зависимостей


from __future__ import annotations

import logging

from .budget import TokenBudgetController  
from .cache import FileCache               
from .errors import NonRetryableError      
from .gateway import LlmGateway            
from .llm_adapter import LlmSolidAdapter   
from .provider import OpenRouterProvider       
from .types import LlmConfig               

logger = logging.getLogger(__name__)

# Константы для будущих провайдеров
_SUPPORTED_PROVIDERS = frozenset({"openrouter"})


def create_llm_adapter(config: LlmConfig) -> LlmSolidAdapter:
    """
    Публичная точка сборки всего LLM-стека: пайплайн вызывает именно эту функцию и получает готовый адаптер
    Ни пайплайн, ни адаптер не знают о конкретных классах инфраструктуры.

    Цепочка сборки:
        LlmConfig
            → _validate_config(config)          # fail-fast
            → OpenRouterProvider(api_key, endpoint)  # Transport Layer
            → FileCache(cache_dir)               # Кэш
            → TokenBudgetController(max_tokens)  # Бюджет
            → LlmGateway(provider, cache, budget)
            → LlmSolidAdapter(gateway, config)
    """
    _validate_config(config)

    gateway = _create_gateway(config)
    return LlmSolidAdapter(gateway=gateway, config=config)


def _validate_config(config: LlmConfig) -> None:
    """
    Fail-fast валидация LlmConfig при старте.
    (для не-Ollama провайдеров) — это ошибка конфигурации, пайплайн
    должен остановиться немедленно, не ждать первого вызова к API.
    """
    if config.provider not in _SUPPORTED_PROVIDERS:
        raise NonRetryableError(
            message=(
                f"Unsupported LLM provider: '{config.provider}'. "
                f"Supported: {sorted(_SUPPORTED_PROVIDERS)}"
            ),
            status_code=None,
        )

    if not config.api_key:
        raise NonRetryableError(
            message=(
                f"LLM provider '{config.provider}' requires an API key, but it is empty."
                f"Please set OPENROUTER_API_KEY in your .env file or pass it via environment variables."
            ),
            status_code=None,
        )


def _create_gateway(config: LlmConfig) -> LlmGateway:

    # Внутренняя сборка LlmGateway из LlmConfig
    # Не экспортируется наружу — пайплайн использует только create_llm_adapter()
    provider = _create_provider(config)
    cache = FileCache(cache_dir=config.cache_dir)
    budget = TokenBudgetController(max_tokens=config.max_tokens_per_run)

    return LlmGateway(
        provider=provider,
        cache=cache,
        budget=budget,
    )

def _create_provider(config: LlmConfig) -> OpenRouterProvider:

    # Создает провайдер в зависимости от config.provider
    # Единственная точка, где упоминаются конкретные классы провайдеров (сейчас поддерживается только OpenRouter)
    if config.provider == "openrouter":
        return OpenRouterProvider(
            api_key=config.api_key,
            endpoint=config.endpoint,
            # timeout оставляем дефолтным из провайдера:
            # connect=10, read=120, write=10, pool=10
        )

    # Заглушка для будущих провайдеров (не должна достигаться после _validate_config)
    raise NotImplementedError(f"Provider '{config.provider}' not implemented yet")
