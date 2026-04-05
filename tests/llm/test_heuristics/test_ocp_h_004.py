# ---------------------------------------------------------------------------
# Юнит-тесты эвристики OCP-H-004
# Детектор: высокая цикломатическая сложность + isinstance в методе
# Включает TestCcNestedScopes — изоляция подсчета CC от вложенных областей
# ---------------------------------------------------------------------------

import ast
import textwrap

from solid_dashboard.llm.heuristics import ocp_h_004, identify_candidates
from solid_dashboard.llm.types import ClassInfo

from .conftest import _pm_from_source


class TestOcpH004Updated:
    def setup_method(self):
        self.class_info = ClassInfo(
            name="ComplexProcessor",
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

    def test_ocp_h_004_nested_isinstance_ignored(self):
        """
        ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE:
        isinstance() внутри вложенной функции не засчитывается
        в CC внешнего метода.
        """
        code = """
            class ComplexProcessor:
                def process(self, x):
                    if x > 1: pass
                    if x > 2: pass
                    if x > 3: pass
                    if x > 4: pass

                    def inner_helper(y):
                        if isinstance(y, int):
                            pass
        """
        node = self._get_class_node(code)
        findings = ocp_h_004.check(node, self.class_info)
        assert len(findings) == 0

    def test_ocp_h_004_direct_isinstance_triggers(self):
        """
        isinstance() непосредственно в теле метода с высокой CC
        корректно триггерит OCP-H-004.
        """
        code = """
            class ComplexProcessor:
                def process(self, x):
                    if x > 1: pass
                    if x > 2: pass
                    if x > 3: pass
                    if x > 4: pass

                    if isinstance(x, int):
                        pass
        """
        node = self._get_class_node(code)
        findings = ocp_h_004.check(node, self.class_info)
        assert len(findings) == 1
        assert findings[0].rule == "OCP-H-004"


class TestCcNestedScopes:
    """
    Проверяет, что _compute_method_cc и OCP-H-004 не засчитывают ветвления
    из вложенных функций и классов как часть CC внешнего метода.

    Без корректного обхода OCP-H-004 выдавал бы ложные срабатывания на
    любом методе с вложенной функцией с разветвленной логикой.
    """

    def test_nested_function_if_not_counted(self):
        """
        Вложенная функция с if — не влияет на CC внешнего метода.
        process() имеет CC=2, не CC=3.
        """
        pm = _pm_from_source(
            """
            class Wrapper:
                def process(self, x):
                    if x > 0:
                        pass
                    def inner(y):
                        if y < 0:
                            pass
            """,
            "Wrapper",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        # CC process() = 1 + 1 = 2 < порога 5 → OCP-H-004 не срабатывает
        assert "OCP-H-004" not in rules

    def test_nested_async_function_not_counted(self):
        """Async-вложенная функция с множеством ветвлений не влияет на CC."""
        pm = _pm_from_source(
            """
            class AsyncHandler:
                def build_task(self, x):
                    if x is None:
                        pass
                    async def _worker(item):
                        if item.a: pass
                        if item.b: pass
                        if item.c: pass
                        if item.d: pass
                        if isinstance(item, object): pass
            """,
            "AsyncHandler",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        # CC build_task() = 1 + 1 = 2 (только внешний if)
        assert "OCP-H-004" not in rules

    def test_nested_class_not_counted(self):
        """Вложенный класс с методами не влияет на CC внешнего метода."""
        pm = _pm_from_source(
            """
            class Factory:
                def make(self, config):
                    if config:
                        pass
                    class _Impl:
                        def run(self):
                            if True: pass
                            if True: pass
                            if True: pass
                            if isinstance(self, object): pass
            """,
            "Factory",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        # CC make() = 1 + 1 = 2
        assert "OCP-H-004" not in rules

    def test_high_cc_in_outer_method_still_triggers(self):
        """
        Внешний метод с высокой CC + isinstance срабатывает
        даже при наличии вложенной функции.
        """
        pm = _pm_from_source(
            """
            class Processor:
                def process(self, item):
                    if item.a: pass
                    if item.b: pass
                    if item.c: pass
                    if isinstance(item, SomeClass): pass
                    def helper():
                        pass
            """,
            "Processor",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        # CC process() = 1 + 4 = 5 >= порога, isinstance есть
        assert "OCP-H-004" in rules

    def test_cc_counts_only_outer_elif_chain(self):
        """
        elif-цепочка снаружи считается; elif-цепочка
        внутри вложенной функции — нет.
        """
        pm = _pm_from_source(
            """
            class Router:
                def route(self, req):
                    if req.method == "GET":
                        pass
                    elif req.method == "POST":
                        pass
                    elif req.method == "PUT":
                        pass
                    elif isinstance(req, SpecialReq):
                        pass
                    def _extra(x):
                        if x == 1: pass
                        elif x == 2: pass
                        elif x == 3: pass
                        elif x == 4: pass
            """,
            "Router",
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        # CC route() = 1 + 4 = 5 >= порога, isinstance есть во внешней цепочке
        assert "OCP-H-004" in rules
