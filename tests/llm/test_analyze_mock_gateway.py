# ---------------------------------------------------------------------------
# Интеграционные тесты метода analyze() с замоканным Gateway
#
# Проверяют оркестрацию внутри LlmSolidAdapter:
# - корректный вызов Gateway для каждого кандидата
# - правильный учет метрик metadata (processed, skipped, tokens_used, parse_*)
# - обработку сценариев, когда Gateway возвращает успехи, частичные результаты 
#   или ошибки (RetryableError/NonRetryableError)
# ---------------------------------------------------------------------------

from pathlib import Path
from typing import cast

import pytest

from solid_dashboard.llm.gateway import LlmGateway
from solid_dashboard.llm.llm_adapter import LlmSolidAdapter
from solid_dashboard.llm.errors import RetryableError
from solid_dashboard.llm.types import (
    ProjectMap,
    LlmConfig,
    LlmAnalysisInput,
)
from .test_parse_llm_response import (
    FakeResponse,
    _make_candidate,
)


@pytest.fixture
def prompts_dir(tmp_path: Path) -> Path:
    # создаем временный каталог prompts, чтобы тесты не зависели от реального дерева проекта
    prompts = tmp_path / "prompts"
    prompts.mkdir()

    # минимальный system prompt, которого достаточно для сборки messages
    (prompts / "system.md").write_text(
        "You are a strict SOLID analyzer. Return JSON only.\n",
        encoding="utf-8",
    )

    # базовый user prompt с плейсхолдерами, которые ожидает текущий LlmAdapter
    (prompts / "user_base.md").write_text(
        "\n".join(
            [
                "Analyze candidate for {candidate_type}.",
                "Class: {class_name}",
                "File: {file_path}",
                "Source code:",
                "{source_code}",
            ]
        ),
        encoding="utf-8",
    )

    # секция для OCP-кандидатов
    (prompts / "user_ocp_section.md").write_text(
        "Focus on OCP violations.\n",
        encoding="utf-8",
    )

    # секция для LSP-кандидатов
    (prompts / "user_lsp_section.md").write_text(
        "Focus on LSP violations.\n",
        encoding="utf-8",
    )

    # минимально достаточный JSON-контракт для Response Parser
    (prompts / "response_schema.json").write_text(
        """
        {
          "type": "object",
          "properties": {
            "findings": {
              "type": "array"
            }
          },
          "required": ["findings"]
        }
        """.strip(),
        encoding="utf-8",
    )

    return prompts


@pytest.fixture
def llm_config(tmp_path: Path, prompts_dir: Path) -> LlmConfig:
    # базовая конфигурация LLM для юнит-тестов, полностью изолированная через tmp_path
    return LlmConfig(
        provider="openrouter",
        model="test-model",
        api_key=None,
        endpoint=None,
        max_tokens_per_run=1000,
        cache_dir=str(tmp_path / "cache"),
        prompts_dir=str(prompts_dir),
    )


