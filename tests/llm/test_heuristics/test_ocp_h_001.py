# ---------------------------------------------------------------------------
# Юнит-тесты эвристики OCP-H-001
# Детектор: верхнеуровневая if/elif-цепочка с isinstance (>=3 ветки)
# ---------------------------------------------------------------------------

import ast
import textwrap

from solid_dashboard.llm.heuristics import ocp_h_001
from solid_dashboard.llm.heuristics.ocp_h_001 import _count_isinstance_branches
from solid_dashboard.llm.types import ClassInfo

from .conftest import _pm_from_source


def test_count_isinstance_branches_helper():
    """Хелпер корректно считает только ветки с isinstance."""
    code = textwrap.dedent("""
        if isinstance(x, A):
            pass
        elif x == 1:
            pass
        elif isinstance(x, B):
            pass
        elif isinstance(x, C):
            pass
        else:
            pass
    """)
    tree = ast.parse(code)
    if_node = tree.body[0]
    assert isinstance(if_node, ast.If)
    # 4 ветки всего, isinstance только в 3 из них
    assert _count_isinstance_branches(if_node) == 3


class TestOcpH001Updated:
    def setup_method(self):
        self.class_info = ClassInfo(
            name="Processor",
            file_path="processor.py",
            source_code="",
            parent_classes=[],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )

    def _get_class_node(self, code: str) -> ast.ClassDef:
        tree = ast.parse(textwrap.dedent(code))
        node = tree.body[0]
        assert isinstance(node, ast.ClassDef)
        return node

    def test_pure_isinstance_chain_triggers(self):
        """Классическая ситуация: 3 подряд isinstance."""
        code = """
            class Processor:
                def process(self, x):
                    if isinstance(x, A): pass
                    elif isinstance(x, B): pass
                    elif isinstance(x, C): pass
        """
        node = self._get_class_node(code)
        findings = ocp_h_001.check(node, self.class_info)
        assert len(findings) == 1
        assert findings[0].rule == "OCP-H-001"
        assert "3 isinstance checks" in findings[0].message

    def test_guard_plus_isinstance_chain_triggers(self):
        """
        ПРЕДОТВРАЩЕНИЕ FALSE NEGATIVE:
        Цепочка начинается с обычного условия (guard),
        дальше идут 3 isinstance — эвристика должна найти.
        """
        code = """
            class Processor:
                def process(self, x):
                    if not x: return
                    elif isinstance(x, A): pass
                    elif isinstance(x, B): pass
                    elif isinstance(x, C): pass
        """
        node = self._get_class_node(code)
        findings = ocp_h_001.check(node, self.class_info)
        assert len(findings) == 1
        assert "3 isinstance checks" in findings[0].message

        assert findings[0].details is not None
        expl = findings[0].details.explanation
        assert expl is not None
        assert "length 4" in expl
        assert "3 branches check concrete types" in expl

    def test_status_checks_with_one_isinstance_no_trigger(self):
        """
        ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE:
        Длинная цепочка (>=3), но isinstance только в одной ветке.
        """
        code = """
            class Processor:
                def process(self, x):
                    if isinstance(x, Special): pass
                    elif x.status == 1: pass
                    elif x.status == 2: pass
                    elif x.status == 3: pass
        """
        node = self._get_class_node(code)
        findings = ocp_h_001.check(node, self.class_info)
        assert len(findings) == 0
