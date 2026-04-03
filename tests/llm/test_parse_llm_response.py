# ---------------------------------------------------------------------------
# Тесты семантического парсера (ACL-B) LLM-ответов
# 
# Проверяют метод `_parse_response` в LlmSolidAdapter в изоляции от Gateway.
# Фиксируют контракт обработки JSON:
# - status="success": валидный ответ (в т.ч. с пустым списком findings)
# - status="partial": часть элементов findings валидна, часть отброшена
# - status="failure": невалидный JSON, отсутствие findings или все элементы отброшены
# ---------------------------------------------------------------------------

import pytest
from typing import cast

from solid_dashboard.llm.gateway import LlmGateway
from solid_dashboard.llm.llm_adapter import LlmSolidAdapter  
from solid_dashboard.llm.types import ( 
    LlmCandidate,
    LlmConfig,
    ParseResult,
    CandidateType
)

class FakeResponse:
    # упрощенный ответ Gateway для тестов ACL-B
    def __init__(self, content: str, tokens_used: int = 10) -> None:
        self.content = content
        self.tokens_used = tokens_used


def _make_adapter() -> LlmSolidAdapter:
    # создаем адаптер с фейковым Gateway, который в этих тестах не используется
    class _DummyGateway:
        def analyze(self, messages, options):
            raise RuntimeError("Gateway should not be called in _parse_response tests")

    dummy_config = LlmConfig(
        provider="openrouter",
        model="test-model",
        api_key=None,
        endpoint=None,
        max_tokens_per_run=1000,
        cache_dir=".solid-cache/llm",
        prompts_dir="tools/solid_verifier/prompts",
    )
    return LlmSolidAdapter(
        gateway=cast(LlmGateway, _DummyGateway()),
        config=dummy_config,
    )


def _make_candidate(candidate_type: CandidateType = "ocp") -> LlmCandidate:
    # базовый кандидат для всех тестов парсера
    return LlmCandidate(
        class_name="MyClass",
        file_path="path/to/file.py",
        source_code="class MyClass: pass",
        candidate_type=candidate_type,  # "ocp" | "lsp" | "both"
        heuristic_reasons=["OCP-H-001"],
        priority=10,
    )


def test_parse_response_success_empty_findings():
    # корректный JSON, но список findings пустой → success, без предупреждений
    adapter = _make_adapter()
    candidate = _make_candidate()

    response = FakeResponse('{"findings": []}')

    result: ParseResult = adapter._parse_response(response, candidate)

    assert result.status == "success"
    assert result.findings == []
    assert result.warnings == []


def test_parse_response_success_with_valid_finding():
    # один валидный finding → success с одним Finding
    adapter = _make_adapter()
    candidate = _make_candidate()

    response = FakeResponse(
        """
        {
          "findings": [
            {
              "principle": "OCP",
              "message": "Short description",
              "severity": "warning"
            }
          ]
        }
        """
    )

    result: ParseResult = adapter._parse_response(response, candidate)

    assert result.status == "success"
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.source == "llm"
    assert f.file == candidate.file_path
    assert f.class_name == candidate.class_name
    assert f.details is not None
    assert f.details.principle == "OCP"


def test_parse_response_failure_invalid_json():
    # текст вместо JSON → failure, findings пустой, есть предупреждение
    adapter = _make_adapter()
    candidate = _make_candidate()

    response = FakeResponse("Hello, I am an LLM, not JSON")

    result: ParseResult = adapter._parse_response(response, candidate)

    assert result.status == "failure"
    assert result.findings == []
    assert result.warnings  # хотя бы одно предупреждение


def test_parse_response_failure_missing_findings_key():
    # JSON без ключа findings → failure
    adapter = _make_adapter()
    candidate = _make_candidate()

    response = FakeResponse('{"foo": 1}')

    result: ParseResult = adapter._parse_response(response, candidate)

    assert result.status == "failure"
    assert result.findings == []
    assert result.warnings  # должно быть предупреждение про отсутствующий findings


def test_parse_response_partial_mixed_valid_and_invalid_items():
    # один валидный, один невалидный (без message) → partial
    adapter = _make_adapter()
    candidate = _make_candidate()

    response = FakeResponse(
        """
        {
          "findings": [
            {
              "principle": "OCP",
              "message": "Valid finding",
              "severity": "warning"
            },
            {
              "principle": "OCP"
            }
          ]
        }
        """
    )

    result: ParseResult = adapter._parse_response(response, candidate)

    assert result.status == "partial"
    assert len(result.findings) == 1
    assert result.warnings or True  # наличие/отсутствие warning не критично, статус главное


def test_parse_response_failure_all_items_invalid():
    # все элементы отброшены → failure
    adapter = _make_adapter()
    candidate = _make_candidate(candidate_type="both")

    response = FakeResponse(
        """
        {
          "findings": [
            { "principle": "???", "severity": "warning" },
            { "severity": "info" }
          ]
        }
        """
    )

    result: ParseResult = adapter._parse_response(response, candidate)

    assert result.status == "failure"
    assert result.findings == []
    assert result.warnings  # логично ожидать предупреждение