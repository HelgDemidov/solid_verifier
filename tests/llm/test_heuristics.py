
# ---------------------------------------------------------------------------
# НАПОМИНАЛКА ПО КОМАНДАМ ЗАПУСКА ТЕСТОВ
# ---------------------------------------------------------------------------

# Только unit-тесты (быстро): pytest tools/solid_verifier/tests/llm/test_heuristics.py -v
# Только интеграционные тесты: pytest -m integration -v
# Все вместе: pytest tools/solid_verifier/tests/llm/ -v

import textwrap
import pytest
from pathlib import Path
import ast

from solid_dashboard.llm.ast_parser import build_project_map
from solid_dashboard.llm.heuristics import (
    _check_ocp_h_001,
    _count_isinstance_branches,
    identify_candidates,
    _check_ocp_h_004,
    _check_lsp_h_004
)
from solid_dashboard.llm.types import (
    ClassInfo,
    MethodSignature,
    ProjectMap,
)

# ---------------------------------------------------------------------------
# Вспомогательная фабрика: создает ProjectMap из одного блока кода напрямую
# (без записи на диск — для тестов отдельных эвристик)
# ---------------------------------------------------------------------------

def _pm_from_source(
    source: str,
    class_name: str,
    parent_classes: list[str] | None = None,
    override_methods: list[str] | None = None,
) -> ProjectMap:
    """
    Упрощенный helper: строит ProjectMap с одним ClassInfo по исходнику.

    parent_classes: список имен базовых классов, который мы хотим видеть
    в ClassInfo.parent_classes (например, ['ABC']).
    override_methods: список имен методов, помечаемых как is_override=True.
    """
    # Нормализуем отступы
    source_dedented = textwrap.dedent(source)

    # Создаем временный .py-файл в каталоге для тестов
    tmp_dir = Path.cwd() / ".tmp_heuristics_tests"
    tmp_dir.mkdir(exist_ok=True)
    tmp_file = tmp_dir / "tmp_module.py"
    tmp_file.write_text(source_dedented, encoding="utf-8")

    # Строим ProjectMap по пути файла
    project_map = build_project_map([str(tmp_file)])

    # Находим наш класс в полученном ProjectMap
    class_info = project_map.classes[class_name]

    # Переопределяем parent_classes, если явно переданы
    if parent_classes is not None:
        class_info = ClassInfo(
            name=class_info.name,
            file_path=class_info.file_path,
            source_code=class_info.source_code,
            parent_classes=parent_classes,
            implemented_interfaces=class_info.implemented_interfaces,
            methods=class_info.methods,
            dependencies=class_info.dependencies,
        )
        project_map.classes[class_name] = class_info

    # Отмечаем override-флаг для заданных методов (если нужно)
    if override_methods:
        new_methods: list[MethodSignature] = []
        for m in class_info.methods:
            if m.name in override_methods:
                new_methods.append(
                    MethodSignature(
                        name=m.name,
                        parameters=m.parameters,
                        return_type=m.return_type,
                        is_override=True,
                        is_abstract=m.is_abstract,  # NEW: сохраняем флаг!
                    )
                )
            else:
                new_methods.append(m)
        class_info = ClassInfo(
            name=class_info.name,
            file_path=class_info.file_path,
            source_code=class_info.source_code,
            parent_classes=class_info.parent_classes,
            implemented_interfaces=class_info.implemented_interfaces,
            methods=new_methods,
            dependencies=class_info.dependencies,
        )
        project_map.classes[class_name] = class_info

    return project_map

# ---------------------------------------------------------------------------
# LSP-H-001: raise NotImplementedError в переопределенном методе
# ---------------------------------------------------------------------------

