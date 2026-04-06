# ===================================================================================================
# C4: тесты для _enrich_with_ancestor_attributes и _compute_lcom4
# Блоки:
#   E — MRO-обход: однородное наследование, diamond, внешние базы, неоднозначные имена
#   F — _compute_lcom4: фильтрация методов, граф LCOM4, количество компонент
#
# Все тесты работают только с ast.parse() — файловая система не нужна.
# ===================================================================================================

import ast
import textwrap
from pathlib import Path
from typing import cast, Dict, List, Tuple

from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter

# ClassInfo импортируется из helpers — единственная точка реэкспорта для тестового пакета
from .helpers import ClassInfo


# ---------------------------------------------------------------------------
# вспомогательные функции
# ---------------------------------------------------------------------------

def _make_adapter() -> CohesionAdapter:
    # cast нужен: Pylance видит CohesionAdapter через IAnalyzer-линзу без приватных методов
    return cast(CohesionAdapter, CohesionAdapter())


def _parse_classes(source: str) -> Dict[str, Tuple[ClassInfo, ast.ClassDef]]:
    """Парсит исходный код и строит {class_name: (ClassInfo, ClassDef)} для всех ClassDef."""
    adapter = _make_adapter()
    tree = ast.parse(textwrap.dedent(source))
    filepath = "/fake/module.py"

    raw: Dict[str, Tuple[ClassInfo, ast.ClassDef]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            ci = adapter._build_class_info(node, Path(filepath)) # pyright: ignore[reportAttributeAccessIssue]
            adapter._collect_instance_attributes_from_init(ci, node) # pyright: ignore[reportAttributeAccessIssue]
            adapter._populate_method_usage(ci, node) # pyright: ignore[reportAttributeAccessIssue]
            raw[node.name] = (ci, node)

    return raw


def _build_index(
    classes: Dict[str, Tuple[ClassInfo, ast.ClassDef]],
    filepath: str = "/fake/module.py",
) -> Dict[str, List[Tuple[str, ast.ClassDef]]]:
    """Строит classdef_index в формате, который ожидает _enrich_with_ancestor_attributes."""
    index: Dict[str, List[Tuple[str, ast.ClassDef]]] = {}
    for name, (_, node) in classes.items():
        index.setdefault(name, []).append((filepath, node))
    return index


# ===========================================================================
# БЛОК E: _enrich_with_ancestor_attributes — MRO-обход
# ===========================================================================


class TestEnrichWithAncestorAttributes:
    """MRO-обход: наследование self.xxx из __init__ предков."""

    # -----------------------------------------------------------------------
    # E1: однородная цепочка — Child наследует атрибуты Parent
    # -----------------------------------------------------------------------
    def test_single_inheritance_attributes_propagated(self) -> None:
        source = """
        class Parent:
            def __init__(self):
                self.x = 1
                self.y = 2

        class Child(Parent):
            def __init__(self):
                super().__init__()
                self.z = 3
        """
        adapter = _make_adapter()
        classes = _parse_classes(source)
        index = _build_index(classes)

        child_ci, child_node = classes["Child"]
        # до обогащения — только собственный атрибут z
        assert "z" in child_ci.attributes
        assert "x" not in child_ci.attributes

        adapter._enrich_with_ancestor_attributes(child_ci, child_node, index, "/fake/module.py") # pyright: ignore[reportAttributeAccessIssue]

        # после обогащения — атрибуты предка тоже присутствуют
        assert "x" in child_ci.attributes
        assert "y" in child_ci.attributes
        assert "z" in child_ci.attributes

    # -----------------------------------------------------------------------
    # E2: двухуровневая цепочка — GrandChild получает атрибуты всех предков
    # -----------------------------------------------------------------------
    def test_two_level_inheritance_fully_enriched(self) -> None:
        source = """
        class GrandParent:
            def __init__(self):
                self.a = 1

        class Parent(GrandParent):
            def __init__(self):
                self.b = 2

        class GrandChild(Parent):
            def __init__(self):
                self.c = 3
        """
        adapter = _make_adapter()
        classes = _parse_classes(source)
        index = _build_index(classes)

        gc_ci, gc_node = classes["GrandChild"]
        adapter._enrich_with_ancestor_attributes(gc_ci, gc_node, index, "/fake/module.py") # pyright: ignore[reportAttributeAccessIssue]

        # все три уровня должны быть в атрибутах GrandChild
        assert {"a", "b", "c"} <= gc_ci.attributes

    # -----------------------------------------------------------------------
    # E3: diamond-наследование — атрибут Base не дублируется и нет бесконечного цикла
    # -----------------------------------------------------------------------
    def test_diamond_inheritance_no_cycle_no_duplicates(self) -> None:
        source = """
        class Base:
            def __init__(self):
                self.shared = 1

        class LeftMixin(Base):
            def __init__(self):
                self.left = 2

        class RightMixin(Base):
            def __init__(self):
                self.right = 3

        class Diamond(LeftMixin, RightMixin):
            def __init__(self):
                self.own = 4
        """
        adapter = _make_adapter()
        classes = _parse_classes(source)
        index = _build_index(classes)

        d_ci, d_node = classes["Diamond"]
        # обогащение не должно зависнуть и не должно сломаться
        adapter._enrich_with_ancestor_attributes(d_ci, d_node, index, "/fake/module.py") # pyright: ignore[reportAttributeAccessIssue]

        # все атрибуты присутствуют (дубликаты Set поглощает сам)
        assert {"shared", "left", "right", "own"} <= d_ci.attributes

    # -----------------------------------------------------------------------
    # E4: внешний базовый класс (не в индексе) — молча пропускается
    # -----------------------------------------------------------------------
    def test_external_base_not_in_index_silently_skipped(self) -> None:
        source = """
        class Child(SomeExternalBase):
            def __init__(self):
                self.own = 1
        """
        adapter = _make_adapter()
        classes = _parse_classes(source)
        index = _build_index(classes)  # SomeExternalBase не попадает в индекс

        child_ci, child_node = classes["Child"]
        # не должно бросить исключение
        adapter._enrich_with_ancestor_attributes(child_ci, child_node, index, "/fake/module.py") # pyright: ignore[reportAttributeAccessIssue]

        assert "own" in child_ci.attributes

    # -----------------------------------------------------------------------
    # E5: атрибуты предка не перетирают собственные атрибуты дочернего класса
    # -----------------------------------------------------------------------
    def test_ancestor_attributes_do_not_overwrite_child_own(self) -> None:
        source = """
        class Parent:
            def __init__(self):
                self.value = 10

        class Child(Parent):
            def __init__(self):
                self.value = 99   # то же имя — переопределение
                self.extra = 5
        """
        adapter = _make_adapter()
        classes = _parse_classes(source)
        index = _build_index(classes)

        child_ci, child_node = classes["Child"]
        adapter._enrich_with_ancestor_attributes(child_ci, child_node, index, "/fake/module.py") # pyright: ignore[reportAttributeAccessIssue]

        # атрибут value должен присутствовать (Set идемпотентен — add не перетирает)
        assert "value" in child_ci.attributes
        assert "extra" in child_ci.attributes

    # -----------------------------------------------------------------------
    # E6: класс без __init__ у предка — атрибуты не добавляются, без ошибок
    # -----------------------------------------------------------------------
    def test_ancestor_without_init_no_error(self) -> None:
        source = """
        class NoInit:
            def compute(self):
                return 42

        class Child(NoInit):
            def __init__(self):
                self.result = 0
        """
        adapter = _make_adapter()
        classes = _parse_classes(source)
        index = _build_index(classes)

        child_ci, child_node = classes["Child"]
        adapter._enrich_with_ancestor_attributes(child_ci, child_node, index, "/fake/module.py") # pyright: ignore[reportAttributeAccessIssue]

        # только собственный атрибут; нет падений
        assert child_ci.attributes == {"result"}


# ===========================================================================
# БЛОК F: _compute_lcom4
# ===========================================================================


class TestComputeLcom4:
    """Расчет LCOM4: граф методов, компоненты связности."""

    def _lcom4(self, source: str, class_name: str = "C") -> tuple[int, int]:
        """Вспомогательный метод: строит ClassInfo и возвращает (lcom4, methods_count)."""
        adapter = _make_adapter()
        classes = _parse_classes(source)
        ci, _ = classes[class_name]
        return adapter._compute_lcom4(ci) # pyright: ignore[reportAttributeAccessIssue]

    # -----------------------------------------------------------------------
    # F1: класс без методов — (0, 0)
    # -----------------------------------------------------------------------
    def test_no_methods_returns_zero_zero(self) -> None:
        source = """
        class C:
            x = 1
        """
        lcom4, count = self._lcom4(source)
        assert lcom4 == 0
        assert count == 0

    # -----------------------------------------------------------------------
    # F2: один нетривиальный метод — одна компонента
    # -----------------------------------------------------------------------
    def test_single_method_one_component(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.a = 1

            def do(self):
                return self.a
        """
        lcom4, count = self._lcom4(source)
        # __init__ исключается из графа; остается только do
        assert count == 1
        assert lcom4 == 1

    # -----------------------------------------------------------------------
    # F3: два метода с общим атрибутом — одна компонента (связный класс)
    # -----------------------------------------------------------------------
    def test_two_methods_shared_attribute_one_component(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.value = 0

            def get(self):
                return self.value

            def set(self, v):
                self.value = v
        """
        lcom4, count = self._lcom4(source)
        assert count == 2
        assert lcom4 == 1  # оба метода делят self.value

    # -----------------------------------------------------------------------
    # F4: два несвязных метода — две компоненты (низкая связность)
    # -----------------------------------------------------------------------
    def test_two_unrelated_methods_two_components(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.x = 1
                self.y = 2

            def use_x(self):
                return self.x

            def use_y(self):
                return self.y
        """
        lcom4, count = self._lcom4(source)
        assert count == 2
        assert lcom4 == 2  # use_x и use_y не делят атрибутов и не вызывают друг друга

    # -----------------------------------------------------------------------
    # F5: связность через вызов метода, а не через атрибуты
    # -----------------------------------------------------------------------
    def test_methods_connected_via_call(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.x = 1
                self.y = 2

            def helper(self):
                return self.x

            def main(self):
                return self.helper() + self.y
        """
        lcom4, count = self._lcom4(source)
        # helper и main связаны через вызов self.helper() — одна компонента
        assert lcom4 == 1

    # -----------------------------------------------------------------------
    # F6: пустые методы исключаются из графа
    # -----------------------------------------------------------------------
    def test_empty_methods_excluded_from_graph(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.a = 1

            def stub(self):
                ...

            def real(self):
                return self.a
        """
        lcom4, count = self._lcom4(source)
        # stub исключен (is_empty=True), остается только real
        assert count == 1
        assert lcom4 == 1

    # -----------------------------------------------------------------------
    # F7: property-методы исключаются из графа
    # -----------------------------------------------------------------------
    def test_property_methods_excluded_from_graph(self) -> None:
        source = """
        class C:
            def __init__(self):
                self._val = 0

            @property
            def val(self):
                return self._val

            def do(self):
                return self._val * 2
        """
        lcom4, count = self._lcom4(source)
        # val-property исключена; остается только do
        assert count == 1
        assert lcom4 == 1

    # -----------------------------------------------------------------------
    # F8: три метода, два из которых связаны — итого две компоненты
    # -----------------------------------------------------------------------
    def test_three_methods_partial_connectivity(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.shared = 0
                self.isolated = 0

            def a(self):
                return self.shared

            def b(self):
                self.shared = 1

            def c(self):
                return self.isolated
        """
        lcom4, count = self._lcom4(source)
        assert count == 3
        # a и b связаны через self.shared; c — изолирован
        assert lcom4 == 2

    # -----------------------------------------------------------------------
    # F9: __init__ всегда исключается из подсчета методов
    # -----------------------------------------------------------------------
    def test_init_excluded_from_lcom4_graph(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.a = 1
                self.b = 2

            def use_a(self):
                return self.a

            def use_b(self):
                return self.b
        """
        lcom4, count = self._lcom4(source)
        # __init__ не в графе; use_a и use_b несвязны
        assert count == 2
        assert lcom4 == 2

    # -----------------------------------------------------------------------
    # F10: полностью связный класс из трёх методов — одна компонента
    # -----------------------------------------------------------------------
    def test_fully_connected_three_methods_one_component(self) -> None:
        source = """
        class C:
            def __init__(self):
                self.x = 0

            def inc(self):
                self.x += 1

            def dec(self):
                self.x -= 1

            def reset(self):
                self.x = 0
        """
        lcom4, count = self._lcom4(source)
        assert count == 3
        assert lcom4 == 1  # все три метода делят self.x
