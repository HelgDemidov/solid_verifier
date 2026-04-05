# ---------------------------------------------------------------------------
# Юнит-тесты эвристики LSP-H-004
# Детектор: проблемный __init__ дочернего класса относительно родителя
# ---------------------------------------------------------------------------

import ast
import textwrap

from solid_dashboard.llm.heuristics import lsp_h_004
from solid_dashboard.llm.types import ClassInfo, ProjectMap

from .conftest import _class_info_node_and_project_map_from_source


class TestLspH004Updated:
    def setup_method(self):
        # Базовая заглушка ClassInfo для тестов с родителем
        self.class_info = ClassInfo(
            name="Child",
            file_path="child.py",
            source_code="",
            parent_classes=["Base"],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )

        # Минимальный ProjectMap — достаточен для большинства unit-тестов
        self.project_map = ProjectMap(
            classes={},
            interfaces={},
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
        findings = lsp_h_004.check(node, self.class_info, self.project_map)
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
        findings = lsp_h_004.check(node, self.class_info, self.project_map)
        assert len(findings) == 0

    def test_negative_no_init_at_all(self):
        """Подкласс без __init__ — эвристика молчит (наследует от родителя)."""
        code = """
            class Child(Base):
                def run(self):
                    pass
        """
        node = self._get_class_node(code)
        findings = lsp_h_004.check(node, self.class_info, self.project_map)
        assert len(findings) == 0

    def test_negative_standalone_class_with_init(self):
        """Класс без родителей — проверка не применима, эвристика молчит."""
        code = """
            class Standalone:
                def __init__(self):
                    self.value = 0
        """
        node = self._get_class_node(code)
        standalone_info = ClassInfo(
            name="Standalone",
            file_path="a.py",
            source_code="",
            parent_classes=[],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )
        findings = lsp_h_004.check(node, standalone_info, self.project_map)
        assert len(findings) == 0

    def test_negative_explicit_object_parent(self):
        """Явное наследование от object не должно давать LSP-H-004."""
        code = """
            class MyValueObject(object):
                def __init__(self):
                    self.x = 1
        """
        node = self._get_class_node(code)
        info = ClassInfo(
            name="MyValueObject",
            file_path="a.py",
            source_code="",
            parent_classes=["object"],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )
        findings = lsp_h_004.check(node, info, self.project_map)
        assert len(findings) == 0

    def test_negative_abc_parent(self):
        """Наследование от ABC игнорируется."""
        code = """
            class BaseService(ABC):
                def __init__(self):
                    self.x = 1
        """
        node = self._get_class_node(code)
        info = ClassInfo(
            name="BaseService",
            file_path="a.py",
            source_code="",
            parent_classes=["ABC"],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )
        findings = lsp_h_004.check(node, info, self.project_map)
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
            name="RepositoryProtocol",
            file_path="a.py",
            source_code="",
            parent_classes=["Protocol"],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )
        findings = lsp_h_004.check(node, info, self.project_map)
        assert len(findings) == 0

    # --- 2. Защита от false positive: dataclass ---

    def test_lsp_h_004_ignores_simple_dataclass_decorator(self):
        """ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE: простой декоратор @dataclass."""
        code = """
            @dataclass
            class MyDto(Base):
                def __init__(self, x):
                    self.x = x
        """
        node = self._get_class_node(code)
        findings = lsp_h_004.check(node, self.class_info, self.project_map)
        assert len(findings) == 0

    def test_lsp_h_004_ignores_dataclass_with_args_decorator(self):
        """ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE: @dataclass(kw_only=True)."""
        code = """
            @dataclass(kw_only=True)
            class MyDto(Base):
                def __init__(self, x):
                    self.x = x
        """
        node = self._get_class_node(code)
        findings = lsp_h_004.check(node, self.class_info, self.project_map)
        assert len(findings) == 0

    def test_negative_dataclasses_dataclass_decorator_is_ignored(self):
        """ПРЕДОТВРАЩЕНИЕ FALSE POSITIVE: @dataclasses.dataclass тоже игнорируется."""
        code = """
            @dataclasses.dataclass
            class MyDto(Base):
                def __init__(self, x):
                    self.x = x
        """
        node = self._get_class_node(code)
        findings = lsp_h_004.check(node, self.class_info, self.project_map)
        assert len(findings) == 0

    # --- 3. Защита от false positive: pure interface ---

    def test_negative_pure_interface_class_is_ignored(self):
        """Чистый интерфейс не дает LSP-H-004 даже с родителем ABC."""
        code = """
            class IRepository(ABC):
                @abstractmethod
                def save(self, item):
                    pass
        """
        node = self._get_class_node(code)
        info = ClassInfo(
            name="IRepository",
            file_path="a.py",
            source_code="",
            parent_classes=["ABC"],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )
        findings = lsp_h_004.check(node, info, self.project_map)
        assert len(findings) == 0
