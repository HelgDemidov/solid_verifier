from __future__ import annotations

import logging

from .budget import TokenBudgetController  # noqa: F401
from .cache import FileCache               # noqa: F401
from .errors import NonRetryableError      # noqa: F401
from .gateway import LlmGateway            # noqa: F401
from .llm_adapter import LlmSolidAdapter   # noqa: F401
from .provider import OpenAiProvider       # noqa: F401
from .types import LlmConfig               # noqa: F401

logger = logging.getLogger(__name__)

# Константы для будущих провайдеров
_SUPPORTED_PROVIDERS = frozenset({"openai"})


def create_llm_adapter(config: LlmConfig) -> LlmSolidAdapter:
    """
    Публичная точка сборки всего LLM-стека.

    Пайплайн вызывает именно эту функцию и получает готовый адаптер.
    Ни пайплайн, ни адаптер не знают о конкретных классах инфраструктуры.

    Цепочка сборки:
        LlmConfig
            → _validate_config(config)          # fail-fast
            → OpenAiProvider(api_key, endpoint)  # Transport Layer
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

    По v13_FINAL таблица деградации: если LLM enabled и нет api_key
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

    # Ollama работает без ключа — для неё api_key необязателен
    _requires_api_key = config.provider != "ollama"
    if _requires_api_key and not config.api_key:
        raise NonRetryableError(
            message=(
                f"LLM provider '{config.provider}' requires api_key, "
                "but it is not configured. "
                "Set apiKey in .solid-analyzer.yml or via environment variable."
            ),
            status_code=None,
        )


def _create_gateway(config: LlmConfig) -> LlmGateway:
    """
    Внутренняя сборка LlmGateway из LlmConfig.

    Не экспортируется наружу — пайплайн использует только create_llm_adapter().
    """
    provider = _create_provider(config)
    cache = FileCache(cache_dir=config.cache_dir)
    budget = TokenBudgetController(max_tokens=config.max_tokens_per_run)

    return LlmGateway(
        provider=provider,
        cache=cache,
        budget=budget,
    )


def _create_provider(config: LlmConfig) -> OpenAiProvider:
    """
    Создаёт провайдер в зависимости от config.provider.

    Сейчас поддерживается только OpenAI; Ollama/Anthropic — Шаг 7.
    Единственная точка, где упоминаются конкретные классы провайдеров.
    """
    if config.provider == "openai":
        return OpenAiProvider(
            api_key=config.api_key,
            endpoint=config.endpoint,
            # timeout оставляем дефолтным из провайдера:
            # connect=10, read=120, write=10, pool=10
        )

    # Заглушка для будущих провайдеров (не должна достигаться после _validate_config)
    raise NotImplementedError(f"Provider '{config.provider}' not implemented yet")