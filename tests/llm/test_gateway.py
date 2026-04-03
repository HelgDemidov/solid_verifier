# ---------------------------------------------------------------------------
# Интеграционные и юнит-тесты LlmGateway (Транспортный слой / ACL-A)
#
# Проверяют механизмы взаимодействия с LLM-провайдером (OpenRouter/Ollama):
# 1. Защиту от нестабильности (retry-логика при временных HTTP/API ошибках).
# 2. Механизм кэширования (hit/miss, детерминированная генерация ключей по промптам).
# 3. Контроль токен-бюджета (учет потраченных токенов, Fail-fast при исчерпании).
# 4. Транспортный ACL-A (Gateway работает с LlmResponse, а не сырыми dict).
#
# Файл большой и может потребовать рефакторинга/разделения на:
# test_gateway_cache.py, test_gateway_budget.py, test_gateway_retries.py.
# ---------------------------------------------------------------------------

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pytest

from solid_dashboard.llm.gateway import LlmGateway  # тестируемый класс
from solid_dashboard.llm.cache import FileCache     # реальный файловый кэш
from solid_dashboard.llm.types import LlmResponse   # контрактный ответ
from solid_dashboard.llm.provider import Message, LlmOptions
from solid_dashboard.llm.budget import TokenBudgetController
from .mock_provider import MockProvider  # наш общий MockProvider

from solid_dashboard.llm.errors import (
    BudgetExhaustedError,
    RetryableError,
    NonRetryableError,
    LlmUnavailableError,
)


def make_messages() -> list[Message]:
    # Вспомогательная функция для создания минимального набора сообщений
    # Подставь реальный способ создания Message из твоего проекта
    return [
        Message(role="user", content="test prompt"),
    ]


def make_options() -> LlmOptions:
    # Минимальные валидные опции для провайдера
    # Важно: поля должны соответствовать реальному LlmOptions
    return LlmOptions(
        model="openai/gpt-4o-mini",
        temperature=0.0,
        max_tokens=64,
        # добавь остальные обязательные поля LlmOptions, если они есть
    )


def make_response(content: str = "ok", tokens: int = 10) -> LlmResponse:
    """
    Удобная фабрика для LlmResponse.

    Оборачивает создание ответа LLM так, чтобы в тестах
    не дублировать конструктор LlmResponse вручную.
    """
    return LlmResponse(
        content=content,
        tokens_used=tokens,
        model="openai/gpt-4o-mini",
    )

# Тест А1: Cache hit → провайдер не вызывается

def test_cache_hit_returns_cached_response_and_skips_provider(tmp_path: Path) -> None:
    """
    Сценарий:
    - создаем FileCache в temp-директории,
    - кладем туда заранее LlmResponse по ключу, который построит LlmGateway,
    - на вызове analyze() ожидаем возврат кэшированного ответа и отсутствие вызовов MockProvider.
    """
    # 1. Подготавливаем входные данные: messages и options
    messages: Sequence[Message] = make_messages()
    options: LlmOptions = make_options()

    # 2. Создаем временную директорию для файлового кэша
    cache_dir = tmp_path / "llm-cache"
    file_cache = FileCache(cache_dir)

    # 3. Создаем MockProvider с "пустым" сценарием (он не должен быть вызван)
    def scenario_never_called(call: int, messages: Sequence[Message], options: LlmOptions) -> LlmResponse:
        # Если этот сценарий вызывается — тест должен провалиться
        raise AssertionError("MockProvider.chat() должен был не вызываться при cache hit")

    mock_provider = MockProvider(scenario=scenario_never_called)

    # 4. Создаем Gateway с провайдером и кэшем, без бюджета
    gateway = LlmGateway(
        provider=mock_provider,
        cache=file_cache,
        budget=None,
    )

    # 5. Строим тот же ключ, который будет использовать analyze(), чтобы заранее записать ответ в кэш
    cache_key = gateway._build_cache_key(messages, options)  # приватный метод, но допустимо в тесте
    cached_response = LlmResponse(
        content="cached answer",
        tokens_used=42,
        model="openai/gpt-4o-mini",
    )
    # Сохраняем ответ в кэш напрямую (обходя Gateway)
    file_cache.set(cache_key, cached_response)

    # 6. Вызываем анализ через Gateway
    result = gateway.analyze(messages, options)

    # 7. Проверяем, что:
    # - вернулся именно кэшированный ответ,
    # - MockProvider ни разу не был вызван
    assert result.content == "cached answer"
    assert result.tokens_used == 42
    assert result.model == "openai/gpt-4o-mini"

    assert mock_provider.calls == 0  # провайдер не должен вызываться при cache hit

