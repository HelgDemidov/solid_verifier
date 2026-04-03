"""
Интерфейс LLM-провайдера и типы сообщений.

Определяет:
- Message, LlmOptions, LlmResponse — типы данных для LLM-вызовов
- LlmProvider — Protocol, который реализуют конкретные провайдеры
- OpenRouterProvider — реализация для OpenRouter API

Правило проекта: import httpx допустим ТОЛЬКО в этом файле.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import httpx

from .types import LlmResponse
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
    response_format: str | None = None  # "json_object" для OpenRouter JSON mode


# ---------------------------------------------------------------------------
# Интерфейс провайдера
# ---------------------------------------------------------------------------


@runtime_checkable
class LlmProvider(Protocol):
    """Интерфейс LLM-провайдера.

    Каждый конкретный провайдер, доступный через OpenRouter API,
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

# базовый URL OpenRouter Chat Completions API.
# ВНИМАНИЕ: здесь мы указываем ПОЛНЫЙ путь, без последующего дописывания /v1/chat/completions.
_DEFAULT_OPENROUTER_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"

_DEFAULT_TIMEOUT = httpx.Timeout(
    connect=10.0,  # быстро определить доступность сервера
    read=120.0,  # LLM может «думать» долго (до 2 минут)
    write=10.0,  # отправка запроса быстрая
    pool=10.0,  # ожидание свободного соединения
)

_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404})

# ---------------------------------------------------------------------------
# OpenRouter API-совместимый провайдер
# ---------------------------------------------------------------------------


