from __future__ import annotations
import pytest
import json
import httpx
import logging

from solid_dashboard.llm.types import LlmResponse
from solid_dashboard.llm.provider import (
    OpenRouterProvider,
    Message,
    LlmOptions,
)

# Импорты типов и ошибок из текущей структуры проекта
from solid_dashboard.llm.errors import (
    RetryableError,
    NonRetryableError,
)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404})

@pytest.mark.parametrize(
    "response_text, expected_message_substring",
    [
        # Битый JSON
        ("not-a-json", "Invalid JSON in OpenRouter response"),
        # Валидный JSON, но нет ожидаемой структуры (нет choices)
        (json.dumps({"wrong": "schema"}), "Missing or invalid 'choices'"),
        # Пустой список choices
        (json.dumps({"choices": []}), "Empty choices array"),
    ],
    ids=["invalid_json", "wrong_schema", "empty_choices"],
)
def test_openrouter_provider_raises_non_retryable_error_for_malformed_200_ok(
    response_text: str,
    expected_message_substring: str,
) -> None:
    """200 OK, но битый/невалидный payload всегда дает NonRetryableError без status_code.

    Контракт:
    - Исключение всегда NonRetryableError (retry не помогает).
    - status_code = None, т.к. проблема не в HTTP-уровне, а в структуре ответа.
    - Сообщение ошибки содержит указание на конкретный тип проблемы.
    """
    call_count = 0

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(status_code=200, text=response_text)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # Важно: проблема на уровне payload-а, а не HTTP-кода,
    # поэтому статус-код не протаскиваем в NonRetryableError.
    assert err.status_code is None

    # Сообщение должно явно указывать на тип проблемы
    msg = str(err)
    assert expected_message_substring in msg
    # sanity-check, что handler вообще вызывался
    assert call_count == 1