class FakeGateway:
    # фейковый Gateway, управляющийся через очередность ответов
    def __init__(self, responses):
        # responses — список FakeResponse или исключений
        self._responses = list(responses)
        self.calls = 0

    def analyze(self, messages, options):
        # на каждый вызов берем следующий элемент из очереди
        item = self._responses[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _make_adapter_with_gateway(
    gateway: FakeGateway,
    config: LlmConfig,
) -> LlmSolidAdapter:
    # приводим тестовый gateway к контрактному типу для статического анализа
    return LlmSolidAdapter(
        gateway=cast(LlmGateway, gateway),
        config=config,
    )


def _make_project_map() -> ProjectMap:
    # пустая карта проекта нас вполне устраивает для логики LlmSolidAdapter
    return ProjectMap()


def test_analyze_all_success(llm_config: LlmConfig):
    # все кандидаты дают success → processed == N, parse_* == 0
    project_map = _make_project_map()
    candidates = [
        _make_candidate(),
        _make_candidate(),
    ]

    responses = [
        FakeResponse('{"findings": []}', tokens_used=5),
        FakeResponse(
            """
            {
              "findings": [
                { "principle": "OCP", "message": "Issue", "severity": "warning" }
              ]
            }
            """,
            tokens_used=7,
        ),
    ]

    gateway = FakeGateway(responses)
    adapter = _make_adapter_with_gateway(gateway, llm_config)

    input_data = LlmAnalysisInput(project_map=project_map, candidates=candidates)

    output = adapter.analyze(input_data)

    assert output.metadata.candidates_processed == 2
    assert output.metadata.candidates_skipped == 0
    assert output.metadata.parse_failures == 0
    assert output.metadata.parse_partials == 0
    assert output.metadata.parse_warnings >= 0
    # ожидаем суммарные токены 5 + 7
    assert output.metadata.tokens_used == 12
    assert output.metadata.cache_hits == 0


def test_analyze_all_parse_failures(llm_config: LlmConfig):
    # все ответы LLM не парсятся → processed=0, skipped=N, parse_failures=N
    project_map = _make_project_map()
    candidates = [
        _make_candidate(),
        _make_candidate(),
    ]

    responses = [
        FakeResponse("not json", tokens_used=3),
        FakeResponse('{"foo": 1}', tokens_used=4),
    ]

    gateway = FakeGateway(responses)
    adapter = _make_adapter_with_gateway(gateway, llm_config)

    input_data = LlmAnalysisInput(project_map=project_map, candidates=candidates)

    output = adapter.analyze(input_data)

    assert output.metadata.candidates_processed == 0
    assert output.metadata.candidates_skipped == 2
    assert output.metadata.parse_failures == 2
    assert output.metadata.parse_partials == 0
    assert output.metadata.tokens_used == 3 + 4
    assert output.metadata.cache_hits == 0
    assert output.findings == []


def test_analyze_mixed_success_partial_failure(llm_config: LlmConfig):
    # success + partial + failure → проверяем все счетчики
    project_map = _make_project_map()
    candidates = [
        _make_candidate(),
        _make_candidate(),
        _make_candidate(),
    ]

    responses = [
        # success — один валидный finding
        FakeResponse(
            """
            {
              "findings": [
                { "principle": "OCP", "message": "Valid 1", "severity": "warning" }
              ]
            }
            """,
            tokens_used=5,
        ),
        # partial — один валидный, один отброшенный
        FakeResponse(
            """
            {
              "findings": [
                { "principle": "OCP", "message": "Valid 2", "severity": "warning" },
                { "principle": "OCP" }
              ]
            }
            """,
            tokens_used=6,
        ),
        # failure — мусор
        FakeResponse("garbage", tokens_used=7),
    ]

    gateway = FakeGateway(responses)
    adapter = _make_adapter_with_gateway(gateway, llm_config)

    input_data = LlmAnalysisInput(project_map=project_map, candidates=candidates)

    output = adapter.analyze(input_data)

    assert output.metadata.candidates_processed == 2
    assert output.metadata.candidates_skipped == 1
    assert output.metadata.parse_failures == 1
    assert output.metadata.parse_partials == 1
    # два валидных finding — по одному из первых двух ответов
    assert len(output.findings) == 2
    assert output.metadata.tokens_used == 5 + 6 + 7


def test_analyze_gateway_error_counts_as_skipped_without_parse_counters(
    llm_config: LlmConfig,
):
    # ошибка Gateway → skipped, но parse_* и warnings не трогаем
    project_map = _make_project_map()
    candidates = [_make_candidate()]

    responses = [RetryableError("temporary")]

    gateway = FakeGateway(responses)
    adapter = _make_adapter_with_gateway(gateway, llm_config)

    input_data = LlmAnalysisInput(project_map=project_map, candidates=candidates)

    output = adapter.analyze(input_data)

    assert output.metadata.candidates_processed == 0
    assert output.metadata.candidates_skipped == 1
    assert output.metadata.parse_failures == 0
    assert output.metadata.parse_partials == 0
    assert output.metadata.parse_warnings == 0
    assert output.metadata.tokens_used == 0
    assert output.metadata.cache_hits == 0
    assert output.findings == []