# ---------------------------------------------------------------------------
# Тесты для solid_dashboard.llm.heuristics._shared
#
# Покрывает все экспортируемые утилиты из _shared.py:
#   - _should_exclude_path()   — фильтрация путей
#   - _parse_class_ast()       — парсинг AST класса из исходника
#   - _make_finding()          — фабрика Finding
#   - _is_abstract_class()     — определение абстрактного класса
#   - _has_isinstance_call()   — поиск isinstance() в выражении
#   - _count_elif_chain()      — подсчёт длины if/elif цепочки
#   - _iter_method_nodes()     — обход тела метода без вложенных функций/классов
#   - _compute_method_cc()     — цикломатическая сложность метода
#
# Запуск:
#   pytest tools/solid_verifier/tests/llm/test_heuristics/test_heuristics_shared.py -v
# ---------------------------------------------------------------------------

import ast
import textwrap

import pytest

from solid_dashboard.llm.heuristics._shared import (
    _should_exclude_path,
    _parse_class_ast,
    _make_finding,
    _is_abstract_class,
    _has_isinstance_call,
    _count_elif_chain,
    _iter_method_nodes,
    _compute_method_cc,
    _DEFAULT_EXCLUDE_PATTERNS,
)
from solid_dashboard.llm.types import ClassInfo, InterfaceInfo, MethodSignature, ProjectMap


# ---------------------------------------------------------------------------
# Вспомогательные фабрики
# ---------------------------------------------------------------------------

def _make_class_info(
    name: str = "TestClass",
    file_path: str = "app/service.py",
    parent_classes: list[str] | None = None,
    methods: list[MethodSignature] | None = None,
) -> ClassInfo:
    return ClassInfo(
        name=name,
        file_path=file_path,
        source_code="",
        parent_classes=parent_classes or [],
        implemented_interfaces=[],
        methods=methods or [],
        dependencies=[],
    )


def _make_interface_info(name: str, file_path: str = "app/interfaces.py") -> InterfaceInfo:
    # Минимальный InterfaceInfo для регистрации в ProjectMap.interfaces
    return InterfaceInfo(name=name, file_path=file_path, methods=[], implementations=[])


def _make_project_map(
    classes: dict[str, ClassInfo] | None = None,
    # Принимаем список имён для удобства тестов — конвертируем в dict[str, InterfaceInfo]
    interfaces: list[str] | None = None,
) -> ProjectMap:
    interfaces_dict: dict[str, InterfaceInfo] = (
        {name: _make_interface_info(name) for name in interfaces}
        if interfaces
        else {}
    )
    return ProjectMap(
        classes=classes or {},
        interfaces=interfaces_dict,
    )


def _parse_func(source: str, func_name: str) -> ast.FunctionDef:
    """Парсит исходник и возвращает FunctionDef с указанным именем."""
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise ValueError(f"FunctionDef '{func_name}' не найден")


def _parse_expr(source: str) -> ast.expr:
    """Парсит одиночное выражение и возвращает его AST-ноду."""
    tree = ast.parse(textwrap.dedent(source), mode="eval")
    return tree.body


def _parse_if(source: str) -> ast.If:
    """Парсит исходник и возвращает первый ast.If."""
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            return node
    raise ValueError("ast.If не найден")


# ---------------------------------------------------------------------------
# TestShouldExcludePath — фильтрация путей
# ---------------------------------------------------------------------------

