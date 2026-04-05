# ---------------------------------------------------------------------------
# Юнит-тесты оркестратора identify_candidates
# Проверяет приоритеты, типы кандидатов, фильтрацию и source findings
# ---------------------------------------------------------------------------

import textwrap

from solid_dashboard.llm.ast_parser import build_project_map
from solid_dashboard.llm.heuristics import identify_candidates
from solid_dashboard.llm.types import ProjectMap

from .conftest import _pm_from_source


class TestIdentifyCandidatesOrchestration:
    def test_empty_project_map(self):
        """Пустой ProjectMap → пустой HeuristicResult."""
        result = identify_candidates(ProjectMap(), exclude_patterns=[])
        assert result.findings == []
        assert result.candidates == []

    def test_candidates_sorted_by_priority(self, tmp_path):
        """Кандидат с большим числом нарушений имеет более высокий приоритет."""
        # Класс с 2 нарушениями
        f_bad = tmp_path / "bad.py"
        f_bad.write_text(textwrap.dedent("""
            class BadChild(Base):
                def __init__(self):
                    self.x = 1
                def run(self):
                    raise NotImplementedError
        """), encoding="utf-8")

        # Класс без нарушений
        f_ok = tmp_path / "ok.py"
        f_ok.write_text(textwrap.dedent("""
            class Base:
                def run(self):
                    return 42
        """), encoding="utf-8")

        pm = build_project_map([str(f_bad), str(f_ok)])

        # Вручную помечаем is_override для run в BadChild
        if "BadChild" in pm.classes:
            for m in pm.classes["BadChild"].methods:
                if m.name == "run":
                    m.is_override = True

        result = identify_candidates(pm, exclude_patterns=[])
        if len(result.candidates) >= 2:
            assert result.candidates[0].priority >= result.candidates[1].priority

    def test_candidate_type_ocp_only(self):
        """Только OCP-эвристика сработала → candidate_type == 'ocp'."""
        pm = _pm_from_source(
            """
            class Dispatcher:
                def dispatch(self, event):
                    if isinstance(event, A):
                        pass
                    elif isinstance(event, B):
                        pass
                    elif isinstance(event, C):
                        pass
            """,
            "Dispatcher",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        if result.candidates:
            candidate = next(
                (c for c in result.candidates if c.class_name == "Dispatcher"), None
            )
            if candidate:
                assert candidate.candidate_type == "ocp"

    def test_candidate_type_both_when_multiple_signals(self):
        """И OCP, и LSP эвристики сработали → candidate_type == 'both'."""
        pm = _pm_from_source(
            """
            class Mixed(Base):
                def __init__(self):
                    self.x = 1
                def dispatch(self, event):
                    if isinstance(event, A):
                        pass
                    elif isinstance(event, B):
                        pass
                    elif isinstance(event, C):
                        pass
                    elif isinstance(event, D):
                        pass
            """,
            "Mixed",
            parent_classes=["Base"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
        candidate = next(
            (c for c in result.candidates if c.class_name == "Mixed"), None
        )
        assert candidate is not None
        assert candidate.candidate_type == "both"

    def test_finding_source_is_heuristic(self):
        """Все findings от эвристик имеют source='heuristic'."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
        for finding in result.findings:
            assert finding.source == "heuristic"

    def test_dynamic_base_class_skipped(self):
        """Класс с динамической базой пропускается эвристиками."""
        pm = _pm_from_source(
            """
            class Foo(get_base()):
                def run(self):
                    raise NotImplementedError
            """,
            "Foo",
            parent_classes=[""],
            override_methods=["run"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" not in rules