# Тест А2: Cache miss → провайдер вызывается и результат кэшируется

def test_cache_miss_calls_provider_and_stores_in_cache(tmp_path: Path) -> None:
    """
    Сценарий:
    - создаем FileCache в temp-директории (изначально пустой),
    - MockProvider возвращает LlmResponse при каждом вызове,
    - первый вызов analyze() должен:
        * вызвать провайдера,
        * сохранить ответ в кэш,
    - второй вызов с теми же messages/options должен:
        * вернуть тот же ответ,
        * НЕ вызывать провайдера повторно (cache hit).
    """
    # 1. Подготавливаем входные данные
    messages: Sequence[Message] = make_messages()
    options: LlmOptions = make_options()

    # 2. Создаем временную директорию для файлового кэша (изначально пустую)
    cache_dir = tmp_path / "llm-cache"
    file_cache = FileCache(cache_dir)

    # 3. Сценарий MockProvider: всегда возвращать один и тот же ответ
    def scenario_always_success(
        call: int,
        messages: Sequence[Message],
        options: LlmOptions,
    ) -> LlmResponse:
        # Здесь call можно использовать в asserts при необходимости
        return make_response(content="from provider", tokens=7)

    mock_provider = MockProvider(scenario=scenario_always_success)

    # 4. Создаем Gateway с провайдером и кэшем, без бюджета
    gateway = LlmGateway(
        provider=mock_provider,
        cache=file_cache,
        budget=None,
    )

    # 5. Первый вызов: кэш пустой, должен быть cache miss + вызов провайдера
    result1 = gateway.analyze(messages, options)

    assert result1.content == "from provider"
    assert result1.tokens_used == 7
    assert result1.model == "openai/gpt-4o-mini"
    assert mock_provider.calls == 1  # провайдер должен был вызваться ровно один раз

    # 6. Второй вызов с теми же messages/options:
    #    теперь должен быть cache hit, без дополнительного вызова провайдера
    result2 = gateway.analyze(messages, options)

    # Ответ с точки зрения контента такой же (берется из кэша)
    assert result2.content == "from provider"
    assert result2.tokens_used == 7
    assert result2.model == "openai/gpt-4o-mini"

    # Важно: провайдер не должен вызываться второй раз
    assert mock_provider.calls == 1

# ============================================================================
# Блок B: Budget
# ============================================================================

def test_without_budget_gateway_works_normally() -> None:
    """
    B3. Если Gateway сконфигурирован без budget (None),
    вызовы проходят штатно, проверки лимитов не ломают пайплайн.
    """
    messages = make_messages()
    options = make_options()

    def scenario_always_success(call: int, msgs: Sequence[Message], opts: LlmOptions) -> LlmResponse:
        return make_response(content="ok", tokens=10)

    mock_provider = MockProvider(scenario=scenario_always_success)

    # Кэш можно не передавать, нас интересует только бюджет
    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=None,
    )

    # Act
    result = gateway.analyze(messages, options)

    # Assert
    assert result.content == "ok"
    assert mock_provider.calls == 1


def test_budget_exhausted_before_call_raises_error() -> None:
    """
    B4. Если лимит токенов уже исчерпан ДО вызова,
    Gateway должен выбросить BudgetExhaustedError и НЕ вызывать провайдера.
    """
    messages = make_messages()
    options = make_options()

    def scenario_never_called(call: int, msgs: Sequence[Message], opts: LlmOptions) -> LlmResponse:
        raise AssertionError("Провайдер не должен вызываться при исчерпанном бюджете")

    mock_provider = MockProvider(scenario=scenario_never_called)

    # Бюджет на 10 токенов
    budget = TokenBudgetController(max_tokens=10)
    # Искусственно исчерпываем бюджет перед запросом
    budget.record_tokens(10)

    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=budget,
    )

    # Act & Assert
    with pytest.raises(BudgetExhaustedError) as exc_info:
        gateway.analyze(messages, options)

    # Проверяем, что ошибка содержит информацию о лимитах (если мы реализовали это в ошибке)
    # И что провайдер так и остался нетронутым
    assert mock_provider.calls == 0
    # Можно проверить текст ошибки, если важно
    assert "Token budget exhausted" in str(exc_info.value)