@pytest.mark.parametrize("exception_cls", [httpx.ReadTimeout, httpx.ConnectError])
def test_openrouter_provider_wraps_timeouts_and_connect_errors_in_retryable_error(
    exception_cls: type[Exception],
) -> None:
    call_count = 0
    last_request: httpx.Request | None = None

    # Handler, который вместо ответа бросает исключение httpx
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, last_request
        call_count += 1
        last_request = request
        raise exception_cls("simulated network error")

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    with pytest.raises(RetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # 1) Это именно RetryableError, а не httpx-исключение
    assert isinstance(err, RetryableError)

    # 2) Для сетевых ошибок статус-код отсутствует
    assert err.status_code is None

    # 3) HTTP-вызов действительно был: handler вызван ровно один раз
    assert call_count == 1
    assert last_request is not None

    # 4) Базовый контракт запроса: POST на chat-completions endpoint
    assert last_request.method == "POST"
    assert "chat" in str(last_request.url)

@pytest.mark.parametrize("status_code", [418, 422])
def test_openrouter_provider_wraps_unexpected_4xx_in_non_retryable_error(status_code: int) -> None:
    # 418 и 422 не входят ни в RETRYABLE_STATUS_CODES, ни в NON_RETRYABLE_STATUS_CODES по спекам,
    # но реализация трактует их как non-retryable "unexpected" статусы.
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # Статус-код прокинут внутрь ошибки
    assert err.status_code == status_code

    # Сообщение содержит статус и маркер "Unexpected" — стабилизируем логи без жесткой привязки к полной строке
    msg = str(err)
    assert str(status_code) in msg
    assert "Unexpected" in msg

def test_openrouter_provider_non_retryable_error_message_contains_status_code() -> None:
    status_code = 401

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    msg = str(exc_info.value)
    # Фиксируем, что в тексте есть статус-код; конкретная формулировка остается свободной
    assert str(status_code) in msg

@pytest.mark.parametrize("status_code", sorted(_NON_RETRYABLE_STATUS_CODES))
def test_openrouter_provider_raises_non_retryable_error_for_client_error_status_codes(
    status_code: int,
) -> None:
    # счетчик вызовов handler и последний запрос
    call_count = 0
    last_request: httpx.Request | None = None

    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, last_request
        call_count += 1
        last_request = request
        # эмулируем «пустой» ответ с нужным статусом
        return httpx.Response(status_code=status_code)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    # 1) для 4xx должен лететь NonRetryableError, а не RetryableError
    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # 2) статус-код должен быть прокинут внутрь NonRetryableError
    assert err.status_code == status_code
    assert isinstance(err, NonRetryableError)
    # на всякий случай убеждаемся, что это не RetryableError
    assert not isinstance(err, RetryableError)

    # 3) HTTP-запрос действительно был отправлен ровно один раз
    assert call_count == 1

    # 4) базовые свойства запроса: POST и корректный endpoint
    assert last_request is not None
    assert last_request.method == "POST"
    # лучше всего — завязаться на реальный endpoint из OpenRouterProvider
    # в спецификации chat URL: f"{self.endpoint}/v1/chat/completions"[file:49]
    assert "chat" in str(last_request.url)

@pytest.mark.parametrize("status_code", sorted(_RETRYABLE_STATUS_CODES))
def test_openrouter_provider_raises_retryable_error_for_retryable_status_codes(status_code: int) -> None:
    call_count = 0              # счетчик вызовов handler
    last_request: httpx.Request | None = None  # последний запрос для последующей проверки

    # handler для MockTransport: эмулируем ответ только со статус-кодом
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, last_request
        call_count += 1         # увеличиваем счетчик — фиксируем факт HTTP-вызова
        last_request = request  # сохраняем запрос для дополнительных assert'ов
        return httpx.Response(status_code=status_code)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    with pytest.raises(RetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # 1) Проверяем, что статус-код прокинут внутрь RetryableError
    assert err.status_code == status_code
    assert isinstance(err, RetryableError)

    # 2) Проверяем, что HTTP-запрос действительно был отправлен ровно один раз
    assert call_count == 1

    # 3) Проверяем базовые свойства запроса:
    #    - метод POST (ожидаем JSON-чат-запрос)
    #    - URL выглядит как OpenRouter endpoint для chat/completions (или тот, который задан в OpenRouterProvider)
    assert last_request is not None
    assert last_request.method == "POST"
    # если в OpenRouterProvider есть атрибут chat_url/endoint_url — лучше завязаться на него
    assert "chat" in str(last_request.url)

def test_openrouter_provider_parses_success_response() -> None:
    captured_request: dict | None = None

    # handler для MockTransport: эмулируем успешный JSON-ответ OpenRouter
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        # Сохраняем запрос для последующих проверок
        captured_request = {
            "method": request.method,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": request.content,
        }

        return httpx.Response(
            status_code=200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "analysis result",
                        }
                    }
                ],
                "usage": {
                    "total_tokens": 42,
                },
                "model": "openai/gpt-4o-mini",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenRouterProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="openai/gpt-4o-mini")

    response = provider.chat(messages, options)

    # Проверяем распарсенный ответ
    assert response.content == "analysis result"
    assert isinstance(response.tokens_used, int)
    assert response.tokens_used == 42
    assert response.model == "openai/gpt-4o-mini"

    # Проверяем, что запрос вообще был отправлен и захвачен
    assert captured_request is not None
    assert captured_request["method"] == "POST"

    # Проверка URL (путь должен заканчиваться на /v1/chat/completions)
    assert "/v1/chat/completions" in captured_request["url"]

    # Проверка заголовков
    headers = captured_request["headers"]
    assert headers.get("content-type", "").startswith("application/json")
    assert headers.get("authorization") == "Bearer test-key"

    # Проверка тела запроса
    body = json.loads(captured_request["body"].decode("utf-8"))
    assert body["model"] == "openai/gpt-4o-mini"
    assert isinstance(body["messages"], list)
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "test"

# Набор вспомогательных данных для тестов
VALID_BASE = {
    "choices": [{"message": {"content": "test content"}, "finish_reason": "stop"}],
    "usage": {"total_tokens": 42},
    "model": "openai/gpt-4o-mini"
}

def test_parse_success_valid():
    """Тест кейс 1: Полный валидный JSON корректно конвертируется в LlmResponse."""
    provider = OpenRouterProvider(api_key="test")
    resp = provider._parse_success(VALID_BASE)
    assert resp.content == "test content"
    assert resp.tokens_used == 42
    assert resp.model == "openai/gpt-4o-mini"