class OpenRouterProvider:
    def __init__(
        self,
        api_key: str | None,
        endpoint: str | None = None,
        timeout: httpx.Timeout | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        """
        Инициализация провайдера.

        :param api_key: API ключ OpenRouter.
        :param endpoint: ПОЛНЫЙ URL Chat Completions API.
                         Если None, берется _DEFAULT_OPENROUTER_ENDPOINT.
        :param timeout: Настройки таймаутов (если None, берется _DEFAULT_TIMEOUT).
        :param client: Кастомный httpx.Client (если None, создается внутренний).
        """

        # приводим endpoint к аккуратному виду без лишнего слеша
        self._endpoint = (endpoint or _DEFAULT_OPENROUTER_ENDPOINT).rstrip("/")
        self._api_key = api_key or ""
        self._timeout = timeout or _DEFAULT_TIMEOUT

        # формируем заголовки, как требует OpenRouter
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # если клиент не передан, создаем свой и помечаем флаг владения
        self._client = client or httpx.Client(
            timeout=self._timeout,
            headers=headers,
        )
        self._owns_client = client is None

        logger.info(
            "OpenRouterProvider initialized with endpoint=%s, has_api_key=%s",
            self._endpoint,
            bool(self._api_key),
        )

    # Этот метод реализует HTTP-вызов и классификацию ошибок,
    # делегируя весь разбор JSON в ACL-A барьер _parse_success().
    def chat(self, messages: list[Message], options: LlmOptions) -> LlmResponse:
        """
        Вызывает OpenRouter API и возвращает LlmResponse.

        Отвечает только за формирование запроса, HTTP-вызов и классификацию
        статус-кодов в Retryable/NonRetryable ошибки. Парсинг успешного JSON
        делегируется в _parse_success (ACL-A барьер).
        """

        # используем endpoint как ПОЛНЫЙ URL, ничего не дописываем
        url = self._endpoint

        payload = self._build_payload(messages, options)

        # формируем заголовки на каждый запрос, чтобы можно было
        # при необходимости подменять api_key/trace-id и т.п.
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # подробное логирование запроса для диагностики 404/4xx/5xx
        logger.info("OpenRouter request: POST %s", url)
        logger.debug("OpenRouter request headers: %s", headers)
        logger.debug("OpenRouter request payload: %s", payload)

        try:
            http_response = self._client.post(
                url,
                json=payload,
                timeout=self._timeout,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            # таймаут считаем временной ошибкой, допускающей retry
            raise RetryableError("LLM request timed out") from exc
        except httpx.HTTPError as exc:
            # сетевые ошибки (DNS, connection reset и т.п.) тоже retryable
            raise RetryableError("LLM request failed due to network error") from exc

        logger.info(
            "OpenRouter response: status=%s, length=%s",
            http_response.status_code,
            len(http_response.text),
        )
        logger.debug("OpenRouter response body (truncated): %s", http_response.text[:1000])

        # дальше используем единый путь обработки статус-кодов и JSON
        return self._handle_response(http_response)

    def close(self) -> None:
        """Закрывает HTTP-клиент, если он был создан провайдером."""
        if self._owns_client:
            self._client.close()

    # --- Формирование запроса ------------------------------------------------

    @staticmethod
    def _build_payload(messages: list[Message], options: LlmOptions) -> dict:
        """Формирует JSON-тело запроса для OpenRouter API."""
        # базовый payload соответствует схеме OpenAI/ChatCompletions
        payload: dict = {
            "model": options.model,
            "messages": [
                {"role": m.role, "content": m.content} for m in messages
            ],
            "temperature": options.temperature,
            "max_tokens": options.max_tokens,
        }

        # при необходимости включаем JSON mode OpenRouter
        if options.response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}

        return payload

    # --- Обработка ответа ----------------------------------------------------

    def _handle_response(self, http_response: httpx.Response) -> LlmResponse:
        """Обрабатывает HTTP-ответ: извлекает данные или бросает ошибку."""
        status = http_response.status_code

        # 408, 429, 5xx из явного списка считаем временными — можно ретраить
        if status in _RETRYABLE_STATUS_CODES:
            self._raise_retryable(http_response)

        # 400, 401, 403, 404 — ожидаемые client errors, сразу non-retryable
        if status in _NON_RETRYABLE_STATUS_CODES:
            self._raise_non_retryable(http_response)

        # любой другой 4xx (например, 418, 422) считаем non-retryable,
        # но помечаем как "Unexpected" в сообщении для стабилизации логов и тестов.
        if 400 <= status < 500:
            body = http_response.text[:500]
            raise NonRetryableError(
                message=f"Unexpected HTTP status from OpenRouter: {status} ({body})",
                status_code=status,
            )

        # любой 5xx, который НЕ попал в RETRYABLE_STATUS_CODES
        # (на практике едва ли, но на всякий случай) считаем non-retryable "unexpected".
        if status >= 500:
            body = http_response.text[:500]
            raise NonRetryableError(
                message=f"Unexpected HTTP status from OpenRouter: {status} ({body})",
                status_code=status,
            )

        # здесь гарантированно 2xx, пробуем безопасно распарсить JSON
        try:
            data = http_response.json()
        except ValueError as exc:
            # 2xx, но тело не JSON — это проблема провайдера, retry не поможет
            raise NonRetryableError("Invalid JSON in OpenRouter response") from exc

        return self._parse_success(data)

    def _parse_success(self, data: dict) -> LlmResponse:
        """
        Транспортный барьер (ACL-A) для безопасного извлечения данных из ответа OpenRouter.
        Алгоритм 9 шагов гарантирует:
        1. Ни KeyError, ни IndexError не покинут этот метод (Safe Extraction).
        2. Возвращается строго LlmResponse(frozen=True) или летит NonRetryableError.
        3. Сырой JSON (dict) не пересекает границу домена.
        """
        # Шаг 1: проверить, что data — dict
        if not isinstance(data, dict):
            raise NonRetryableError("Response is not a JSON object")

        # Шаг 2: проверить наличие "error" в body (обход некоторых HTTP-прокси, отдающих 200)
        if "error" in data:
            msg = data.get("error", {}).get("message", "Unknown API error")
            raise NonRetryableError(f"API error in response body: {msg}")

        # Шаг 3: извлечь choices
        choices = data.get("choices")
        if not isinstance(choices, list):
            raise NonRetryableError("Missing or invalid 'choices'")
        if len(choices) == 0:
            raise NonRetryableError("Empty choices array")

        # Шаг 4: извлечь первый choice (без IndexError)
        choice = choices[0]
        if not isinstance(choice, dict):
            raise NonRetryableError("Invalid choice format")

        # Шаг 5: обработать finish_reason по матрице (content_filter, tool_calls недопустимы)
        finish_reason = choice.get("finish_reason")
        if finish_reason == "content_filter":
            raise NonRetryableError("Content filtered by provider")
        if finish_reason == "tool_calls":
            raise NonRetryableError("Unexpected finish_reason: tool_calls")

        # Шаг 6: безопасно извлечь content (без KeyError и без AttributeError)
        message = choice.get("message")
        if not isinstance(message, dict):
            # Невалидный формат message → считаем ответ невалидным
            raise NonRetryableError("Invalid 'message' format in choice")

        content = message.get("content")
        content = content if isinstance(content, str) else ""

        # Шаг 7: применить логику finish_reason × content
        if finish_reason == "length":
            logger.warning("LLM response may be truncated (finish_reason=length)")
        elif finish_reason not in ("stop", "length", None):
            if content:
                logger.warning("Unknown finish_reason: %s", finish_reason)
            else:
                raise NonRetryableError(
                    f"Empty response, finish_reason: {finish_reason}"
                )
        elif finish_reason is None and not content:
            raise NonRetryableError("Empty response, no finish_reason")

        # Шаг 8: извлечь tokens_used (usage опционален, может отсутствовать у локальных моделей)
        tokens_used = data.get("usage", {}).get("total_tokens", 0)
        if not isinstance(tokens_used, int):
            tokens_used = 0

        # Шаг 9: извлечь model
        model = data.get("model", "unknown")
        if not isinstance(model, str):
            model = "unknown"

        # Возвращаем иммутабельный контракт
        return LlmResponse(content=content, tokens_used=tokens_used, model=model)

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