class TestShouldExcludePath:

    def test_excludes_tests_dir(self):
        """Путь с 'tests/' исключается."""
        assert _should_exclude_path("app/tests/test_service.py", None) is True

    def test_excludes_test_prefix(self):
        """Файл 'test_foo.py' исключается."""
        assert _should_exclude_path("app/test_foo.py", None) is True

    def test_excludes_test_suffix(self):
        """Файл 'service_test.py' исключается."""
        assert _should_exclude_path("app/service_test.py", None) is True

    def test_excludes_migrations(self):
        """Путь с 'migrations/' исключается."""
        assert _should_exclude_path("app/migrations/0001_initial.py", None) is True

    def test_excludes_config_py(self):
        """config.py исключается."""
        assert _should_exclude_path("project/config.py", None) is True

    def test_excludes_settings_py(self):
        """settings.py исключается."""
        assert _should_exclude_path("project/settings.py", None) is True

    def test_does_not_exclude_domain_file(self):
        """Обычный доменный файл НЕ исключается."""
        assert _should_exclude_path("app/services/order_service.py", None) is False

    def test_custom_patterns_override(self):
        """Кастомные паттерны работают независимо от дефолтных."""
        # Дефолтные паттерны не применяются при явном списке
        assert _should_exclude_path("app/tests/foo.py", ["vendors/"]) is False
        assert _should_exclude_path("app/vendors/bar.py", ["vendors/"]) is True

    def test_empty_custom_patterns_excludes_nothing(self):
        """Пустой список паттернов — ничего не исключается."""
        assert _should_exclude_path("app/tests/test_foo.py", []) is False

    def test_case_insensitive_matching(self):
        """Сопоставление регистронезависимое."""
        assert _should_exclude_path("App/TESTS/Foo.py", None) is True

    def test_windows_path_separator(self):
        """Обратные слэши (Windows) нормализуются корректно."""
        assert _should_exclude_path("app\\tests\\test_foo.py", None) is True


# ---------------------------------------------------------------------------
# TestParseClassAst — парсинг AST класса
# ---------------------------------------------------------------------------

class TestParseClassAst:

    def test_returns_class_def_for_valid_source(self):
        """Возвращает ClassDef при корректном исходнике и правильном имени."""
        source = "class Foo:\n    pass\n"
        result = _parse_class_ast(source, "Foo")
        assert isinstance(result, ast.ClassDef)
        assert result.name == "Foo"

    def test_returns_none_for_wrong_name(self):
        """Возвращает None, если класс с указанным именем не найден."""
        source = "class Foo:\n    pass\n"
        assert _parse_class_ast(source, "Bar") is None

    def test_returns_none_for_empty_source(self):
        """Возвращает None при пустом исходнике."""
        assert _parse_class_ast("", "Foo") is None
        assert _parse_class_ast("   ", "Foo") is None

    def test_returns_none_for_syntax_error(self):
        """Возвращает None при SyntaxError в исходнике."""
        assert _parse_class_ast("class Foo(:\n    pass", "Foo") is None

    def test_finds_class_among_multiple(self):
        """Находит нужный класс среди нескольких в одном файле."""
        source = "class A:\n    pass\nclass B:\n    x = 1\nclass C:\n    pass\n"
        result = _parse_class_ast(source, "B")
        assert result is not None
        assert result.name == "B"


# ---------------------------------------------------------------------------
# TestMakeFinding — фабрика Finding
# ---------------------------------------------------------------------------

class TestMakeFinding:

    def test_basic_fields(self):
        """_make_finding создает Finding с корректными обязательными полями."""
        class_info = _make_class_info(name="MyClass", file_path="app/my_class.py")
        finding = _make_finding(
            rule="OCP-H-001",
            class_info=class_info,
            message="Test message",
            principle="OCP",
            explanation="Some explanation",
            suggestion="Do this instead",
        )
        assert finding.rule == "OCP-H-001"
        assert finding.class_name == "MyClass"
        assert finding.file == "app/my_class.py"
        assert finding.message == "Test message"
        assert finding.source == "heuristic"
        assert finding.severity == "warning"

    def test_details_populated(self):
        """FindingDetails заполняются корректно."""
        class_info = _make_class_info()
        finding = _make_finding(
            rule="LSP-H-001",
            class_info=class_info,
            message="msg",
            principle="LSP",
            explanation="explanation text",
            suggestion="suggestion text",
            method_name="process",
        )
        assert finding.details is not None
        assert finding.details.principle == "LSP"
        assert finding.details.explanation == "explanation text"
        assert finding.details.suggestion == "suggestion text"
        assert finding.details.method_name == "process"

    def test_method_name_optional_none(self):
        """method_name=None (по умолчанию) не вызывает ошибки."""
        class_info = _make_class_info()
        finding = _make_finding(
            rule="OCP-H-004",
            class_info=class_info,
            message="msg",
            principle="OCP",
            explanation="expl",
            suggestion="sugg",
        )
        # Сначала сужаем Optional[FindingDetails] до FindingDetails
        assert finding.details is not None
        assert finding.details.method_name is None

    def test_line_is_none(self):
        """Поле line всегда None (эвристики не дают точную строку)."""
        class_info = _make_class_info()
        finding = _make_finding(
            rule="LSP-H-002",
            class_info=class_info,
            message="msg",
            principle="LSP",
            explanation="expl",
            suggestion="sugg",
        )
        assert finding.line is None


