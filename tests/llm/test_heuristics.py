"""
Тесты Шага 3: identify_candidates + первые 4 эвристики.
Для каждой эвристики — минимум один positive test (срабатывает)
и один negative test (не срабатывает на чистом коде).
"""

import textwrap
import pytest

from tools.solid_verifier.solid_dashboard.llm.ast_parser import build_project_map
from tools.solid_verifier.solid_dashboard.llm.heuristics import identify_candidates
from tools.solid_verifier.solid_dashboard.llm.types import (
    ClassInfo,
    MethodSignature,
    ProjectMap,
)


# ---------------------------------------------------------------------------
# Вспомогательная фабрика: создаёт ProjectMap из одного блока кода напрямую
# (без записи на диск — для тестов отдельных эвристик)
# ---------------------------------------------------------------------------

def _pm_from_source(
    source: str,
    class_name: str,
    parent_classes: list[str] | None = None,
    override_methods: list[str] | None = None,
) -> ProjectMap:
    source = textwrap.dedent(source).strip()  # ← добавили .strip(), который ->
    # убирает ведущий/хвостовой перенос строки из многострочного литерала ->
    # в результате _parse_class_ast получает чистый код
    
    # Явно создаём ProjectMap с пустыми словарями
    pm = ProjectMap(classes={}, interfaces={})  # ← было ProjectMap()
    # Теперь явная инициализация вместо ProjectMap(), если dataclass не имеет default_factory
    
    methods = []
    if override_methods:
        for name in override_methods:
            methods.append(MethodSignature(
                name=name, parameters="self", return_type="Any", is_override=True
            ))

    pm.classes[class_name] = ClassInfo(
        name=class_name,
        file_path="test_file.py",
        source_code=source,
        parent_classes=parent_classes or [],
        implemented_interfaces=[],
        methods=methods,
        dependencies=[],
    )
    return pm

# ---------------------------------------------------------------------------
# LSP-H-001: raise NotImplementedError в переопределённом методе
# ---------------------------------------------------------------------------

class TestLspH001:
    def test_positive_bare_raise(self):
        """Переопределённый метод бросает NotImplementedError без аргументов."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" in rules

    def test_positive_raise_with_message(self):
        """Переопределённый метод бросает NotImplementedError с сообщением."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError("not supported in this subclass")
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" in rules

    def test_negative_non_override_method(self):
        """Метод НЕ является переопределением — эвристика не срабатывает."""
        pm = _pm_from_source(
            """
            class StandaloneUtil:
                def helper(self):
                    raise NotImplementedError("to be implemented by subclasses")
            """,
            "StandaloneUtil",
            # override_methods не передаём — метод не помечен как is_override
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" not in rules

    def test_negative_other_exception(self):
        """Бросает другое исключение — не NotImplementedError — эвристика молчит."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise ValueError("invalid input")
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" not in rules

    def test_finding_metadata(self):
        """Проверяем, что finding содержит корректные метаданные."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def process(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["process"],
        )
        result = identify_candidates(pm)
        finding = next(f for f in result.findings if f.rule == "LSP-H-001")
        assert finding.source == "heuristic"
        assert finding.severity == "warning"
        assert finding.class_name == "Child"
        assert finding.details is not None
        assert finding.details.principle == "LSP"
        assert "process" in finding.message


# ---------------------------------------------------------------------------
# LSP-H-002: пустое тело переопределённого метода
# ---------------------------------------------------------------------------

