# ---------------------------------------------------------------------------
# Юнит-тесты эвристики LSP-H-001
# Детектор: raise NotImplementedError в переопределенном методе
# ---------------------------------------------------------------------------

import pytest

from solid_dashboard.llm.heuristics import lsp_h_001
from solid_dashboard.llm.types import ClassInfo, ProjectMap

from .conftest import _class_info_node_and_project_map_from_source


class TestLspH001:
    def test_positive_bare_raise(self):
        """Переопределенный метод бросает NotImplementedError без аргументов."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        findings = lsp_h_001.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-001" in rules

    def test_positive_raise_with_message(self):
        """Переопределенный метод бросает NotImplementedError с сообщением."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError("not supported in this subclass")
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        findings = lsp_h_001.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-001" in rules

    def test_negative_non_override_method(self):
        """Метод НЕ является переопределением — эвристика не срабатывает."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class StandaloneUtil:
                def helper(self):
                    raise NotImplementedError("to be implemented by subclasses")
            """,
            "StandaloneUtil",
        )
        findings = lsp_h_001.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-001" not in rules

    def test_negative_other_exception(self):
        """Бросает другое исключение — эвристика молчит."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def run(self):
                    raise ValueError("invalid input")
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        findings = lsp_h_001.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-001" not in rules

    def test_finding_metadata(self):
        """Finding содержит корректные метаданные."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def process(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["process"],
        )
        findings = lsp_h_001.check(class_node, class_info, pm)
        finding = next(f for f in findings if f.rule == "LSP-H-001")

        assert finding.source == "heuristic"
        assert finding.severity == "warning"
        assert finding.class_name == "Child"
        assert finding.details is not None
        assert finding.details.principle == "LSP"
        assert "process" in finding.message

    def test_abstract_method_without_abc_base_is_ignored(self):
        """Класс с @abstractmethod и NotImplementedError не дает LSP-H-001."""
        source = """
            from abc import abstractmethod

            class BaseAdapter:
                @abstractmethod
                def process(self, value: int) -> str:
                    raise NotImplementedError
        """
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            source=source,
            class_name="BaseAdapter",
            override_methods=["process"],
        )
        findings = lsp_h_001.check(class_node, class_info, pm)
        lsp_h001_findings = [f for f in findings if f.rule == "LSP-H-001"]
        assert lsp_h001_findings == []