# ---------------------------------------------------------------------------
# TestIsAbstractClass — определение абстрактного класса
# ---------------------------------------------------------------------------

class TestIsAbstractClass:

    def test_abc_in_parent_classes(self):
        """Класс с ABC в parent_classes — абстрактный."""
        ci = _make_class_info(parent_classes=["ABC"])
        pm = _make_project_map()
        from solid_dashboard.llm.heuristics._shared import _is_abstract_class
        assert _is_abstract_class(ci, pm) is True

    def test_registered_interface_in_project_map(self):
        """Класс, зарегистрированный как интерфейс в ProjectMap — абстрактный."""
        ci = _make_class_info(name="IService", parent_classes=[])
        pm = _make_project_map(interfaces=["IService"])
        from solid_dashboard.llm.heuristics._shared import _is_abstract_class
        assert _is_abstract_class(ci, pm) is True

    def test_has_abstract_method(self):
        """Класс с хотя бы одним is_abstract методом — абстрактный."""
        methods = [
            MethodSignature(
                # parameters — строка сигнатуры, как в types.py
                name="process", parameters="", return_type=None,
                is_override=False, is_abstract=True,
            )
        ]
        ci = _make_class_info(parent_classes=[], methods=methods)
        pm = _make_project_map()
        from solid_dashboard.llm.heuristics._shared import _is_abstract_class
        assert _is_abstract_class(ci, pm) is True

    def test_concrete_class_not_abstract(self):
        """Конкретный класс без ABC и без абстрактных методов — не абстрактный."""
        methods = [
            MethodSignature(
                name="run", parameters="", return_type=None,
                is_override=False, is_abstract=False,
            )
        ]
        ci = _make_class_info(parent_classes=["SomeBase"], methods=methods)
        pm = _make_project_map()
        from solid_dashboard.llm.heuristics._shared import _is_abstract_class
        assert _is_abstract_class(ci, pm) is False


# ---------------------------------------------------------------------------
# TestHasIsinstanceCall — поиск isinstance() в выражении
# ---------------------------------------------------------------------------

class TestHasIsinstanceCall:

    def test_simple_isinstance(self):
        """Простое isinstance(x, Foo) — найдено."""
        expr = _parse_expr("isinstance(x, Foo)")
        assert _has_isinstance_call(expr) is True

    def test_isinstance_in_and_condition(self):
        """isinstance внутри and-выражения — найдено."""
        expr = _parse_expr("x > 0 and isinstance(x, Bar)")
        assert _has_isinstance_call(expr) is True

    def test_no_isinstance(self):
        """Выражение без isinstance — НЕ найдено."""
        expr = _parse_expr("x > 0 and y < 100")
        assert _has_isinstance_call(expr) is False

    def test_nested_isinstance(self):
        """isinstance внутри вложенного вызова — найдено."""
        expr = _parse_expr("not isinstance(obj, str)")
        assert _has_isinstance_call(expr) is True

    def test_similar_name_not_matched(self):
        """Вызов issubclass() — НЕ является isinstance()."""
        expr = _parse_expr("issubclass(Foo, Bar)")
        assert _has_isinstance_call(expr) is False


# ---------------------------------------------------------------------------
# TestCountElifChain — подсчёт длины if/elif цепочки
# ---------------------------------------------------------------------------

class TestCountElifChain:

    def test_single_if(self):
        """Одиночный if без elif — длина 1."""
        if_node = _parse_if("""
            if x == 1:
                pass
        """)
        assert _count_elif_chain(if_node) == 1

    def test_if_elif(self):
        """if + 1 elif — длина 2."""
        if_node = _parse_if("""
            if x == 1:
                pass
            elif x == 2:
                pass
        """)
        assert _count_elif_chain(if_node) == 2

    def test_if_elif_elif_elif(self):
        """if + 3 elif — длина 4."""
        if_node = _parse_if("""
            if x == 1:
                pass
            elif x == 2:
                pass
            elif x == 3:
                pass
            elif x == 4:
                pass
        """)
        assert _count_elif_chain(if_node) == 4

    def test_if_else_not_counted_as_elif(self):
        """if + else (без elif) — длина 1 (else не увеличивает счётчик)."""
        if_node = _parse_if("""
            if x == 1:
                pass
            else:
                pass
        """)
        assert _count_elif_chain(if_node) == 1


