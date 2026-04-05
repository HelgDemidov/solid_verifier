# ---------------------------------------------------------------------------
# Юнит-тесты эвристики LSP-H-003
# Детектор: isinstance на параметре с аннотацией базового типа
# ---------------------------------------------------------------------------

import textwrap

from solid_dashboard.llm.ast_parser import build_project_map
from solid_dashboard.llm.heuristics import identify_candidates


class TestLspH003:
    def test_positive_isinstance_on_annotated_base_param(self, tmp_path):
        """
        Метод принимает базовый тип и использует isinstance на нем.
        Базовый тип должен быть в ProjectMap.
        """
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
        Эвристика молчит — внешние типы не анализируются.
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
        """Параметр без аннотации — эвристика молчит."""
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
        """Finding упоминает имя параметра и базовый тип."""
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
        finding = next((fi for fi in result.findings if fi.rule == "LSP-H-003"), None)
        assert finding is not None
        assert finding.details is not None
        assert finding.details.explanation is not None
        # Имя параметра упоминается в explanation
        assert "shape" in finding.details.explanation
        # Базовый тип упоминается в message
        assert "base" in finding.message
