from __future__ import annotations
from pathlib import Path

import json
import re

import logging
from dataclasses import dataclass
from typing import List, Sequence

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
    ProjectMap, 
    FindingDetails, # тип ProjectMap для явной связи с контекстом
    ParseResult,        
    ParseStatus
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

    def analyze(self, analysis_input: LlmAnalysisInput) -> LlmAnalysisOutput:
        project_map = analysis_input.project_map
        candidates = sorted(
            analysis_input.candidates,
            key=lambda c: c.priority,
            reverse=True,
        )

        all_findings: list[Finding] = []
        processed = 0
        skipped = 0
        tokens_used = 0
        cache_hits = 0

        parse_failures = 0      # NEW
        parse_partials = 0      # NEW
        parse_warnings = 0      # NEW

        for candidate in candidates:
            try:
                context = self._build_context(project_map, candidate)
                messages, options = self._build_prompt_and_options(context, candidate)
                response = self.gateway.analyze(messages, options)

                # парсим ответ LLM через ACL-B
                parse_result = self._parse_response(response, candidate)

                # Агрегируем warnings ACL-B
                parse_warnings += len(parse_result.warnings)

                if parse_result.status == "failure":
                    # HTTP-уровень ок, но JSON/контент не соответствует контракту
                    parse_failures += 1
                    skipped += 1
                else:
                    # success или partial считаем обработанными
                    if parse_result.status == "partial":
                        parse_partials += 1

                    all_findings.extend(parse_result.findings)
                    processed += 1

                # Токены и cache
                tokens_for_call = getattr(response, "tokens_used", 0)
                if tokens_for_call == 0:
                    cache_hits += 1
                else:
                    tokens_used += tokens_for_call

            except (RetryableError, NonRetryableError) as exc:
                logger.warning(
                    "LLM error for candidate '%s' (%s), skipping. %s: %s",
                    candidate.class_name,
                    candidate.candidate_type,
                    type(exc).__name__,
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
                "All LLM candidates failed: %d skipped out of %d. "
                "Check model compatibility or prompt configuration.",
                skipped,
                len(candidates),
            )

        metadata = LlmMetadata(
            candidates_processed=processed,
            candidates_skipped=skipped,
            tokens_used=tokens_used,
            cache_hits=cache_hits,
            parse_failures=parse_failures,     # NEW
            parse_partials=parse_partials,     # NEW
            parse_warnings=parse_warnings,     # NEW
        )
        return LlmAnalysisOutput(findings=all_findings, metadata=metadata)

    # --- Context Assembler (минимальная версия, будет расширена на Шаге 5.x) ---

    def _build_context(self, project_map: ProjectMap, candidate: LlmCandidate) -> dict:
        """
        Минимальный контекст: только фокус-класс.
        Поля соответствуют LlmCandidate в types.py: class_name, file_path, source_code, candidate_type.

        В следующих шагах сюда будут добавлены связи из ProjectMap (родители, интерфейсы, соседние классы).
        """
        # комментарий: пока игнорируем project_map, используем только данные кандидата
        return {
            "class_name": candidate.class_name,
            "file_path": candidate.file_path,
            "source_code": candidate.source_code,
            "candidate_type": candidate.candidate_type,
        }

    # --- Prompt Builder (версия с опорой на внешние .md-шаблоны)

    def _build_prompt_and_options(
        self,
        context: dict,
        candidate: "LlmCandidate",
    ) -> tuple[Sequence[Message], LlmOptions]:
        """
        Строит итоговый промпт для LLM, объединяя контекст и шаблоны из файловой системы.

        Использует:
        - system.md      — роль и общие правила анализа SOLID;
        - user_base.md   — базовый пользовательский промпт для одного класса;
        - user_ocp_section.md / user_lsp_section.md — доп. секции в зависимости от типа кандидата;
        - response_schema.json — описание ожидаемого JSON-ответа.
        """
        prompts_dir = Path(self.config.prompts_dir)

        # --- System prompt ---

        system_path = prompts_dir / "system.md"
        try:
            system_text = system_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "Failed to read system.md from %s: %s. Using fallback.",
                prompts_dir,
                exc,
            )
            system_text = (
                "You are a SOLID principles expert. "
                "Analyze Python code for potential OCP and LSP issues."
            )

        # --- Base user prompt template ---

        user_base_path = prompts_dir / "user_base.md"
        try:
            user_template = user_base_path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "Failed to read user_base.md from %s: %s. Using fallback.",
                prompts_dir,
                exc,
            )
            user_template = (
                "Analyze the following Python class for {candidate_type} issues.\n"
                "Class: {class_name}\n"
                "File: {file_path}\n"
                "```python\n{source_code}\n```"
            )

        # --- Optional OCP / LSP focus sections ---

        ocp_section_text = ""
        lsp_section_text = ""

        # комментарий: OCP-секция добавляется только для кандидатов с типом ocp или both
        if candidate.candidate_type in ("ocp", "both"):
            ocp_path = prompts_dir / "user_ocp_section.md"
            try:
                ocp_section_text = ocp_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning(
                    "Failed to read user_ocp_section.md from %s: %s. Skipping OCP section.",
                    prompts_dir,
                    exc,
                )

        # комментарий: LSP-секция добавляется только для кандидатов с типом lsp или both
        if candidate.candidate_type in ("lsp", "both"):
            lsp_path = prompts_dir / "user_lsp_section.md"
            try:
                lsp_section_text = lsp_path.read_text(encoding="utf-8")
            except Exception as exc:
                logger.warning(
                    "Failed to read user_lsp_section.md from %s: %s. Skipping LSP section.",
                    prompts_dir,
                    exc,
                )

        # --- Response schema (JSON) ---

        schema_suffix = ""
        schema_path = prompts_dir / "response_schema.json"
        try:
            raw = schema_path.read_text(encoding="utf-8")
            # комментарий: предполагаем простую структуру {"instruction": "...", ...}
            # и используем только поле instruction; при ошибках парсинга fallback — пустой суффикс.
            import json  # локальный импорт, чтобы не тянуть json глобально, если LLM отключен

            data = json.loads(raw)
            instruction = data.get("instruction")
            if isinstance(instruction, str) and instruction.strip():
                schema_suffix = "\n\n" + instruction.strip()
        except Exception as exc:
            logger.warning(
                "Failed to read or parse response_schema.json from %s: %s. "
                "Proceeding without explicit schema instruction.",
                prompts_dir,
                exc,
            )

        # --- Формирование итогового user-промпта ---

        try:
            base_user_text = user_template.format(
                candidate_type=candidate.candidate_type,
                class_name=candidate.class_name,
                file_path=candidate.file_path,
                source_code=candidate.source_code,
            )
        except KeyError as exc:
            logger.error(
                "Missing placeholder %s in user_base.md. Using fallback user prompt.",
                exc,
            )
            base_user_text = (
                f"Analyze the following Python class for {candidate.candidate_type} issues.\n"
                f"Class: {candidate.class_name}\n"
                f"File: {candidate.file_path}\n"
                f"```python\n{candidate.source_code}\n```"
            )

        # комментарий: собираем итоговый текст пользователя по слоям:
        # базовый промпт → OCP/LSP‑фокусы → инструкция по JSON-схеме
        user_parts = [base_user_text]
        if ocp_section_text:
            user_parts.append("\n\n" + ocp_section_text.strip())
        if lsp_section_text:
            user_parts.append("\n\n" + lsp_section_text.strip())
        if schema_suffix:
            user_parts.append(schema_suffix)

        user_text = "".join(user_parts)

        # --- Messages + options для Gateway ---

        messages: List[Message] = [
            Message(role="system", content=system_text),
            Message(role="user", content=user_text),
        ]
        options = LlmOptions(model=self.config.model)

        return messages, options

    # --- Response Parser (ACL-B) ---

    def _extract_json_content(self, text: str) -> dict | None:
        """
        Слой 1: Извлечение сырого JSON из текста ответа LLM.
        Пытается распарсить напрямую, очистить от markdown-тегов или найти блок { ... }.
        """
        if not text:
            return None

        text = text.strip()
        
        # Попытка 1: Прямой парсинг (если модель вернула идеальный JSON)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # Попытка 2: Вырезка markdown-блока (```json ... ``` или ``` ... ```)
        # re.DOTALL позволяет символу '.' совпадать с переносами строк
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass

        # Попытка 3: Экстренный поиск первого '{' и последнего '}'
        start_idx = text.find("{")
        end_idx = text.rfind("}")
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            try:
                return json.loads(text[start_idx : end_idx + 1])
            except json.JSONDecodeError:
                pass

        return None

    def _validate_structure(self, data: dict) -> list[dict] | None:
        """
        Слой 2: Минимальная проверка ожидаемой структуры ответа.
        """
        if not isinstance(data, dict):
            return None
        
        findings = data.get("findings")
        if not isinstance(findings, list):
            return None
            
        return findings

    def _validate_finding(self, raw: dict, candidate: LlmCandidate) -> Finding | None:
        """
        Слой 3: Валидация конкретной находки и маппинг в доменные
        Finding / FindingDetails.
        """
        if not isinstance(raw, dict):
            return None

        # message — минимально обязательное содержимое finding;
        # без него finding теряет смысл для отчета.
        raw_message = raw.get("message")
        if not isinstance(raw_message, str) or not raw_message.strip():
            return None
        message = raw_message.strip()

        # нормализуем severity в поддерживаемый набор;
        # неизвестные значения понижаем до warning как безопасный дефолт.
        raw_severity = str(raw.get("severity", "")).lower()
        severity = raw_severity if raw_severity in ("error", "warning", "info") else "warning"

        # principle — смысловое поле details, а не top-level поля Finding.
        # Сначала доверяем явному ответу модели, потом используем candidate_type как fallback.
        raw_principle = str(raw.get("principle", "")).upper()
        if raw_principle not in ("OCP", "LSP"):
            if candidate.candidate_type == "ocp":
                raw_principle = "OCP"
            elif candidate.candidate_type == "lsp":
                raw_principle = "LSP"
            else:
                # для candidate_type="both" без явного principle
                # finding неоднозначен, поэтому лучше пропустить его.
                return None

        # rule генерируем сами по внутреннему соглашению проекта,
        # чтобы не зависеть от капризов LLM и сохранить единый naming contract.
        rule = f"{raw_principle}-LLM-001"

        # explanation допускает два возможных имени поля в JSON:
        # "explanation" как целевое и "details" как совместимость с ранними промптами.
        raw_explanation = raw.get("explanation")
        if not isinstance(raw_explanation, str) or not raw_explanation.strip():
            raw_explanation = raw.get("details")
        explanation = raw_explanation.strip() if isinstance(raw_explanation, str) and raw_explanation.strip() else None

        raw_suggestion = raw.get("suggestion")
        suggestion = raw_suggestion.strip() if isinstance(raw_suggestion, str) and raw_suggestion.strip() else None

        raw_method_name = raw.get("method_name")
        method_name = raw_method_name.strip() if isinstance(raw_method_name, str) and raw_method_name.strip() else None

        raw_analyzed_with = raw.get("analyzed_with")
        analyzed_with = None
        if isinstance(raw_analyzed_with, list):
            cleaned = [
                item.strip()
                for item in raw_analyzed_with
                if isinstance(item, str) and item.strip()
            ]
            if cleaned:
                analyzed_with = cleaned

        details = FindingDetails(
            principle=raw_principle,
            explanation=explanation,
            suggestion=suggestion,
            analyzed_with=analyzed_with,
            heuristic_corroboration=True,
            method_name=method_name,
        )

        return Finding(
            rule=rule,
            file=candidate.file_path,
            severity=severity,
            message=message,
            source="llm",
            class_name=candidate.class_name,
            line=None,
            details=details,
        )

    def _parse_response(self, response, candidate: LlmCandidate) -> ParseResult:
        # warnings собираем по мере прохождения слоев ACL-B
        warnings: list[str] = []

        raw_content = getattr(response, "content", "")

        # 1. Извлекаем JSON-словарь
        parsed_data = self._extract_json_content(raw_content)
        if not parsed_data:
            msg = (
                f"Failed to extract JSON from LLM response for candidate "
                f"'{candidate.class_name}'. Raw snippet: "
                f"{raw_content[:100].replace('\\n', ' ')}..."
            )
            logger.warning(msg)
            warnings.append(msg)

            return ParseResult(
                findings=[],
                warnings=warnings,
                status="failure",
            )

        # 2. Проверяем наличие ключа findings
        raw_findings = self._validate_structure(parsed_data)
        if raw_findings is None:
            keys = list(parsed_data.keys())
            msg = (
                f"LLM response for candidate '{candidate.class_name}' "
                f"missing 'findings' array. Parsed keys: {keys}"
            )
            logger.warning(msg)
            warnings.append(msg)

            return ParseResult(
                findings=[],
                warnings=warnings,
                status="failure",
            )

        # 3. Маппим каждый finding
        valid_findings: list[Finding] = []
        dropped_count = 0

        for raw_item in raw_findings:
            finding = self._validate_finding(raw_item, candidate)
            if finding:
                valid_findings.append(finding)
            else:
                dropped_count += 1

        if not valid_findings and len(raw_findings) > 0:
            msg = (
                f"Extracted {len(raw_findings)} items for candidate "
                f"'{candidate.class_name}', but none were valid findings."
            )
            logger.warning(msg)
            warnings.append(msg)

        # Определяем статус
        if not valid_findings and not raw_findings:
            # JSON и структура корректны, просто модель не нашла нарушений.
            status: ParseStatus = "success"
        elif dropped_count == 0:
            status = "success"
        elif dropped_count < len(raw_findings):
            status = "partial"
        else:
            # формально сюда мы уже зашли в ветке "none valid", но оставляем на случай
            status = "failure"

        return ParseResult(
            findings=valid_findings,
            warnings=warnings,
            status=status,
        )