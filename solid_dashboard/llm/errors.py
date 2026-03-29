"""
Ошибки LLM-провайдера для SOLID-верификатора.

Этот модуль определяет контракт ошибок между HTTP-уровнем (провайдером)
и LlmGateway. Логика классификации ошибок описана в
http_client_tech_spec.md, раздел «Классификация HTTP-ошибок» [file:36].

Правила:
- Провайдер ВСЕГДА преобразует HTTP/сетевые ошибки в один из двух типов:
  RetryableError или NonRetryableError.
- LlmGateway НИКОГДА не анализирует HTTP-статусы напрямую, он работает
  только с этими двумя классами.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class LlmError(Exception):
    """
    Базовый тип ошибок LLM-провайдера.

    Поля:
        message: Человекочитаемое описание ошибки (для логов и отчётов).
        status_code: HTTP-статус (если известен), иначе None.

    Замечание:
        Этот класс сам по себе не используется в логике retry.
        LlmGateway различает только подклассы RetryableError и
        NonRetryableError.
    """
    message: str
    status_code: Optional[int] = None

    def __str__(self) -> str:
        base = self.message
        if self.status_code is not None:
            return f"[status={self.status_code}] {base}"
        return base


@dataclass
class RetryableError(LlmError):
    """
    Ошибка, для которой Gateway может попытаться повторить запрос.

    Примеры (см. http_client_tech_spec.md):
        - Таймауты (TimeoutException, ConnectError).
        - HTTP 429 Too Many Requests.
        - HTTP 5xx (500, 502, 503, 504).

    Поведение LlmGateway:
        - Выполняет до N повторов (по спецификации — ещё 2 попытки)
          с увеличивающимися задержками.
        - Если все попытки исчерпаны, превращает ситуацию в
          LlmUnavailableError (или аналогичный агрегирующий сигнал)
          на своём уровне.
    """
    pass


@dataclass
class NonRetryableError(LlmError):
    """
    Ошибка, при которой повтор запроса смысла не имеет.

    Примеры (см. http_client_tech_spec.md):
        - HTTP 400 Bad Request (ошибка формата запроса).
        - HTTP 401 Unauthorized, 403 Forbidden, 404 Not Found.
        - Любой 2xx-ответ с невалидной структурой JSON
          (неожиданный формат данных от провайдера).

    Поведение LlmGateway:
        - НЕ выполняет повторов.
        - Немедленно фиксирует ошибку и переходит к деградации:
          per-candidate warning, пропуск кандидата, или fail-fast
          в зависимости от настроек деградации.
    """
    pass

@dataclass
class BudgetExhaustedError(LlmError):
    """
    Токен-бюджет на запуск исчерпан.

    Генерируется Gateway до отправки запроса, если:
      - уже достигнут или превышен лимит maxtokensperrun;
      - дальнейший вызов провайдера нарушит бюджет по спецификации.

    status_code здесь обычно None, так как ошибка локальная, не HTTP.
    """

    def __init__(self, message: str = "Token budget exhausted", status_code: Optional[int] = None) -> None:
        super().__init__(message=message, status_code=status_code)


@dataclass
class LlmUnavailableError(LlmError):
    """
    Провайдер так и не смог успешно ответить после нескольких retry.

    Используется Gateway:
      - когда исчерпаны все попытки на RetryableError;
      - когда в результате деградации сервис остаётся недоступным.

    Это «зонтичная» ошибка для внешнего мира: 
    пайплайн видит, что LLM-сервис временно недоступен.
    """

    def __init__(self, message: str = "LLM provider unavailable", status_code: Optional[int] = None) -> None:
        super().__init__(message=message, status_code=status_code)