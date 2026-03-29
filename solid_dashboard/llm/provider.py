"""
Интерфейс LLM-провайдера и типы сообщений.

Определяет:
- Message, LlmOptions, LlmResponse — типы данных для LLM-вызовов
- LlmProvider — Protocol, который реализуют конкретные провайдеры
- OpenAiProvider — реализация для OpenAI-совместимых API

Правило проекта: import httpx допустим ТОЛЬКО в этом файле.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, asdict
from typing import Literal, Protocol, runtime_checkable

import httpx
from .errors import RetryableError, NonRetryableError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Типы данных
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """Одно сообщение в LLM-диалоге."""
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class LlmOptions:
    """Параметры LLM-вызова.

    Значения по умолчанию оптимизированы для анализа кода:
    - temperature=0.2: низкая для детерминированности
    - max_tokens=4096: достаточно для JSON-ответа с findings
    """
    model: str
    temperature: float = 0.2
    max_tokens: int = 4096
    response_format: str | None = None  # "json_object" для OpenAI JSON mode


@dataclass
class LlmResponse:
    """Ответ от LLM-провайдера."""
    content: str            # Текст ответа
    tokens_used: int        # prompt_tokens + completion_tokens
    model: str              # Фактически использованная модель
    raw: dict | None = None # Сырой ответ для отладки


# ---------------------------------------------------------------------------
# Интерфейс провайдера
# ---------------------------------------------------------------------------

@runtime_checkable
class LlmProvider(Protocol):
    """Интерфейс LLM-провайдера.

    Каждый конкретный провайдер (OpenAI, Anthropic, Ollama)
    реализует этот протокол.

    Raises:
        RetryableError: timeout, 429 Too Many Requests, 5xx
        NonRetryableError: 401 Unauthorized, 403 Forbidden, 400 Bad Request
    """

    def chat(self, messages: list[Message], options: LlmOptions) -> LlmResponse:
        """Отправляет сообщения и возвращает ответ."""
        ...


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

_DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1"

_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,   # быстро определить доступность сервера
    read=120.0,     # LLM может «думать» долго (до 2 минут)
    write=10.0,     # отправка запроса быстрая
    pool=10.0,      # ожидание свободного соединения
)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404})


# ---------------------------------------------------------------------------
# OpenAI-совместимый провайдер
# ---------------------------------------------------------------------------

class OpenAiProvider:
    """Провайдер для OpenAI-совместимых API.

    Поддерживает: OpenAI, Azure OpenAI, любой OpenAI-compatible endpoint.
    Для Ollama: endpoint="http://localhost:11434/v1", api_key=None.

    Args:
        api_key: API-ключ. None для локальных провайдеров (Ollama).
        endpoint: Базовый URL API. По умолчанию OpenAI.
        timeout: Настройки таймаутов. По умолчанию оптимизированы для LLM.
        client: Внешний httpx.Client (для тестов через MockTransport).
    """

    def __init__(
        self,
        api_key: str | None,
        endpoint: str | None = None,
        timeout: httpx.Timeout | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self._endpoint = (endpoint or _DEFAULT_OPENAI_ENDPOINT).rstrip("/")
        self._api_key = api_key
        self._timeout = timeout or _DEFAULT_TIMEOUT

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        self._client = client or httpx.Client(
            timeout=self._timeout,
            headers=headers,
        )
        self._owns_client = client is None

    def chat(self, messages: list[Message], options: LlmOptions) -> LlmResponse:
            """
            Вызывает OpenAI Chat Completions API и возвращает LlmResponse.

            Всегда навешивает заголовок Authorization (если api_key задан) поверх клиента,
            независимо от того, был ли клиент передан извне или создан внутри.
            """
            url = f"{self._endpoint}/v1/chat/completions"

            body = {
                "model": options.model,
                "messages": [asdict(m) for m in messages],  # или эквивалентная сериализация
            }

            # Базовый набор заголовков на уровне запроса
            headers: dict[str, str] = {
                "Content-Type": "application/json",
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            try:
                resp = self._client.post(url, json=body, timeout=self._timeout, headers=headers)
            except httpx.TimeoutException as exc:
                raise RetryableError("LLM request timed out") from exc
            except httpx.HTTPError as exc:
                raise RetryableError("LLM request failed due to network error") from exc

            # Разбор статус-кода в Retryable/NonRetryableError по спекам
            if resp.status_code in _RETRYABLE_STATUS_CODES:
                raise RetryableError(
                    f"Transient HTTP error from OpenAI: {resp.status_code}",
                    status_code=resp.status_code,
                )
            if resp.status_code in _NON_RETRYABLE_STATUS_CODES:
                raise NonRetryableError(
                    f"Non-retryable HTTP error from OpenAI: {resp.status_code}",
                    status_code=resp.status_code,
                )
            if resp.status_code != 200:
                # Любой неожиданный статус тоже считаем нефатальным для retry
                raise NonRetryableError(
                    f"Unexpected HTTP status from OpenAI: {resp.status_code}",
                    status_code=resp.status_code,
                )

            try:
                data = resp.json()
            except ValueError as exc:
                # 200, но тело не JSON — это проблема провайдера, retry не поможет
                raise NonRetryableError("Invalid JSON in OpenAI response") from exc

            try:
                content = data["choices"][0]["message"]["content"]
                tokens_used = int(data.get("usage", {}).get("total_tokens", 0))
                model = data.get("model")
            except (KeyError, TypeError, ValueError) as exc:
                # 200, но структура не соответствует ожиданиям
                raise NonRetryableError("Malformed OpenAI response payload") from exc

            return LlmResponse(
                content=content,
                tokens_used=tokens_used,
                model=model,
                raw=data,
            )

    def close(self) -> None:
        """Закрывает HTTP-клиент, если он был создан провайдером."""
        if self._owns_client:
            self._client.close()

    # --- Формирование запроса ------------------------------------------------

    @staticmethod
    def _build_payload(messages: list[Message], options: LlmOptions) -> dict:
        """Формирует JSON-тело запроса для OpenAI Chat Completions API."""
        payload: dict = {
            "model": options.model,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
        }

        if options.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}

        return payload

    # --- Обработка ответа ----------------------------------------------------

    def _handle_response(self, http_response: httpx.Response) -> LlmResponse:
        """Обрабатывает HTTP-ответ: извлекает данные или бросает ошибку."""
        status = http_response.status_code

        if status in _RETRYABLE_STATUS_CODES:
            self._raise_retryable(http_response)

        if status in _NON_RETRYABLE_STATUS_CODES or status >= 400:
            self._raise_non_retryable(http_response)

        return self._parse_success(http_response.json())

    @staticmethod
    def _parse_success(data: dict) -> LlmResponse:
        """Извлекает content, tokens, model из успешного ответа."""
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise NonRetryableError(
                message=f"Unexpected response structure: {exc}. "
                        f"Keys present: {list(data.keys()) if isinstance(data, dict) else type(data)}",
                status_code=200,
            ) from exc

        usage = data.get("usage", {})
        tokens_used = usage.get("total_tokens", 0)
        model = data.get("model", "unknown")

        return LlmResponse(
            content=content or "",
            tokens_used=tokens_used,
            model=model,
            raw=data,
        )

    # --- Генерация ошибок ----------------------------------------------------

    @staticmethod
    def _raise_retryable(resp: httpx.Response) -> None:
        """Формирует RetryableError из HTTP-ответа."""
        body = resp.text[:500]
        raise RetryableError(
            message=f"HTTP {resp.status_code}: {body}",
            status_code=resp.status_code,
        )

    @staticmethod
    def _raise_non_retryable(resp: httpx.Response) -> None:
        """Формирует NonRetryableError из HTTP-ответа."""
        body = resp.text[:500]
        raise NonRetryableError(
            message=f"HTTP {resp.status_code}: {body}",
            status_code=resp.status_code,
        )