class TestLspH001:
    def test_positive_bare_raise(self):
        """Переопределенный метод бросает NotImplementedError без аргументов."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        assert "LSP-H-001" in rules

    def test_positive_raise_with_message(self):
        """Переопределенный метод бросает NotImplementedError с сообщением."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def run(self):
                    raise NotImplementedError("not supported in this subclass")
            """,
            "Child", parent_classes=["Base"], override_methods=["run"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
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
            # override_methods не передаем — метод не помечен как is_override
        )
        result = identify_candidates(pm, exclude_patterns=[])
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
        result = identify_candidates(pm, exclude_patterns=[])
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
        result = identify_candidates(pm, exclude_patterns=[])
        finding = next(f for f in result.findings if f.rule == "LSP-H-001")
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
        pm = _pm_from_source(
            source=source,
            class_name="BaseAdapter",
            # parent_classes не задаем: нет ABC
            override_methods=["process"],
        )

        result = identify_candidates(pm, exclude_patterns=[])
        findings = [f for f in result.findings if f.rule == "LSP-H-001"]
        assert findings == []

# ---------------------------------------------------------------------------
# LSP-H-002: пустое тело переопределенного метода
# ---------------------------------------------------------------------------

class TestLspH002:
    def test_positive_pass_body(self):
        """Переопределенный метод содержит только pass."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def save(self):
                    pass
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        assert "LSP-H-002" in rules

    def test_positive_docstring_only_body(self):
        """Переопределенный метод содержит только docstring."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def save(self):
                    "Not needed here."
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
        assert "LSP-H-002" in rules

    def test_negative_method_with_body(self):
        """Переопределенный метод имеет реализацию — эвристика молчит."""
        pm = _pm_from_source(
            """
            class Child(Base):
                def save(self, data):
                    self._storage.write(data)
                    return True
            """,
            "Child", parent_classes=["Base"], override_methods=["save"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
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
        result = identify_candidates(pm, exclude_patterns=[])
        rules = [f.rule for f in result.findings]
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
        project_map = _pm_from_source(
            source=source,
            class_name="BaseHandler",
            parent_classes=["ABC"],          # помечаем как абстрактный
            override_methods=["handle"],     # handle считается override
        )

        result = identify_candidates(project_map, exclude_patterns=[])
        lsp_h002_findings = [
            f for f in result.findings if f.rule == "LSP-H-002"
        ]

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
        pm = _pm_from_source(
            source=source,
            class_name="BaseHandler",
            override_methods=["handle"],
        )

        result = identify_candidates(pm, exclude_patterns=[])
        findings = [f for f in result.findings if f.rule == "LSP-H-002"]
        assert findings == []


# ---------------------------------------------------------------------------
# Тесты для OCP-H-001 (Переработанная логика type-dispatch цепочек)
# ---------------------------------------------------------------------------

def test_count_isinstance_branches_helper():
    """Проверяем, что хелпер корректно считает только ветки с isinstance."""
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
    
    # Убеждаемся для Pylance, что if_node — это действительно ast.If
    assert isinstance(if_node, ast.If)
    # Всего 4 ветки (не считая else), но isinstance только в 3 из них
    assert _count_isinstance_branches(if_node) == 3

class TestOcpH001Updated:
    def setup_method(self):
        # Заполняем все обязательные поля, требуемые в актуальном types.ClassInfo
        self.class_info = ClassInfo(
            name="Processor",
            file_path="processor.py",
            source_code="",               # Для этих тестов сам код внутри ClassInfo не используется
            parent_classes=[],
            implemented_interfaces=[],    # Добавлено недостающее поле
            methods=[],
            dependencies=[],              # Добавлено недостающее поле
        )

    def _get_class_node(self, code: str) -> ast.ClassDef:
        tree = ast.parse(textwrap.dedent(code))
        node = tree.body[0]
        # Type Guard: убеждаемся, что распарсенная нода — это класс
        assert isinstance(node, ast.ClassDef)
        return node

    def test_pure_isinstance_chain_triggers(self):
        """Проверяем классическую ситуацию: 3 подряд isinstance."""
        code = """
        class Processor:
            def process(self, x):
                if isinstance(x, A): pass
                elif isinstance(x, B): pass
                elif isinstance(x, C): pass
        """
        node = self._get_class_node(code)
        findings = _check_ocp_h_001(node, self.class_info)

        assert len(findings) == 1
        assert findings[0].rule == "OCP-H-001"
        assert "3 isinstance checks" in findings[0].message

    def test_guard_plus_isinstance_chain_triggers(self):
        """
        ПРЕДОТВРАЩЕНИЕ FALSE NEGATIVE: 
        Цепочка начинается с обычного условия (guard), но дальше идут 3 isinstance.
        Раньше эвристика это пропускала, теперь должна находить.
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
        findings = _check_ocp_h_001(node, self.class_info)

        assert len(findings) == 1
        assert "3 isinstance checks" in findings[0].message
        
        # Type Guard для Pylance: убеждаемся, что details и explanation не None
        assert findings[0].details is not None
        expl = findings[0].details.explanation
        assert expl is not None, "Explanation must be provided"
        assert "length 4" in expl
        assert "3 branches check concrete types" in expl

    def test_status_checks_with_one_isinstance_no_trigger(self):
        """
        ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE:
        Длинная цепочка (>=3), но isinstance только в одной ветке.
        Раньше эвристика ложно срабатывала, теперь должна игнорировать.
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
        findings = _check_ocp_h_001(node, self.class_info)

        # Ложных срабатываний быть не должно
        assert len(findings) == 0


# ---------------------------------------------------------------------------
# LSP-H-004: __init__ без super().__init__() (включая dataclass и исключения)
# ---------------------------------------------------------------------------

class TestLspH004Updated:
    def setup_method(self):
        # Базовая заглушка для тестов, где есть родитель
        self.class_info = ClassInfo(
            name="Child",
            file_path="child.py",
            source_code="",
            parent_classes=["Base"], 
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )

    def _get_class_node(self, code: str) -> ast.ClassDef:
        tree = ast.parse(textwrap.dedent(code))
        node = tree.body[0]
        assert isinstance(node, ast.ClassDef)
        return node

    # --- 1. Базовые сценарии ---

    def test_positive_init_without_super(self):
        """__init__ есть, но super().__init__() не вызывается."""
        code = """
        class Child(Base):
            def __init__(self):
                self.value = 42
        """
        node = self._get_class_node(code)
        findings = _check_lsp_h_004(node, self.class_info)
        
        assert len(findings) == 1
        assert findings[0].rule == "LSP-H-004"

    def test_negative_init_with_super(self):
        """__init__ вызывает super().__init__() — эвристика молчит."""
        code = """
        class Child(Base):
            def __init__(self, name):
                super().__init__()
                self.name = name
        """
        node = self._get_class_node(code)
        findings = _check_lsp_h_004(node, self.class_info)
        assert len(findings) == 0

    def test_negative_no_init_at_all(self):
        """Подкласс без __init__ — эвристика молчит (наследует от родителя)."""
        code = """
        class Child(Base):
            def run(self):
                pass
        """
        node = self._get_class_node(code)
        findings = _check_lsp_h_004(node, self.class_info)
        assert len(findings) == 0

    def test_negative_standalone_class_with_init(self):
        """Класс без родителей — проверка не применима, эвристика молчит."""
        code = """
        class Standalone:
            def __init__(self):
                self.value = 0
        """
        node = self._get_class_node(code)
        
        # Меняем ClassInfo: убираем родителей
        standalone_info = ClassInfo(
            name="Standalone", file_path="a.py", source_code="",
            parent_classes=[], implemented_interfaces=[],
            methods=[], dependencies=[]
        )
        
        findings = _check_lsp_h_004(node, standalone_info)
        assert len(findings) == 0


    # --- 2. Сценарии с исключенными родительскими классами ---

    def test_negative_explicit_object_parent(self):
        """Явное наследование от object не должно давать LSP-H-004."""
        code = """
        class MyValueObject(object):
            def __init__(self):
                self.x = 1
        """
        node = self._get_class_node(code)
        
        info = ClassInfo(
            name="MyValueObject", file_path="a.py", source_code="",
            parent_classes=["object"], implemented_interfaces=[],
            methods=[], dependencies=[]
        )
        
        findings = _check_lsp_h_004(node, info)
        assert len(findings) == 0

    def test_negative_abc_parent(self):
        """Наследование от ABC (абстрактный класс) игнорируется."""
        code = """
        class BaseService(ABC):
            def __init__(self):
                self.x = 1
        """
        node = self._get_class_node(code)
        
        info = ClassInfo(
            name="BaseService", file_path="a.py", source_code="",
            parent_classes=["ABC"], implemented_interfaces=[],
            methods=[], dependencies=[]
        )
        
        findings = _check_lsp_h_004(node, info)
        assert len(findings) == 0

    def test_negative_protocol_parent(self):
        """Наследование от Protocol игнорируется."""
        code = """
        class RepositoryProtocol(Protocol):
            def __init__(self):
                self.x = 1
        """
        node = self._get_class_node(code)
        
        info = ClassInfo(
            name="RepositoryProtocol", file_path="a.py", source_code="",
            parent_classes=["Protocol"], implemented_interfaces=[],
            methods=[], dependencies=[]
        )
        
        findings = _check_lsp_h_004(node, info)
        assert len(findings) == 0


    # --- 3. Сценарии с Dataclass ---

    def test_lsp_h_004_ignores_simple_dataclass_decorator(self):
        """ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE: простой декоратор @dataclass."""
        code = """
        @dataclass
        class MyDto(Base):
            def __init__(self, x):
                self.x = x
        """
        node = self._get_class_node(code)
        findings = _check_lsp_h_004(node, self.class_info)
        assert len(findings) == 0

    def test_lsp_h_004_ignores_dataclass_with_args_decorator(self):
        """ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE: декоратор с аргументами @dataclass(kw_only=True)."""
        code = """
        @dataclass(kw_only=True)
        class MyDto(Base):
            def __init__(self, x):
                self.x = x
        """
        node = self._get_class_node(code)
        findings = _check_lsp_h_004(node, self.class_info)
        assert len(findings) == 0

# ---------------------------------------------------------------------------
# Тесты identify_candidates: приоритеты, типы кандидатов, пустой ProjectMap
# ---------------------------------------------------------------------------

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
            # Нет parent_classes — нет иерархии
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
        """Все findings от эвристик должны иметь source='heuristic'."""
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
            parent_classes=[""],  # <--- Замените "<dynamic>" на пустую строку
            override_methods=["run"],
        )
        result = identify_candidates(pm, exclude_patterns=[])
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
        # Если ее нет (Python < 3.10), тест аккуратно пропускается
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
        result = identify_candidates(pm, exclude_patterns=[])
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
        Метод принимает базовый тип и использует isinstance на нем.
        Базовый тип должен быть в ProjectMap.
        """
        # Создаем файл с базовым классом и потребителем
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
        result = identify_candidates(pm, exclude_patterns=[])
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
        result = identify_candidates(pm, exclude_patterns=[])
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
        result = identify_candidates(pm, exclude_patterns=[])
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
        result = identify_candidates(pm, exclude_patterns=[])
        finding = next((f for f in result.findings if f.rule == "LSP-H-003"), None)
        assert finding is not None
        assert "shape" in finding.message
        assert "Shape" in finding.message

