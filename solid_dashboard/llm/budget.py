from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class TokenBudgetController:
    """
    Контроллер токен-бюджета.
    Реализует протокол BudgetController для LlmGateway.
    """

    def __init__(self, max_tokens: int) -> None:
        self.max_tokens = max_tokens
        self.used_tokens = 0

    def is_exhausted(self) -> bool:
        """Проверяет, превысил ли расход токенов установленный лимит."""
        if self.max_tokens <= 0:
            return False  # Бюджет не ограничен (например, для локальной Ollama)
            
        is_exhausted = self.used_tokens >= self.max_tokens
        if is_exhausted:
            logger.warning(
                "LLM budget exhausted: used %d / %d tokens",
                self.used_tokens,
                self.max_tokens
            )
        return is_exhausted

    def record_tokens(self, tokens: int) -> None:
        """Регистрирует потраченные токены."""
        if tokens > 0:
            self.used_tokens += tokens
            logger.debug(
                "LLM budget updated: +%d tokens. Total used: %d / %d",
                tokens,
                self.used_tokens,
                self.max_tokens
            )