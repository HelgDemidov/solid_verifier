# tools/solid_verifier/solid_dashboard/llm/llm_adapter.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from .errors import RetryableError, NonRetryableError
from .gateway import LlmGateway
from .provider import Message, LlmOptions
from .types import (
    LlmAnalysisInput,
    LlmAnalysisOutput,
    LlmMetadata,
    LlmConfig,
    LlmCandidate,
    Finding,
)

logger = logging.getLogger(__name__)


@dataclass
class LlmSolidAdapter:
    """
    Ядро LLM-анализа.

    Получает LlmGateway и LlmConfig через DI.
    Здесь только оркестрация: контекст → промпт → Gateway → парсинг.
    """

    gateway: LlmGateway
    config: LlmConfig

    def analyze(self, input_data: LlmAnalysisInput) -> LlmAnalysisOutput:
        """
        Обходит кандидатов по убыванию priority, вызывает Gateway и собирает findings.
        """
        candidates: List[LlmCandidate] = sorted(
            input_data.candidates,
            key=lambda c: c.priority,
            reverse=True,
        )

        all_findings: List[Finding] = []
        processed = 0
        skipped = 0
        tokens_used = 0
        cache_hits = 0

        for candidate in candidates:
            try:
                context = self._build_context(input_data.project_map, candidate)
                messages, options = self._build_prompt_and_options(context, candidate)

                response = self.gateway.analyze(messages, options)

                candidate_findings = self._parse_response(response, candidate)
                all_findings.extend(candidate_findings)

                # Простая эвристика: если токены 0 — считаем cache hit
                if getattr(response, "tokens_used", 0) == 0:
                    cache_hits += 1
                else:
                    tokens_used += getattr(response, "tokens_used", 0)

                processed += 1

            except (RetryableError, NonRetryableError) as exc:
                logger.warning(
                    "LLM error for candidate '%s' (%s), skipping: %s",
                    candidate.class_name,
                    candidate.candidate_type,
                    exc,
                )
                skipped += 1
            except Exception as exc:
                logger.exception(
                    "Unexpected error in LlmSolidAdapter for candidate '%s': %s",
                    candidate.class_name,
                    exc,
                )
                skipped += 1

        if candidates and processed == 0:
            logger.warning(
                "All LLM candidates failed (%d skipped). "
                "Check model compatibility or prompt configuration.",
                skipped,
            )

        metadata = LlmMetadata(
            candidates_processed=processed,
            candidates_skipped=skipped,
            tokens_used=tokens_used,
            cache_hits=cache_hits,
        )

        return LlmAnalysisOutput(findings=all_findings, metadata=metadata)

    # --- Context Assembler (заглушка) ---

    def _build_context(self, project_map, candidate: LlmCandidate) -> dict:
        """
        Минимальный контекст: только фокус-класс.
        Поля соответствуют LlmCandidate в types.py: class_name, file_path, source_code, candidate_type.
        """
        return {
            "class_name": candidate.class_name,
            "file_path": candidate.file_path,
            "source_code": candidate.source_code,
            "candidate_type": candidate.candidate_type,
        }

    # --- Prompt Builder (минимальная версия) ---

    def _build_prompt_and_options(self, context: dict, candidate: LlmCandidate) -> tuple[List[Message], LlmOptions]:
        """
        Временный упрощённый билд промпта, без внешних .md-шаблонов.
        """
        system_text = "You are a SOLID principles expert. Analyze Python code for OCP and LSP."

        user_text = (
            f"Analyze the following class for {context['candidate_type']} issues:\n\n"
            f"Class: {context['class_name']}\n"
            f"File: {context['file_path']}\n\n"
            f"```python\n{context['source_code']}\n```\n"
        )

        messages = [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]

        options = LlmOptions(model=self.config.model)

        return messages, options

    # --- Response Parser (заглушка) ---

    def _parse_response(self, response, candidate: LlmCandidate) -> List[Finding]:
        """
        Заглушка: позже будет полноценный парсер JSON-ответа LLM.
        """
        return []