# ---------------------------------------------------------------------------
# Тесты для OCP-H-004 (Сложность + isinstance с учетом границ AST)
# ---------------------------------------------------------------------------

class TestOcpH004Updated:
    def setup_method(self):
        # Используем ту же правильную заглушку ClassInfo
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
        Проверяет, что вызов isinstance() внутри вложенной функции
        не триггерит OCP-H-004 для внешнего метода, даже если он сложный.
        """
        # Делаем CC = 5 для внешнего метода (база 1 + 4 if'а)
        code = """
        class ComplexProcessor:
            def process(self, x):
                if x > 1: pass
                if x > 2: pass
                if x > 3: pass
                if x > 4: pass
                
                def inner_helper(y):
                    # Этот isinstance не должен портить статистику process()
                    if isinstance(y, int):
                        pass
        """
        node = self._get_class_node(code)
        findings = _check_ocp_h_004(node, self.class_info)
        
        # До исправления ast.walk находил бы isinstance и выдавал ошибку.
        # Теперь эвристика должна молчать.
        assert len(findings) == 0

    def test_ocp_h_004_direct_isinstance_triggers(self):
        """
        Проверяет, что isinstance() в самом методе с высокой CC 
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
        findings = _check_ocp_h_004(node, self.class_info)
        
        assert len(findings) == 1
        assert findings[0].rule == "OCP-H-004"

