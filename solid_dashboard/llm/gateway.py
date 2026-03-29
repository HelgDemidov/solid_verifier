from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, asdict
from typing import Sequence

from .budget import TokenBudgetController
from .cache import FileCache
from .errors import (
    BudgetExhaustedError,
    LlmUnavailableError,
    RetryableError,
    NonRetryableError,
)
from .provider import LlmProvider, Message, LlmOptions, LlmResponse

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

    provider: LlmProvider
    cache: FileCache
    budget: TokenBudgetController

    _max_attempts: int = 3
    _retry_delays: tuple[float, float] = (2.0, 5.0)

    def analyze(self, messages: Sequence[Message], options: LlmOptions) -> LlmResponse:
        """
        Высокоуровневый вызов LLM с учётом кэша, бюджета и retry.
        """
        cache_key = self._build_cache_key(messages, options)

        # 1. Попытка взять из кэша
        cached = self.cache.get(cache_key)
        if cached is not None:
            # FileCache уже вернул готовый LlmResponse
            return cached

        # 2. Проверка бюджета до первого реального вызова
        if self.budget.is_exhausted():
            logger.info(
                "LLM budget exhausted before call: max_tokens_per_run reached."
            )
            raise BudgetExhaustedError("Token budget exhausted before LLM call")

        # 3. Вызов провайдера с retry
        attempt = 0
        last_error: Exception | None = None

        while attempt < self._max_attempts:
            try:
                response = self.provider.chat(list(messages), options)

                # 4. Учёт токенов
                if response.tokens_used > 0:
                    self.budget.record_tokens(response.tokens_used)

                # 5. Сохраняем в кэш как LlmResponse
                self.cache.set(cache_key, response)

                return response

            except RetryableError as exc:
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
                logger.error("Non-retryable LLM error: %s", exc)
                raise

        if last_error is not None:
            raise LlmUnavailableError(
                "LLM provider unavailable after retries"
            ) from last_error

        raise LlmUnavailableError("LLM provider unavailable (no attempts made)")

    def _build_cache_key(self, messages: Sequence[Message], options: LlmOptions) -> str:
        """
        Строит ключ кэша как SHA256(full_prompt).

        full_prompt = JSON всего набора messages + model.
        """
        payload = {
            "messages": [asdict(m) for m in messages],
            "model": options.model,
        }
        serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()