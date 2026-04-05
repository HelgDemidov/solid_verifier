# ---------------------------------------------------------------------------
# Юнит-тесты логики дедупликации findings и кандидатов
# Правила приоритетов: OCP-H-001 > OCP-H-004, LSP-H-001 > LSP-H-002
# ---------------------------------------------------------------------------

from solid_dashboard.llm.heuristics._runner import (
    _deduplicate_findings,
    _deduplicate_candidates,
)
from solid_dashboard.llm.types import Finding, FindingDetails, LlmCandidate


class TestDeduplicateFindings:
    """
    Проверяет слияние конфликтующих OCP findings для одного метода.
    OCP-H-001 побеждает OCP-H-004 как более специфичный.
    """

    def test_ocp_h001_and_h004_on_same_method_are_merged(self):
        """
        Оба OCP-finding на одном методе → остается один OCP-H-001,
        в explanation добавляется упоминание OCP-H-004.
        """
        file_path = "app/services/payment_service.py"
        class_name = "PaymentService"
        method_name = "process"

        f1 = Finding(
            rule="OCP-H-001",
            file=file_path,
            class_name=class_name,
            line=None,
            severity="warning",
            message="Method 'process' contains an isinstance() chain with 4 branches",
            source="heuristic",
            details=FindingDetails(
                principle="OCP",
                explanation="OCP-H-001 explanation",
                suggestion="OCP-H-001 suggestion",
                method_name=method_name,
            ),
        )

        f2 = Finding(
            rule="OCP-H-004",
            file=file_path,
            class_name=class_name,
            line=None,
            severity="warning",
            message="Method 'process' has high cyclomatic complexity",
            source="heuristic",
            details=FindingDetails(
                principle="OCP",
                explanation="OCP-H-004 explanation",
                suggestion="OCP-H-004 suggestion",
                method_name=method_name,
            ),
        )

        deduped = _deduplicate_findings([f1, f2])

        assert len(deduped) == 1
        winner = deduped[0]
        assert winner.rule == "OCP-H-001"
        assert winner.details is not None
        assert "Also detected: OCP-H-004" in (winner.details.explanation or "")


class TestDeduplicateFindingsLSP:
    """
    Проверяет, что LSP-H-001 и LSP-H-002 на одном методе
    не дублируются: LSP-H-001 побеждает.
    """

    def test_lsp_h001_and_h002_on_same_method_are_merged(self):
        """
        Оба LSP-finding на одном методе → остается один LSP-H-001,
        в explanation добавляется упоминание LSP-H-002.
        """
        file_path = "app/models/user.py"
        class_name = "UserRepository"
        method_name = "save"

        f1 = Finding(
            rule="LSP-H-001",
            file=file_path,
            class_name=class_name,
            line=None,
            severity="warning",
            message="Overridden method 'save' raises NotImplementedError",
            source="heuristic",
            details=FindingDetails(
                principle="LSP",
                explanation="LSP-H-001 explanation",
                suggestion="LSP-H-001 suggestion",
                method_name=method_name,
            ),
        )

        f2 = Finding(
            rule="LSP-H-002",
            file=file_path,
            class_name=class_name,
            line=None,
            severity="warning",
            message="Overridden method 'save' has an empty body",
            source="heuristic",
            details=FindingDetails(
                principle="LSP",
                explanation="LSP-H-002 explanation",
                suggestion="LSP-H-002 suggestion",
                method_name=method_name,
            ),
        )

        deduped = _deduplicate_findings([f1, f2])

        assert len(deduped) == 1
        winner = deduped[0]
        assert winner.rule == "LSP-H-001"
        assert winner.details is not None
        assert "Also detected: LSP-H-002" in (winner.details.explanation or "")


class TestDeduplicateCandidates:
    """
    Проверяет объединение нескольких кандидатов одного класса
    в один LlmCandidate с агрегированными полями.
    """

    def test_candidates_for_same_class_are_merged(self):
        """
        OCP- и LSP-кандидат одного класса → один кандидат
        с типом 'both', объединенными heuristic_reasons и max priority.
        """
        file_path = "app/services/report_service.py"
        class_name = "ReportService"

        c1 = LlmCandidate(
            class_name=class_name,
            file_path=file_path,
            source_code="class ReportService: ...",
            candidate_type="ocp",
            heuristic_reasons=["OCP-H-001"],
            priority=5,
        )

        c2 = LlmCandidate(
            class_name=class_name,
            file_path=file_path,
            source_code="class ReportService: ...",
            candidate_type="lsp",
            heuristic_reasons=["LSP-H-001"],
            priority=3,
        )

        merged = _deduplicate_candidates([c1, c2])

        assert len(merged) == 1
        winner = merged[0]
        assert winner.class_name == class_name
        assert winner.file_path == file_path
        assert winner.candidate_type == "both"
        assert set(winner.heuristic_reasons) == {"OCP-H-001", "LSP-H-001"}
        assert winner.priority == 5