@pytest.mark.parametrize("payload, expected_err", [
    # Кейс 2: HTTP 200 + {"error": {...}} (обход прокси)
    ({"error": {"message": "rate limit"}}, "API error in response body"),
    
    # Кейс 3: Пустой choices (тот самый баг с IndexError, из-за которого мы все это затеяли)
    ({"choices": []}, "Empty choices array"),
    
    # Кейс 4: finish_reason="content_filter" без контента
    ({"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]}, "Content filtered"),
    
    # Кейс 5: finish_reason="content_filter" с контентом (все равно падаем)
    ({"choices": [{"message": {"content": "partial"}, "finish_reason": "content_filter"}]}, "Content filtered"),
    
    # Кейс 7: finish_reason="tool_calls"
    ({"choices": [{"message": {"tool_calls": []}, "finish_reason": "tool_calls"}]}, "Unexpected finish_reason: tool_calls"),
    
    # Кейс 11: отсутствие finish_reason и пустой контент
    ({"choices": [{"message": {"content": ""}}]}, "Empty response, no finish_reason"),
    
    # Кейс 12: полностью невалидная структура (нет choices)
    ({"foo": "bar"}, "Missing or invalid 'choices'"),
])
def test_parse_success_raises_non_retryable(payload, expected_err):
    """
    Тестирование матрицы аномалий.
    Ожидаем, что любые нарушения структуры вызовут строго NonRetryableError.
    """
    provider = OpenRouterProvider(api_key="test")
    with pytest.raises(NonRetryableError) as exc_info:
        provider._parse_success(payload)
    assert expected_err in str(exc_info.value)

def test_parse_success_warnings(caplog):
    """
    Тестирование матрицы предупреждений.
    Кейсы, где ответ принимается, но логируется warning (length или unknown reason).
    """
    provider = OpenRouterProvider(api_key="test")
    
    # Кейс 6: finish_reason="length"
    payload_length = {"choices": [{"message": {"content": "trunc"}, "finish_reason": "length"}]}
    with caplog.at_level(logging.WARNING):
        resp = provider._parse_success(payload_length)
    assert resp.content == "trunc"
    assert "truncated" in caplog.text

    caplog.clear()

    # Неизвестный finish_reason с контентом
    payload_unknown = {"choices": [{"message": {"content": "data"}, "finish_reason": "alien_reason"}]}
    with caplog.at_level(logging.WARNING):
        resp = provider._parse_success(payload_unknown)
    assert resp.content == "data"
    assert "Unknown finish_reason: alien_reason" in caplog.text

def test_parse_success_defaults():
    """
    Тестирование fallback-значений при частичном, но валидном ответе.
    Кейс 8 (null content), Кейс 9 (отсутствие usage), Кейс 10 (нет finish_reason, но есть content).
    """
    provider = OpenRouterProvider(api_key="test")
    
    # null content превращается в пустую строку
    payload_null_content = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}
    resp = provider._parse_success(payload_null_content)
    assert resp.content == ""

    # Отсутствие usage (Оллама) -> tokens_used = 0
    payload_no_usage = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
    resp = provider._parse_success(payload_no_usage)
    assert resp.tokens_used == 0

    # Нет finish_reason, но контент есть (успех)
    payload_no_reason = {"choices": [{"message": {"content": "ok"}}]}
    resp = provider._parse_success(payload_no_reason)
    assert resp.content == "ok"

@pytest.mark.parametrize("evil_payload", [
    [],                      # Не dict
    "string",                # Не dict
    {"choices": [[]]},       # choice не dict
    {"choices": [{}]},       # choice пуст
    {"choices": [{"message": []}]}, # message не dict
])
def test_no_keyerror_ever(evil_payload):
    """
    Тест-контракт: алгоритм Safe Extraction не должен пропускать сырые Python-исключения.
    KeyError, IndexError, TypeError никогда не должны вылетать.
    """
    provider = OpenRouterProvider(api_key="test")
    with pytest.raises(NonRetryableError):
        provider._parse_success(evil_payload)