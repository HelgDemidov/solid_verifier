from __future__ import annotations
import pytest
import json
import httpx
from tools.solid_verifier.solid_dashboard.llm.errors import (
    RetryableError,
    NonRetryableError,
)
from tools.solid_verifier.solid_dashboard.llm.provider import (
    OpenAiProvider,
    Message,
    LlmOptions,
)

_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_NON_RETRYABLE_STATUS_CODES = frozenset({400, 401, 403, 404})

@pytest.mark.parametrize(
    "response_text",
    [
        "not-a-json",  # Битый JSON
        json.dumps({"wrong": "schema"}),  # Валидный JSON, но нет ожидаемой структуры (choices)
        json.dumps({"choices": []}),  # Пустой список choices
    ],
    ids=["invalid_json", "wrong_schema", "empty_choices"],
)
def test_openai_provider_raises_non_retryable_error_for_malformed_200_ok(
    response_text: str,
) -> None:
    call_count = 0
    
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(status_code=200, text=response_text)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # Для 200 OK статус-код всё равно 200, но ошибка фатальная (parsing failed)
    assert err.status_code == 200
    
    # Убеждаемся, что запрос был отправлен
    assert call_count == 1
    
    # Можно проверить, что в тексте ошибки есть упоминание парсинга или формата
    msg = str(err).lower()
    assert "parse" in msg or "format" in msg or "invalid" in msg or "json" in msg

@pytest.mark.parametrize("exception_cls", [httpx.ReadTimeout, httpx.ConnectError])
def test_openai_provider_wraps_timeouts_and_connect_errors_in_retryable_error(
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

    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

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
def test_openai_provider_wraps_unexpected_4xx_in_non_retryable_error(status_code: int) -> None:
    # 418 и 422 не входят ни в RETRYABLE_STATUS_CODES, ни в NON_RETRYABLE_STATUS_CODES по спекам,
    # но реализация трактует их как non-retryable "unexpected" статусы.
    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    err = exc_info.value

    # Статус-код прокинут внутрь ошибки
    assert err.status_code == status_code

    # Сообщение содержит статус и маркер "Unexpected" — стабилизируем логи без жёсткой привязки к полной строке
    msg = str(err)
    assert str(status_code) in msg
    assert "Unexpected" in msg

def test_openai_provider_non_retryable_error_message_contains_status_code() -> None:
    status_code = 401

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status_code)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

    with pytest.raises(NonRetryableError) as exc_info:
        provider.chat(messages, options)

    msg = str(exc_info.value)
    # Фиксируем, что в тексте есть статус-код; конкретная формулировка остаётся свободной
    assert str(status_code) in msg

@pytest.mark.parametrize("status_code", sorted(_NON_RETRYABLE_STATUS_CODES))
def test_openai_provider_raises_non_retryable_error_for_client_error_status_codes(
    status_code: int,
) -> None:
    # счётчик вызовов handler и последний запрос
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

    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

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
    # лучше всего — завязаться на реальный endpoint из OpenAiProvider
    # в спецификации chat URL: f"{self.endpoint}/v1/chat/completions"[file:49]
    assert "chat" in str(last_request.url)

@pytest.mark.parametrize("status_code", sorted(_RETRYABLE_STATUS_CODES))
def test_openai_provider_raises_retryable_error_for_retryable_status_codes(status_code: int) -> None:
    call_count = 0              # счётчик вызовов handler
    last_request: httpx.Request | None = None  # последний запрос для последующей проверки

    # handler для MockTransport: эмулируем ответ только со статус-кодом
    def mock_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count, last_request
        call_count += 1         # увеличиваем счётчик — фиксируем факт HTTP-вызова
        last_request = request  # сохраняем запрос для дополнительных assert'ов
        return httpx.Response(status_code=status_code)

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

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
    #    - URL выглядит как OpenAI endpoint для chat/completions (или тот, который задан в OpenAiProvider)
    assert last_request is not None
    assert last_request.method == "POST"
    # если в OpenAiProvider есть атрибут chat_url/endoint_url — лучше завязаться на него
    assert "chat" in str(last_request.url)

def test_openai_provider_parses_success_response() -> None:
    captured_request: dict | None = None

    # handler для MockTransport: эмулируем успешный JSON-ответ OpenAI
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
                "model": "gpt-4o-mini",
            },
        )

    transport = httpx.MockTransport(mock_handler)
    client = httpx.Client(transport=transport)

    provider = OpenAiProvider(api_key="test-key", client=client)

    messages = [Message(role="user", content="test")]
    options = LlmOptions(model="gpt-4o-mini")

    response = provider.chat(messages, options)

    # Проверяем распарсенный ответ
    assert response.content == "analysis result"
    assert isinstance(response.tokens_used, int)
    assert response.tokens_used == 42
    assert response.model == "gpt-4o-mini"

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
    assert body["model"] == "gpt-4o-mini"
    assert isinstance(body["messages"], list)
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][0]["content"] == "test"