# ---------------------------------------------------------------------------
# Юнит-тесты эвристики LSP-H-002
# Детектор: пустое тело переопределенного метода (pass / docstring-only / ...)
# ---------------------------------------------------------------------------

import pytest

from solid_dashboard.llm.heuristics import lsp_h_002

from .conftest import _class_info_node_and_project_map_from_source


class TestLspH002:
    def test_positive_pass_body(self):
        """Переопределенный метод содержит только pass."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def save(self):
                    pass
            """,
            "Child",
            parent_classes=["Base"],
            override_methods=["save"],
        )
        findings = lsp_h_002.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-002" in rules

    def test_positive_docstring_only_body(self):
        """Переопределенный метод содержит только docstring."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def save(self):
                    "Not needed here."
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        findings = lsp_h_002.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-002" in rules

    def test_negative_method_with_body(self):
        """Переопределенный метод имеет реализацию — эвристика молчит."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class Child(Base):
                def save(self, data):
                    self._storage.write(data)
                    return True
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        findings = lsp_h_002.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-002" not in rules

    def test_negative_non_override_pass(self):
        """Метод с pass, но НЕ является переопределением — эвристика молчит."""
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            """
            class NewClass:
                def placeholder(self):
                    pass
            """,
            "NewClass",
        )
        findings = lsp_h_002.check(class_node, class_info, pm)
        rules = [f.rule for f in findings]
        assert "LSP-H-002" not in rules

    def test_abstract_base_class_with_pass_is_ignored(self):
        """Абстрактный класс с pass-телом не должен давать finding."""
        source = """
            from abc import ABC, abstractmethod

            class BaseHandler(ABC):
                @abstractmethod
                def handle(self, value: int) -> None:
                    pass
        """
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            source=source,
            class_name="BaseHandler",
            parent_classes=["ABC"],
            override_methods=["handle"],
        )
        findings = lsp_h_002.check(class_node, class_info, pm)
        lsp_h002_findings = [f for f in findings if f.rule == "LSP-H-002"]
        assert lsp_h002_findings == []

    def test_abstract_method_with_pass_is_ignored(self):
        """Класс с @abstractmethod и pass-телом не дает LSP-H-002."""
        source = """
            from abc import abstractmethod

            class BaseHandler:
                @abstractmethod
                def handle(self, value: int) -> None:
                    pass
        """
        class_info, class_node, pm = _class_info_node_and_project_map_from_source(
            source=source,
            class_name="BaseHandler",
            override_methods=["handle"],
        )
        findings = lsp_h_002.check(class_node, class_info, pm)
        lsp_h002_findings = [f for f in findings if f.rule == "LSP-H-002"]
        assert lsp_h002_findings == []
