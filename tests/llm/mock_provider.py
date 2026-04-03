from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from solid_dashboard.llm.types import LlmResponse  # импортируем контрактный ответ LLM
from solid_dashboard.llm.provider import Message, LlmOptions  # типы сообщений и опций

# Тип сценария: по номеру вызова и входу решает, что делать (вернуть ответ или бросить ошибку)
ScenarioFunc = Callable[[int, Sequence[Message], LlmOptions], LlmResponse]


@dataclass
class MockProvider:
    """
    Тестовый провайдер для LlmGateway.

    Поведение задается функцией-сценарием:
    - на каждом вызове chat() увеличивает счетчик calls,
    - передает номер вызова, messages и options в scenario,
    - scenario может вернуть LlmResponse или бросить RetryableError/NonRetryableError.
    """

    scenario: ScenarioFunc
    calls: int = 0

    def chat(self, messages: list[Message], options: LlmOptions) -> LlmResponse:
        # Увеличиваем счетчик вызовов провайдера
        self.calls += 1
        # Делегируем фактическое поведение сценарной функции
        return self.scenario(self.calls, messages, options)