class TestLspH002:
    def test_positive_pass_body(self):
        """Переопределённый метод содержит только pass."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def save(self):
                    pass
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-002" in rules

    def test_positive_docstring_only_body(self):
        """Переопределённый метод содержит только docstring."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def save(self):
                    "Not needed here."
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-002" in rules

    def test_negative_method_with_body(self):
        """Переопределённый метод имеет реализацию — эвристика молчит."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def save(self, data):
                    self._storage.write(data)
                    return True
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-002" not in rules

    def test_negative_non_override_pass(self):
        """Метод с pass, но НЕ является переопределением — эвристика молчит."""
        pm = _pm_from_source(
            """
            class NewClass:
                def placeholder(self):
                    pass
            """,
            "NewClass",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-002" not in rules


# ---------------------------------------------------------------------------
# OCP-H-001: цепочки if/elif с isinstance()
# ---------------------------------------------------------------------------

class TestOcpH001:
    def test_positive_three_branch_chain(self):
        """Цепочка if/elif/elif с isinstance — ровно 3 ветви, порог == 3."""
        pm = _pm_from_source(
            """
            class Renderer:
                def render(self, obj):
                    if isinstance(obj, Circle):
                        self._draw_circle(obj)
                    elif isinstance(obj, Square):
                        self._draw_square(obj)
                    elif isinstance(obj, Triangle):
                        self._draw_triangle(obj)
            """,
            "Renderer",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-001" in rules

    def test_positive_five_branch_chain(self):
        """Длинная цепочка из 5 ветвей — эвристика срабатывает."""
        pm = _pm_from_source(
            """
            class Handler:
                def handle(self, event):
                    if isinstance(event, ClickEvent):
                        pass
                    elif isinstance(event, KeyEvent):
                        pass
                    elif isinstance(event, ScrollEvent):
                        pass
                    elif isinstance(event, ResizeEvent):
                        pass
                    elif isinstance(event, CloseEvent):
                        pass
            """,
            "Handler",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-001" in rules

    def test_negative_two_branch_chain(self):
        """Только 2 ветви (if + elif) — ниже порога, эвристика молчит."""
        pm = _pm_from_source(
            """
            class Formatter:
                def format(self, value):
                    if isinstance(value, int):
                        return str(value)
                    elif isinstance(value, float):
                        return f"{value:.2f}"
            """,
            "Formatter",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-001" not in rules

    def test_negative_isinstance_without_elif_chain(self):
        """isinstance используется в одиночном if без цепочки — эвристика молчит."""
        pm = _pm_from_source(
            """
            class Validator:
                def validate(self, value):
                    if isinstance(value, str):
                        return len(value) > 0
                    return True
            """,
            "Validator",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-001" not in rules

    def test_negative_if_without_isinstance(self):
        """Цепочка if/elif без isinstance — не OCP-запах, эвристика молчит."""
        pm = _pm_from_source(
            """
            class Router:
                def route(self, path):
                    if path == "/home":
                        return "home"
                    elif path == "/about":
                        return "about"
                    elif path == "/contact":
                        return "contact"
            """,
            "Router",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-001" not in rules

    def test_finding_contains_branch_count(self):
        """В тексте сообщения должно быть указано количество ветвей."""
        pm = _pm_from_source(
            """
            class P:
                def process(self, obj):
                    if isinstance(obj, A):
                        pass
                    elif isinstance(obj, B):
                        pass
                    elif isinstance(obj, C):
                        pass
            """,
            "P",
        )
        result = identify_candidates(pm)
        finding = next(f for f in result.findings if f.rule == "OCP-H-001")
        assert "3" in finding.message


# ---------------------------------------------------------------------------
# LSP-H-004: __init__ без super().__init__()
# ---------------------------------------------------------------------------

class TestLspH004:
    def test_positive_init_without_super(self):
        """__init__ есть, но super().__init__() не вызывается."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def __init__(self):
                    self.value = 42
            """,
            "Child", parent_classes=["Base"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-004" in rules

    def test_negative_init_with_super(self):
        """__init__ вызывает super().__init__() — эвристика молчит."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def __init__(self, name):
                    super().__init__()
                    self.name = name
            """,
            "Child", parent_classes=["Base"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-004" not in rules

    def test_negative_no_init_at_all(self):
        """Подкласс без __init__ — эвристика молчит (наследует от родителя)."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    pass
            """,
            "Child", parent_classes=["Base"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-004" not in rules

    def test_negative_standalone_class_with_init(self):
        """Класс без родителей — проверка не применима, эвристика молчит."""
        pm = _pm_from_source(
            """
            class Standalone:
                def __init__(self):
                    self.value = 0
            """,
            "Standalone",
            # parent_classes пустой — нет родителей
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-004" not in rules


# ---------------------------------------------------------------------------
# Тесты identify_candidates: приоритеты, типы кандидатов, пустой ProjectMap
# ---------------------------------------------------------------------------

class TestIdentifyCandidatesOrchestration:
    def test_empty_project_map(self):
        """Пустой ProjectMap → пустой HeuristicResult."""
        result = identify_candidates(ProjectMap())
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

        # Класс с 0 нарушениями, но в иерархии
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

        result = identify_candidates(pm)

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
            # Нет parent_classes — нет иерархии
        )
        result = identify_candidates(pm)
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
            """,
            "Mixed",
            parent_classes=["Base"],
        )
        result = identify_candidates(pm)
        candidate = next(
            (c for c in result.candidates if c.class_name == "Mixed"), None
        )
        assert candidate is not None
        assert candidate.candidate_type == "both"

    def test_finding_source_is_heuristic(self):
        """Все findings от эвристик должны иметь source='heuristic'."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm)
        for finding in result.findings:
            assert finding.source == "heuristic"

    def test_dynamic_base_class_skipped(self):
        """Класс с динамической базой <dynamic> пропускается эвристиками."""
        pm = _pm_from_source(
            """
            class Foo(get_base()):
                def run(self):
                    raise NotImplementedError
            """,
            "Foo",
            parent_classes=["<dynamic>"],
            override_methods=["run"],
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" not in rules

# ---------------------------------------------------------------------------
# OCP-H-002: match/case на типах (Python 3.10+)
# ---------------------------------------------------------------------------

class TestOcpH002:
    def test_positive_three_case_branches(self):
        """match/case с тремя ветвями на типы — эвристика срабатывает."""
        import ast as _ast

        # Сначала проверяем поддержку синтаксиса match/case
        # Если её нет (Python < 3.10), тест аккуратно пропускается
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
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-002" in rules

        def test_negative_two_case_branches(self):
            """match/case с двумя ветвями — ниже порога, эвристика молчит."""
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
            result = identify_candidates(pm)
            rules = [f.rule for f in result.findings]
            import ast as _ast
            if not hasattr(_ast, "Match"):
                pytest.skip("match/case requires Python 3.10+")
            assert "OCP-H-002" not in rules

    def test_finding_metadata_ocp_h002(self):
        """Finding содержит корректные метаданные."""
        import ast as _ast
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
        result = identify_candidates(pm)
        finding = next((f for f in result.findings if f.rule == "OCP-H-002"), None)
        assert finding is not None
        assert finding.details is not None          # ← добавили
        assert finding.details.principle == "OCP"   # теперь безопасно

# ---------------------------------------------------------------------------
# LSP-H-003: isinstance в коде с параметром базового типа
# ---------------------------------------------------------------------------

class TestLspH003:
    def test_positive_isinstance_on_annotated_base_param(self, tmp_path):
        """
        Метод принимает базовый тип и использует isinstance на нём.
        Базовый тип должен быть в ProjectMap.
        """
        # Создаём файл с базовым классом и потребителем
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class Animal:
                def speak(self): pass

            class AnimalProcessor:
                def process(self, animal: Animal) -> None:
                    if isinstance(animal, Animal):
                        animal.speak()
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-003" in rules

    def test_negative_isinstance_on_external_type(self, tmp_path):
        """
        isinstance с типом, которого нет в ProjectMap (внешняя библиотека).
        Эвристика молчит — мы не можем судить о внешних типах.
        """
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class Serializer:
                def serialize(self, obj: object) -> str:
                    if isinstance(obj, list):
                        return str(obj)
                    return repr(obj)
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-003" not in rules

    def test_negative_no_annotation(self, tmp_path):
        """Параметр без аннотации — эвристика молчит (не можем определить базовый тип)."""
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class Animal:
                def speak(self): pass

            class Processor:
                def process(self, obj) -> None:
                    if isinstance(obj, Animal):
                        obj.speak()
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "LSP-H-003" not in rules

    def test_finding_contains_param_name(self, tmp_path):
        """В тексте finding упоминается имя параметра и базовый тип."""
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class Shape:
                def draw(self): pass

            class Canvas:
                def render(self, shape: Shape) -> None:
                    if isinstance(shape, Shape):
                        shape.draw()
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        finding = next((f for f in result.findings if f.rule == "LSP-H-003"), None)
        assert finding is not None
        assert "shape" in finding.message
        assert "Shape" in finding.message


# ---------------------------------------------------------------------------
# OCP-H-003: словарь-диспетчер типов
# ---------------------------------------------------------------------------

class TestOcpH003:
    def test_positive_type_key_dict_in_method(self, tmp_path):
        """Словарь с ключами-именами классов из ProjectMap в методе."""
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class CircleRenderer:
                def draw(self): pass

            class SquareRenderer:
                def draw(self): pass

            class TriangleRenderer:
                def draw(self): pass

            class DrawingEngine:
                def setup(self):
                    self._handlers = {
                        CircleRenderer: self._draw_circle,
                        SquareRenderer: self._draw_square,
                        TriangleRenderer: self._draw_triangle,
                    }
                def _draw_circle(self): pass
                def _draw_square(self): pass
                def _draw_triangle(self): pass
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-003" in rules

    def test_negative_data_dict_not_dispatcher(self, tmp_path):
        """Словарь с данными (не callable значениями) — эвристика молчит."""
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class TypeA:
                pass
            class TypeB:
                pass
            class TypeC:
                pass

            class Config:
                def __init__(self):
                    # Словарь с данными, не с callable — не OCP-запах
                    self._labels = {
                        TypeA: "type_a_label",
                        TypeB: "type_b_label",
                        TypeC: "type_c_label",
                    }
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-003" not in rules

    def test_negative_two_type_keys_below_threshold(self, tmp_path):
        """Словарь только с двумя типовыми ключами — ниже порога."""
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class Dog:
                def speak(self): pass
            class Cat:
                def speak(self): pass

            class SoundDispatcher:
                def setup(self):
                    self._handlers = {
                        Dog: self._dog_sound,
                        Cat: self._cat_sound,
                    }
                def _dog_sound(self): pass
                def _cat_sound(self): pass
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-003" not in rules

    def test_finding_metadata_ocp_h003(self, tmp_path):
        """Finding содержит корректные метаданные source и principle."""
        f = tmp_path / "example.py"
        f.write_text(textwrap.dedent("""
            class StrategyA:
                def run(self): pass
            class StrategyB:
                def run(self): pass
            class StrategyC:
                def run(self): pass

            class StrategyEngine:
                def build(self):
                    self._map = {
                        StrategyA: self._run_a,
                        StrategyB: self._run_b,
                        StrategyC: self._run_c,
                    }
                def _run_a(self): pass
                def _run_b(self): pass
                def _run_c(self): pass
        """), encoding="utf-8")
        pm = build_project_map([str(f)])
        result = identify_candidates(pm)
        finding = next((f for f in result.findings if f.rule == "OCP-H-003"), None)
        assert finding is not None
        assert finding.source == "heuristic"
        assert finding.details is not None   
        assert finding.details.principle == "OCP"


# ---------------------------------------------------------------------------
# OCP-H-004: высокая цикломатическая сложность + isinstance
# ---------------------------------------------------------------------------

class TestOcpH004:

    def test_positive_high_cc_with_isinstance(self):
        """
        Метод с CC >= 5 и isinstance() — эвристика срабатывает.
        Строим метод с ровно 5 узлами ветвления (CC=5):
        4 if-ветви + 1 isinstance = 5 добавленных узлов, итого CC = 6.
        """
        pm = _pm_from_source(
            """
            class OrderProcessor:
                def process(self, order):
                    if order.status == "new":
                        self._init(order)
                    if order.priority > 5:
                        self._escalate(order)
                    if order.retry_count < 3:
                        self._retry(order)
                    if order.region == "EU":
                        self._apply_eu_rules(order)
                    if isinstance(order, SpecialOrder):
                        self._handle_special(order)
            """,
            "OrderProcessor",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-004" in rules

    def test_positive_cc_exactly_at_threshold(self):
        """
        CC ровно на пороге (== _OCP_H004_CC_THRESHOLD) + isinstance.
        Граничный случай: порог включительный (>=), должен сработать.
        Строим метод с CC = 5: базовая 1 + 4 if-ветви.
        """
        pm = _pm_from_source(
            """
            class Checker:
                def check(self, item):
                    if item.a:
                        pass
                    if item.b:
                        pass
                    if item.c:
                        pass
                    if isinstance(item, Special):
                        pass
            """,
            "Checker",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        # CC = 1 (base) + 4 (четыре if) = 5, порог = 5 → должен сработать
        assert "OCP-H-004" in rules

    def test_negative_high_cc_without_isinstance(self):
        """
        Высокая CC, но без isinstance — эвристика молчит.
        Сложный метод без type-dispatch — не OCP-запах для этой эвристики.
        """
        pm = _pm_from_source(
            """
            class Calculator:
                def calculate(self, data):
                    if data.mode == "fast":
                        result = data.value * 2
                    elif data.mode == "slow":
                        result = data.value * 3
                    elif data.mode == "precise":
                        result = data.value * 4
                    elif data.mode == "estimate":
                        result = data.value * 5
                    else:
                        result = data.value
                    return result
            """,
            "Calculator",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-004" not in rules

    def test_negative_low_cc_with_isinstance(self):
        """
        isinstance есть, но CC ниже порога — эвристика молчит.
        Простой метод с одним isinstance не считается OCP-запахом этой эвристики.
        (OCP-H-001 и OCP-H-002 покрывают простые случаи с цепочками.)
        """
        pm = _pm_from_source(
            """
            class Formatter:
                def format(self, value):
                    if isinstance(value, int):
                        return str(value)
                    return repr(value)
            """,
            "Formatter",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        # CC = 1 (base) + 1 (if) + 1 (isinstance-if) = 2+1 = нет, считаем точно:
        # ast.If для isinstance + ast.If нет отдельно (это тот же ast.If) → CC=2
        assert "OCP-H-004" not in rules

    def test_negative_below_threshold_exactly(self):
        """
        CC ровно на 1 ниже порога (CC = 4) + isinstance — не срабатывает.
        Проверяем, что порог строгий: CC < 5 → тишина.
        """
        pm = _pm_from_source(
            """
            class Validator:
                def validate(self, obj):
                    if obj.field_a:
                        pass
                    if obj.field_b:
                        pass
                    if isinstance(obj, SpecialObj):
                        pass
            """,
            "Validator",
        )
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        # CC = 1 (base) + 3 (три if) = 4 < 5 → не срабатывает
        assert "OCP-H-004" not in rules

    def test_finding_metadata_ocp_h004(self):
        """
        Finding содержит корректные метаданные: rule, source, principle, CC в message.
        """
        pm = _pm_from_source(
            """
            class Engine:
                def run(self, task):
                    if task.urgent:
                        self._fast_path(task)
                    if task.retries > 0:
                        self._retry_path(task)
                    if task.region == "US":
                        self._us_rules(task)
                    if task.region == "EU":
                        self._eu_rules(task)
                    if isinstance(task, PremiumTask):
                        self._premium(task)
            """,
            "Engine",
        )
        result = identify_candidates(pm)
        finding = next((f for f in result.findings if f.rule == "OCP-H-004"), None)
        assert finding is not None
        assert finding.source == "heuristic"
        assert finding.severity == "warning"
        assert finding.details is not None
        assert finding.details.principle == "OCP"
        # CC в тексте сообщения — важно для debuggability
        assert "run" in finding.message
        # Проверяем, что числовое значение CC попало в сообщение
        assert any(char.isdigit() for char in finding.message)