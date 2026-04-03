from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TokenBudgetController:
    """
    Контроллер токен-бюджета для LLM.

    Реализует протокол BudgetController, используемый в LlmGateway:
    - хранит максимальный лимит токенов на один запуск CLI,
    - накапливает фактически использованные токены,
    - сообщает, исчерпан ли бюджет.

    Важный момент: сам контроллер НЕ бросает исключений.
    Решение о том, когда поднять BudgetExhaustedError, принимает LlmGateway.
    """

    def __init__(self, max_tokens: int) -> None:
        # Максимальное количество токенов на один запуск (из LlmConfig.max_tokens_per_run)
        self.max_tokens = max_tokens
        # Счетчик уже использованных токенов за текущий запуск
        self.used_tokens = 0

    def is_exhausted(self) -> bool:
        """
        Проверяет, превысил ли расход токенов установленный лимит.

        Контракт:
        - если max_tokens <= 0, бюджет считается неограниченным
          (например, для локального провайдера вроде Ollama);
        - при достижении/превышении лимита логируется warning и возвращается True.
        """
        if self.max_tokens <= 0:
            # Неограниченный бюджет: всегда считаем, что лимит не исчерпан
            return False

        is_exhausted = self.used_tokens >= self.max_tokens
        if is_exhausted:
            logger.warning(
                "LLM budget exhausted: used %d / %d tokens",
                self.used_tokens,
                self.max_tokens,
            )
        return is_exhausted

    def record_tokens(self, tokens: int) -> None:
        """
        Регистрирует потраченные токены.

        Контракт:
        - отрицательные и нулевые значения игнорируются;
        - контроллер только обновляет счетчик и логирует debug-сообщение,
          не поднимая исключений.
        """
        if tokens > 0:
            self.used_tokens += tokens
            logger.debug(
                "LLM budget updated: +%d tokens. Total used: %d / %d",
                tokens,
                self.used_tokens,
                self.max_tokens,
            )