from __future__ import annotations

import hashlib        # для SHA256-ключа кэша
import json           # для детерминированной сериализации промпта
import logging
import time

from dataclasses import dataclass, asdict
from typing import Sequence, Optional

from .provider import LlmProvider, Message, LlmOptions
from .interfaces import LlmCache  # протокол кэша для Gateway
from .types import LlmResponse    # контрактный тип ответа LLM

from .budget import TokenBudgetController
from .errors import (
    BudgetExhaustedError,
    LlmUnavailableError,
    RetryableError,
    NonRetryableError,
)

logger = logging.getLogger(__name__)


@dataclass
class LlmGateway:
    """
    Оркестратор LLM-вызовов.

    Отвечает за:
      - кэширование по полному промпту,
      - контроль токен-бюджета,
      - retry-логику для временных ошибок провайдера.

    Не знает ничего про httpx/HTTP — работает только через LlmProvider.
    """

    # Провайдер LLM (OpenRouter), уже защищенный ACL-A
    provider: LlmProvider

    # Опциональный кэш, реализующий протокол LlmCache
    cache: Optional[LlmCache] = None

    # Опциональный контроллер токен-бюджета
    budget: Optional[TokenBudgetController] = None

    # Максимальное количество попыток вызова провайдера (1 основная + ретраи)
    _max_attempts: int = 3

    # Задержки между ретраями (в секундах) для попыток 2, 3 и далее
    _retry_delays: tuple[float, float] = (2.0, 5.0)

    def analyze(
        self,
        messages: Sequence[Message],
        options: LlmOptions,
    ) -> LlmResponse:
        """
        Высокоуровневый вызов LLM с учетом:
        - кэша (если он передан),
        - токен-бюджета (если он передан),
        - retry-логики для временных ошибок провайдера.
        """
        # 0. Строим детерминированный ключ кэша по полному промпту
        cache_key = self._build_cache_key(messages, options)

        # 1. Попытка взять ответ из кэша (если кэш сконфигурирован)
        if self.cache is not None:
            cached = self.cache.get(cache_key)
            if cached is not None:
                # Кэш-хит: возвращаем сохраненный ответ, провайдера не трогаем
                logger.info("LLM cache hit for key=%s", cache_key)
                return cached
            logger.info("LLM cache miss for key=%s", cache_key)

        # 2. Проверка бюджета до первого реального вызова (если бюджет подключен)
        if self.budget is not None and self.budget.is_exhausted():
            logger.info(
                "LLM budget exhausted before call: used %d / %d tokens",
                self.budget.used_tokens,
                self.budget.max_tokens,
            )
            # Fail-fast поведение: дальнейший вызов провайдера нарушил бы бюджет
            raise BudgetExhaustedError(
                used=self.budget.used_tokens,
                limit=self.budget.max_tokens,
            )

        # 3. Вызов провайдера с retry-логикой
        attempt = 0
        last_error: Exception | None = None

        while attempt < self._max_attempts:
            try:
                # Вызов конкретного провайдера (через OpenRouter)
                # ACL-A уже отработал внутри provider.chat (через _parse_success)
                response = self.provider.chat(list(messages), options)

                # 4. Учет токенов (если бюджет подключен)
                if self.budget is not None and response.tokens_used > 0:
                    self.budget.record_tokens(response.tokens_used)

                # 5. Сохранение успешного ответа в кэш (если кэш есть)
                if self.cache is not None:
                    try:
                        self.cache.set(cache_key, response)
                    except Exception as e:
                        # Любые проблемы с кэшем не должны ломать основной сценарий
                        logger.warning(
                            "Failed to write LLM cache for key=%s: %s",
                            cache_key,
                            e,
                        )

                # 6. Возвращаем успешный ответ провайдера
                return response

            except RetryableError as exc:
                # Временная ошибка: увеличиваем счетчик попыток и решаем, ретраить ли
                last_error = exc
                attempt += 1

                if attempt >= self._max_attempts:
                    logger.warning(
                        "LLM provider still failing after %d attempts: %s",
                        attempt,
                        exc,
                    )
                    raise LlmUnavailableError(
                        f"LLM provider unavailable after {attempt} attempts"
                    ) from exc

                # Выбираем задержку между ретраями из таблицы
                delay_index = attempt - 1
                delay = (
                    self._retry_delays[delay_index]
                    if delay_index < len(self._retry_delays)
                    else self._retry_delays[-1]
                )

                logger.warning(
                    "Retryable LLM error on attempt %d/%d, retrying in %.1fs: %s",
                    attempt,
                    self._max_attempts,
                    delay,
                    exc,
                )
                time.sleep(delay)

            except NonRetryableError as exc:
                # Невосстановимая ошибка провайдера: немедленно пробрасываем
                logger.error("Non-retryable LLM error: %s", exc)
                raise

        # Если сюда дошли, значит цикл завершился без успешного ответа
        if last_error is not None:
            raise LlmUnavailableError(
                "LLM provider unavailable after retries"
            ) from last_error

        # Теоретически недостижимая ветка (например, если _max_attempts == 0)
        raise LlmUnavailableError("LLM provider unavailable (no attempts made)")

    def _build_cache_key(
        self,
        messages: Sequence[Message],
        options: LlmOptions,
    ) -> str:
        """
        Строит ключ кэша как SHA256(full_prompt).

        full_prompt = детерминированный JSON:
        - список сообщений (Message → dict через asdict),
        - все публичные поля LlmOptions.
        """
        # Сериализуем сообщения и опции в единый словарь
        payload = {
            "messages": [asdict(m) for m in messages],
            "options": asdict(options),
        }
        # Детерминированная JSON-сериализация (ключи отсортированы)
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        # Вычисляем SHA256 по байтам сериализованного промпта
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()