# ---------------------------------------------------------------------------
# TestIterMethodNodes — обход тела метода
# ---------------------------------------------------------------------------

class TestIterMethodNodes:

    def test_yields_top_level_nodes(self):
        """Обходит узлы тела метода верхнего уровня."""
        func = _parse_func("""
            def my_method(self):
                x = 1
                y = 2
                return x + y
        """, "my_method")
        nodes = list(_iter_method_nodes(func))
        # Должен содержать Assign и Return
        node_types = {type(n).__name__ for n in nodes}
        assert "Assign" in node_types
        assert "Return" in node_types

    def test_does_not_enter_nested_function(self):
        """Вложенная функция не обходится (пропускается целиком)."""
        func = _parse_func("""
            def outer(self):
                x = 1
                def inner():
                    isinstance(y, Foo)  # этот isinstance НЕ должен быть найден
                return x
        """, "outer")
        nodes = list(_iter_method_nodes(func))
        # Убеждаемся, что FunctionDef (inner) не попал в nodes
        func_def_nodes = [n for n in nodes if isinstance(n, ast.FunctionDef)]
        assert func_def_nodes == []

    def test_does_not_enter_nested_class(self):
        """Вложенный класс не обходится."""
        func = _parse_func("""
            def method(self):
                x = 1
                class Inner:
                    def helper(self):
                        return 42
                return x
        """, "method")
        nodes = list(_iter_method_nodes(func))
        class_def_nodes = [n for n in nodes if isinstance(n, ast.ClassDef)]
        assert class_def_nodes == []


# ---------------------------------------------------------------------------
# TestComputeMethodCc — цикломатическая сложность
# ---------------------------------------------------------------------------

class TestComputeMethodCc:

    def test_simple_method_cc_is_one(self):
        """Метод без ветвлений — CC = 1."""
        func = _parse_func("""
            def simple(self, x: int) -> int:
                return x * 2
        """, "simple")
        assert _compute_method_cc(func) == 1

    def test_single_if_cc_is_two(self):
        """Один if добавляет +1 к CC."""
        func = _parse_func("""
            def check(self, x):
                if x > 0:
                    return True
                return False
        """, "check")
        assert _compute_method_cc(func) == 2

    def test_for_loop_adds_to_cc(self):
        """for-цикл добавляет +1 к CC."""
        func = _parse_func("""
            def loop(self, items):
                for item in items:
                    print(item)
        """, "loop")
        assert _compute_method_cc(func) == 2

    def test_try_except_adds_to_cc(self):
        """ExceptHandler добавляет +1 к CC."""
        func = _parse_func("""
            def safe(self):
                try:
                    return 1
                except ValueError:
                    return 0
        """, "safe")
        assert _compute_method_cc(func) == 2

    def test_and_operator_adds_to_cc(self):
        """Оператор and добавляет к CC."""
        func = _parse_func("""
            def validate(self, x, y):
                return x > 0 and y > 0
        """, "validate")
        # CC = 1 (base) + 1 (and with 2 values => len-1=1)
        assert _compute_method_cc(func) == 2

    def test_ternary_adds_to_cc(self):
        """Тернарный оператор добавляет +1 к CC."""
        func = _parse_func("""
            def pick(self, flag):
                return 'yes' if flag else 'no'
        """, "pick")
        assert _compute_method_cc(func) == 2

    def test_complex_method_cc(self):
        """Метод с несколькими ветвлениями — CC суммируется корректно."""
        func = _parse_func("""
            def process(self, items, flag):
                if not items:
                    return []
                result = []
                for item in items:
                    if flag and item > 0:
                        result.append(item)
                return result
        """, "process")
        # CC = 1 + 1 (if not items) + 1 (for) + 1 (if flag...) + 1 (and) = 5
        assert _compute_method_cc(func) == 5

    def test_nested_function_not_counted(self):
        """Вложенная функция не увеличивает CC внешнего метода."""
        func = _parse_func("""
            def outer(self):
                def inner(x):
                    if x > 0:  # Этот if НЕ должен считаться в CC outer
                        return x
                    for i in range(x):  # Этот for тоже
                        pass
                return inner
        """, "outer")
        # CC outer = 1 (нет ветвлений на верхнем уровне)
        assert _compute_method_cc(func) == 1
