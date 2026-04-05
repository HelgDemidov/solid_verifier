# ---------------------------------------------------------------------------
# Юнит-тесты эвристики OCP-H-002
# Детектор: match/case как диспетчер типов (Python 3.10+)
# ---------------------------------------------------------------------------

import ast as _ast

import pytest

from solid_dashboard.llm.heuristics import identify_candidates

from .conftest import _pm_from_source


class TestOcpH002:
    def test_positive_three_case_branches(self):
        """match/case с тремя ветвями на типы — эвристика срабатывает."""
        if not hasattr(_ast, "Match"):
            pytest.skip("match/case requires Python 3.10+")

        pm = _pm_from_source(
            """
            class EventProcessor:
                def handle(self, event):
                    match event:
                        case ClickEvent():
                            self._on_click(event)
                        case KeyEvent():
                            self._on_key(event)
                        case ScrollEvent():
                            self._on_scroll(event)
            """,
            "EventProcessor",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        assert "OCP-H-002" in rules

    def test_negative_two_case_branches(self):
        """match/case с двумя ветвями — ниже порога, эвристика молчит."""
        if not hasattr(_ast, "Match"):
            pytest.skip("match/case requires Python 3.10+")

        pm = _pm_from_source(
            """
            class SmallSwitch:
                def handle(self, event):
                    match event:
                        case ClickEvent():
                            pass
                        case KeyEvent():
                            pass
            """,
            "SmallSwitch",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        assert "OCP-H-002" not in rules

    def test_finding_metadata_ocp_h002(self):
        """Finding содержит корректные метаданные."""
        if not hasattr(_ast, "Match"):
            pytest.skip("match/case requires Python 3.10+")

        pm = _pm_from_source(
            """
            class Dispatcher:
                def route(self, cmd):
                    match cmd:
                        case CmdA():
                            pass
                        case CmdB():
                            pass
                        case CmdC():
                            pass
            """,
            "Dispatcher",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        finding = next((f for f in result.findings if f.rule == "OCP-H-002"), None)
        assert finding is not None
        assert finding.details is not None
        assert finding.details.principle == "OCP"