def test_budget_record_tokens_after_successful_call() -> None:
    """
    B5. При успешном ответе провайдера, потраченные токены
    должны быть записаны в TokenBudgetController.
    """
    messages = make_messages()
    options = make_options()

    def scenario_returns_15_tokens(call: int, msgs: Sequence[Message], opts: LlmOptions) -> LlmResponse:
        # Провайдер "потратил" 15 токенов
        return make_response(content="expensive answer", tokens=15)

    mock_provider = MockProvider(scenario=scenario_returns_15_tokens)

    # Бюджет большой, исчерпан не будет
    budget = TokenBudgetController(max_tokens=100)

    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=budget,
    )

    # Убеждаемся, что до вызова 0
    assert budget.used_tokens == 0

    # Act
    result = gateway.analyze(messages, options)

    # Assert
    assert result.content == "expensive answer"
    assert mock_provider.calls == 1
    # Gateway должен был вызвать budget.record_tokens(15)
    assert budget.used_tokens == 15

# ============================================================================
# Блок C: Retry
# ============================================================================

def test_retryable_error_then_success(monkeypatch) -> None:
    """
    C6. При RetryableError на первой попытке и успехе на второй
    Gateway должен сделать ровно 2 вызова провайдера и вернуть успешный результат.
    """
    messages = make_messages()
    options = make_options()

    # Сценарий: 1-й вызов -> RetryableError, 2-й -> успешный ответ
    def scenario_retry_then_success(
        call: int,
        msgs: Sequence[Message],
        opts: LlmOptions,
    ) -> LlmResponse:
        if call == 1:
            raise RetryableError("temporary error")
        return make_response(content="after retry", tokens=5)

    mock_provider = MockProvider(scenario=scenario_retry_then_success)

    # Чтобы тест не реально спал между ретраями, подменим time.sleep на no-op
    import solid_dashboard.llm.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod.time, "sleep", lambda _: None)

    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=None,
        _max_attempts=3,
        _retry_delays=(0.1, 0.2),
    )

    # Act
    result = gateway.analyze(messages, options)

    # Assert
    assert result.content == "after retry"
    # Провайдер должен быть вызван ровно 2 раза (1 ошибка + 1 успех)
    assert mock_provider.calls == 2


def test_retryable_error_exceeds_max_attempts_raises_llm_unavailable(monkeypatch) -> None:
    """
    C7. Если на всех попытках провайдер бросает RetryableError,
    Gateway должен после исчерпания _max_attempts поднять LlmUnavailableError.
    """
    messages = make_messages()
    options = make_options()

    def scenario_always_retryable(
        call: int,
        msgs: Sequence[Message],
        opts: LlmOptions,
    ) -> LlmResponse:
        raise RetryableError(f"temporary error #{call}")

    mock_provider = MockProvider(scenario=scenario_always_retryable)

    import solid_dashboard.llm.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod.time, "sleep", lambda _: None)

    max_attempts = 3

    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=None,
        _max_attempts=max_attempts,
        _retry_delays=(0.1, 0.2),
    )

    # Act & Assert
    with pytest.raises(LlmUnavailableError) as exc_info:
        gateway.analyze(messages, options)

    # Провайдер должен быть вызван ровно max_attempts раз
    assert mock_provider.calls == max_attempts
    assert "unavailable" in str(exc_info.value).lower()


def test_non_retryable_error_not_retried(monkeypatch) -> None:
    """
    C8. NonRetryableError не должен ретраиться:
    провайдер вызывается ровно один раз, ошибка пробрасывается сразу.
    """
    messages = make_messages()
    options = make_options()

    def scenario_non_retryable(
        call: int,
        msgs: Sequence[Message],
        opts: LlmOptions,
    ) -> LlmResponse:
        raise NonRetryableError("fatal error")

    mock_provider = MockProvider(scenario=scenario_non_retryable)

    import solid_dashboard.llm.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod.time, "sleep", lambda _: None)

    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=None,
        _max_attempts=3,
        _retry_delays=(0.1, 0.2),
    )

    # Act & Assert
    with pytest.raises(NonRetryableError):
        gateway.analyze(messages, options)

    # Важно: NonRetryableError не ретраится, вызов провайдера ровно один раз
    assert mock_provider.calls == 1