# ---------------------------------------------------------------------------
# Тесты корректности подсчета CC: вложенные функции и классы в методах
# ---------------------------------------------------------------------------

class TestCcNestedScopes:
    """
    Проверяет, что _compute_method_cc и OCP-H-004 не засчитывают ветвления
    из вложенных функций и классов как часть CC внешнего метода.

    Это критично для универсального анализатора: в Python вложенные функции
    (closures, фабрики, декораторы) и вложенные классы — легальная конструкция,
    особенно в async-коде, data-pipelines, фабричных методах.

    Без корректного обхода OCP-H-004 выдавал бы ложные срабатывания на
    любом методе с вложенной функцией, у которой есть своя разветвленная логика.
    """

    def test_nested_function_if_not_counted(self):
        """
        Вложенная функция с if внутри — не влияет на CC внешнего метода.
        Внешний метод должен иметь CC=2 (base=1 + один if), не CC=3.
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
        # CC process() = 1 (base) + 1 (внешний if) = 2 < порога 5
        # OCP-H-004 не должен сработать
        assert "OCP-H-004" not in rules

    def test_nested_async_function_not_counted(self):
        """
        Async-вложенная функция с множеством ветвлений — не влияет на CC.
        """
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
        # CC build_task() = 1 + 1 = 2 (только внешний if считается)
        # _worker() со своими 5 if-ами — отдельная единица, не влияет
        assert "OCP-H-004" not in rules

    def test_nested_class_not_counted(self):
        """
        Вложенный класс с методами внутри — не влияет на CC внешнего метода.
        """
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
        # CC make() = 1 + 1 = 2, вложенный класс _Impl и его методы не считаются
        assert "OCP-H-004" not in rules

    def test_high_cc_in_outer_method_still_triggers(self):
        """
        Внешний метод с высокой CC + isinstance срабатывает даже при наличии
        вложенной функции. Проверяем, что мы не «потеряли» срабатывание.
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
        # CC process() = 1 + 4 (четыре if в теле) = 5 >= порога
        # isinstance есть → OCP-H-004 должен сработать
        assert "OCP-H-004" in rules

    def test_cc_counts_only_outer_elif_chain(self):
        """
        elif-цепочка снаружи считается, elif-цепочка внутри вложенной функции — нет.
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
        # CC route() = 1 + 4 (4 ветви в elif-цепочке) = 5 >= порога
        # isinstance есть во внешней цепочке → OCP-H-004 срабатывает
        assert "OCP-H-004" in rules

# ===========================================================================
# Тесты дедупликации (Шаг 3)
# ===========================================================================

from solid_dashboard.llm.heuristics import _deduplicate_findings
from solid_dashboard.llm.types import Finding, FindingDetails

class TestDeduplicateFindings:
    """
    Проверяет логику слияния конфликтующих findings для одного метода.
    По правилам Шага 3:
    - OCP-H-001 > OCP-H-004
    - LSP-H-001 > LSP-H-002
    """

    def test_ocp_h001_and_h004_on_same_method_are_merged(self):
        """
        Если на один метод срабатывают обе OCP-эвристики, должен остаться
        только OCP-H-001 (как более специфичный), а факт обнаружения OCP-H-004
        записывается в explanation.
        """
        file_path = "app/services/payment_service.py"
        class_name = "PaymentService"
        method_name = "process"

        # Имитируем finding от проверки isinstance-цепочек (OCP-H-001)
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

        # Имитируем finding от проверки цикломатической сложности (OCP-H-004)
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

        # Запускаем дедупликацию (порядок в списке не должен иметь значения)
        deduped = _deduplicate_findings([f1, f2])

        # Убеждаемся, что остался ровно один finding
        assert len(deduped) == 1
        winner = deduped[0]

        # Побеждает OCP-H-001
        assert winner.rule == "OCP-H-001"
        assert winner.details is not None
        
        # Проверяем, что в explanation победителя добавилось упоминание проигравшего
        assert "Also detected: OCP-H-004" in (winner.details.explanation or "")


# ===========================================================================
# Тесты дедупликации LSP-эвристик
# ===========================================================================

class TestDeduplicateFindingsLSP:
    """
    Проверяет, что для одного и того же метода LSP-H-001 и LSP-H-002
    не дублируются, а объединяются по правилу приоритета:
    LSP-H-001 > LSP-H-002.
    """

    def test_lsp_h001_and_h002_on_same_method_are_merged(self):
        """
        Если на один метод срабатывают LSP-H-001 и LSP-H-002, должен остаться
        только LSP-H-001, а факт обнаружения LSP-H-002 добавляется в explanation.
        """
        file_path = "app/models/user.py"
        class_name = "UserRepository"
        method_name = "save"

        # Имитируем finding от LSP-H-001 (NotImplementedError в переопределенном методе)
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

        # Имитируем finding от LSP-H-002 (пустое тело переопределенного метода)
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

        # Запускаем дедупликацию
        deduped = _deduplicate_findings([f1, f2])

        # Должен остаться один finding
        assert len(deduped) == 1
        winner = deduped[0]

        # Победитель — LSP-H-001
        assert winner.rule == "LSP-H-001"
        assert winner.details is not None

        # Explanation должен содержать упоминание LSP-H-002
        assert "Also detected: LSP-H-002" in (winner.details.explanation or "")


# ===========================================================================
# Тесты дедупликации кандидатов (LlmCandidate)
# ===========================================================================

from solid_dashboard.llm.heuristics import _deduplicate_candidates
from solid_dashboard.llm.types import LlmCandidate

class TestDeduplicateCandidates:
    """
    Проверяет, что несколько кандидатов для одного и того же класса/файла
    объединяются в один объект LlmCandidate с агрегированными полями.
    """

    def test_candidates_for_same_class_are_merged(self):
        """
        Если для одного класса есть кандидат по OCP и кандидат по LSP,
        должен остаться один кандидат с типом 'both', объединенными
        heuristic_reasons и максимальным priority.
        """
        file_path = "app/services/report_service.py"
        class_name = "ReportService"

        # Кандидат, поднятый OCP-эвристикой
        c1 = LlmCandidate(
            class_name=class_name,
            file_path=file_path,
            source_code="class ReportService: ...",
            candidate_type="ocp",
            heuristic_reasons=["OCP-H-001"],
            priority=5,
        )

        # Кандидат, поднятый LSP-эвристикой
        c2 = LlmCandidate(
            class_name=class_name,
            file_path=file_path,
            source_code="class ReportService: ...",
            candidate_type="lsp",
            heuristic_reasons=["LSP-H-001"],
            priority=3,
        )

        merged = _deduplicate_candidates([c1, c2])

        # Должен остаться один кандидат
        assert len(merged) == 1
        winner = merged[0]

        # Имя класса и путь не меняются
        assert winner.class_name == class_name
        assert winner.file_path == file_path

        # Тип агрегирован до 'both'
        assert winner.candidate_type == "both"

        # Причины объединены
        assert set(winner.heuristic_reasons) == {"OCP-H-001", "LSP-H-001"}

        # Приоритет — максимум из исходных
        assert winner.priority == 5