# ============================================================================
# Блок D: Degradation / robustness
# ============================================================================

# Тест D1: сбой записи в кэш не ломает пайплайн

def test_cache_write_error_does_not_break_pipeline(tmp_path: Path, monkeypatch) -> None:
    """
    D1. Ошибка при записи в кэш не должна ломать основной сценарий.

    Сценарий:
    - cache.get() всегда возвращает None (cache miss),
    - cache.set() бросает исключение (например, OSError),
    - провайдер возвращает валидный LlmResponse,
    - Gateway должен вернуть ответ провайдера и НЕ пробрасывать ошибку кэша.
    """
    messages = make_messages()
    options = make_options()

    # Провайдер всегда успешно отвечает
    def scenario_success(
        call: int,
        msgs: Sequence[Message],
        opts: LlmOptions,
    ) -> LlmResponse:
        return make_response(content="from provider despite cache error", tokens=3)

    mock_provider = MockProvider(scenario=scenario_success)

    # Реализуем минимальный "сломанный" кэш прямо в тесте
    class BrokenCache:
        # Простая реализация протокола LlmCache с ошибкой на set()

        def get(self, key: str) -> LlmResponse | None:
            # Всегда cache miss
            return None

        def set(self, key: str, value: LlmResponse) -> None:
            # Имитируем проблему файловой системы
            raise OSError("disk full or permission denied")

    broken_cache = BrokenCache()

    gateway = LlmGateway(
        provider=mock_provider,
        cache=broken_cache,  # подсовываем сломанный кэш
        budget=None,
    )

    # Для надежности подменим time.sleep на no-op (на случай RetryableError, хотя тут его нет)
    import solid_dashboard.llm.gateway as gateway_mod

    monkeypatch.setattr(gateway_mod.time, "sleep", lambda _: None)

    # Act: вызываем analyze()
    result = gateway.analyze(messages, options)

    # Assert:
    # - получаем нормальный ответ от провайдера
    # - провайдер был вызван ровно 1 раз
    # - исключений из-за cache.set() не было
    assert result.content == "from provider despite cache error"
    assert mock_provider.calls == 1

# Тест D2: max_tokens <= 0 → неограниченный бюджет

def test_budget_unlimited_when_max_tokens_non_positive() -> None:
    """
    D2. Если max_tokens <= 0, бюджет считается неограниченным:
    - is_exhausted() всегда False,
    - BudgetExhaustedError не возникает даже при большом расходе токенов.
    """
    messages = make_messages()
    options = make_options()

    # Провайдер всегда возвращает ответ с ненулевым числом токенов
    def scenario_many_tokens(
        call: int,
        msgs: Sequence[Message],
        opts: LlmOptions,
    ) -> LlmResponse:
        # Каждый вызов "тратит" 25 токенов
        return make_response(content=f"call #{call}", tokens=25)

    mock_provider = MockProvider(scenario=scenario_many_tokens)

    # max_tokens=0 → по нашему контракту бюджет не ограничен
    budget = TokenBudgetController(max_tokens=0)

    gateway = LlmGateway(
        provider=mock_provider,
        cache=None,
        budget=budget,
    )

    # До любых вызовов бюджет не должен считаться исчерпанным
    assert budget.is_exhausted() is False

    # Делаем несколько вызовов подряд, суммарно "перерасходуя" больше 0
    results = []
    for _ in range(3):
        result = gateway.analyze(messages, options)
        results.append(result)

    # Проверяем, что каждый вызов прошел успешно и BudgetExhaustedError не возник
    assert [r.content for r in results] == ["call #1", "call #2", "call #3"]
    assert mock_provider.calls == 3

    # Контроллер считает токены, но is_exhausted() все равно False при max_tokens <= 0
    assert budget.used_tokens > 0
    assert budget.is_exhausted() is False