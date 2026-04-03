"""
Интеграционные тесты Шага 3: полный цикл build_project_map → identify_candidates.

В отличие от test_heuristics.py (unit-тесты отдельных эвристик напрямую),
здесь мы проверяем всю цепочку на реальных Python-файлах:
build_project_map([paths]) → ProjectMap → identify_candidates(pm) → HeuristicResult

Каждый тест проверяет один аспект интеграции: что конкретный rule-код попадает в findings,
что чистый код не порождает ложных срабатываний, что метаданные корректны и т.д.
"""
import textwrap
import pytest
import sys
from pathlib import Path

from solid_dashboard.llm.ast_parser import build_project_map
from solid_dashboard.llm.heuristics import identify_candidates
from solid_dashboard.llm.types import HeuristicResult

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Фикстуры: один тестовый Python-файл на несколько тестов
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def smelly_project_dir(tmp_path_factory):
    """
    Создает временную директорию с двумя Python-файлами:
    - smells.py — классы с намеренными нарушениями для каждой эвристики.
    - clean.py — классы без нарушений (для проверки отсутствия false positives).

    scope="module" — файлы создаются один раз для всего модуля тестов.
    Это экономит время: build_project_map вызывается не на каждый тест.
    """
    base = tmp_path_factory.mktemp("integration_project")

    # --- smells.py: по одному нарушению на каждую эвристику ---
    (base / "smells.py").write_text(textwrap.dedent("""
    # Классы-«носители» нарушений для интеграционных тестов.
    # Каждый класс — нарушение ровно одной эвристики.

    # === LSP-H-001: override метод бросает NotImplementedError ===
    class BaseSerializer:
        def serialize(self, data): ...

    class XmlSerializer(BaseSerializer):
        def serialize(self, data):
            raise NotImplementedError("XML not supported")

    # === LSP-H-002: override метод с пустым телом ===
    class BaseNotifier:
        def notify(self, message): ...

    class SilentNotifier(BaseNotifier):
        def notify(self, message):
            pass  # намеренно пустой override

    # === LSP-H-004: __init__ без super().__init__() ===
    class BaseConfig:
        def __init__(self):
            self.debug = False

    class AppConfig(BaseConfig):
        def __init__(self):
            # Намеренно не вызываем super().__init__() — нарушение LSP
            self.app_name = "MyApp"

    # === OCP-H-001: цепочка if/elif с isinstance >= 4 ветвей ===
    class Circle: pass
    class Square: pass
    class Triangle: pass
    class Hexagon: pass

    class ShapeRenderer:
        def render(self, shape):
            if isinstance(shape, Circle):
                return "circle"
            elif isinstance(shape, Square):
                return "square"
            elif isinstance(shape, Triangle):
                return "triangle"
            elif isinstance(shape, Hexagon):
                return "hexagon"

    # === OCP-H-004: высокая CC + isinstance ===
    class BaseReport: pass

    class ComplexProcessor:
        def process(self, item):
            # 4 независимых if-ветки создают CC=5 (base=1 + 4 ветви)
            if item.step == "validate":
                self._validate(item)
            if item.step == "transform":
                self._transform(item)
            if item.step == "enrich":
                self._enrich(item)
            if isinstance(item, BaseReport):
                self._special_report_handling(item)
                
        def _validate(self, item): ...
        def _transform(self, item): ...
        def _enrich(self, item): ...
        def _special_report_handling(self, item): ...

    # === Класс с несколькими нарушениями: должен иметь высший приоритет ===
    class HighPrioritySmell(BaseSerializer):
        def __init__(self):
            # LSP-H-004: нет super().__init__()
            self.ready = False

        def serialize(self, data):
            # LSP-H-001: override с NotImplementedError
            raise NotImplementedError
    """), encoding="utf-8")

    # --- clean.py: образцово-показательный код без нарушений ---
    (base / "clean.py").write_text(textwrap.dedent("""
    # Чистый модуль — никаких нарушений OCP/LSP.
    # Должен давать нулевое количество findings от наших эвристик.

    class DataTransformer:
        \"\"\"Трансформирует данные без наследования и type-dispatch.\"\"\"

        def transform(self, data: dict) -> dict:
            # Простая логика без isinstance и без длинных if/elif цепочек
            result = {}
            for key, value in data.items():
                result[key] = str(value).strip()
            return result

        def validate(self, data: dict) -> bool:
            return bool(data)

    class StringNormalizer:
        \"\"\"Нормализует строки — простой класс без наследования.\"\"\"

        def normalize(self, text: str) -> str:
            return text.lower().strip()

        def is_empty(self, text: str) -> bool:
            return len(text.strip()) == 0
    """), encoding="utf-8")

    return base

@pytest.fixture(scope="module")
def heuristic_result(smelly_project_dir) -> HeuristicResult:
    """
    Строит ProjectMap по всем файлам в smelly_project_dir и запускает identify_candidates.
    """
    py_files = [
        str(smelly_project_dir / "smells.py"),
        str(smelly_project_dir / "clean.py"),
    ]
    pm = build_project_map(py_files)
    return identify_candidates(pm)

# ---------------------------------------------------------------------------
# Тест 1: все ожидаемые rule-коды присутствуют в findings
# ---------------------------------------------------------------------------

class TestAllRulesPresent:

    # OCP-H-002 (match/case) проверен отдельно. OCP-H-003 и LSP-H-003 удалены.
    EXPECTED_RULES = {
        "LSP-H-001",
        "LSP-H-002",
        "LSP-H-004",
        "OCP-H-001",
        "OCP-H-004",
    }

    def test_all_expected_rules_present(self, heuristic_result):
        """Каждый из rule-кодов должен встретиться хотя бы в одном finding."""
        found_rules = {f.rule for f in heuristic_result.findings}
        missing = self.EXPECTED_RULES - found_rules
        assert not missing, f"Missing rule codes in findings: {missing}"

    def test_no_unexpected_rule_codes(self, heuristic_result):
        """В findings не должно появляться неизвестных rule-кодов."""
        known_rules = {
            "LSP-H-001", "LSP-H-002", "LSP-H-004",
            "OCP-H-001", "OCP-H-002", "OCP-H-004",
        }
        found_rules = {f.rule for f in heuristic_result.findings}
        unknown = found_rules - known_rules
        assert not unknown, f"Unknown rule codes appeared: {unknown}"

# ---------------------------------------------------------------------------
# Тест 2: отсутствие ложных срабатываний на чистом коде
# ---------------------------------------------------------------------------

class TestNoFalsePositivesOnCleanCode:

    def test_clean_classes_produce_no_findings(self, smelly_project_dir):
        """Классы из clean.py не должны давать ни одного finding."""
        pm = build_project_map([str(smelly_project_dir / "clean.py")])
        result = identify_candidates(pm)
        assert result.findings == [], (
            f"Expected no findings for clean code, got: "
            f"{[f.rule for f in result.findings]}"
        )

    def test_clean_classes_not_in_candidates_as_findings(self, smelly_project_dir):
        """Чистые классы не должны попадать в кандидаты по причинам эвристик."""
        pm = build_project_map([str(smelly_project_dir / "clean.py")])
        result = identify_candidates(pm)
        for candidate in result.candidates:
            assert candidate.heuristic_reasons == [], (
                f"Clean class '{candidate.class_name}' unexpectedly has "
                f"heuristic reasons: {candidate.heuristic_reasons}"
            )

# ---------------------------------------------------------------------------
# Тест 3: корректность метаданных findings
# ---------------------------------------------------------------------------

class TestFindingMetadataIntegrity:

    def test_all_findings_have_required_fields(self, heuristic_result):
        """Каждый finding должен иметь все обязательные поля."""
        for finding in heuristic_result.findings:
            assert finding.rule, f"Empty rule in finding: {finding}"
            assert finding.file, f"Empty file in finding: {finding}"
            assert finding.message, f"Empty message in finding: {finding}"
            assert finding.source == "heuristic", f"Wrong source: {finding.source}"
            assert finding.severity in ("warning", "info"), f"Wrong severity: {finding.severity}"
            assert finding.details is not None, f"Missing details in: {finding}"

    def test_lsp_rules_have_lsp_principle(self, heuristic_result):
        """Все findings с rule=LSP-H-* должны иметь details.principle == 'LSP'."""
        lsp_findings = [f for f in heuristic_result.findings if f.rule.startswith("LSP")]
        assert lsp_findings, "Expected at least one LSP finding"
        for finding in lsp_findings:
            assert finding.details.principle == "LSP", (
                f"Rule {finding.rule} has wrong principle: {finding.details.principle}"
            )

    def test_ocp_rules_have_ocp_principle(self, heuristic_result):
        """Все findings с rule=OCP-H-* должны иметь details.principle == 'OCP'."""
        ocp_findings = [f for f in heuristic_result.findings if f.rule.startswith("OCP")]
        assert ocp_findings, "Expected at least one OCP finding"
        for finding in ocp_findings:
            assert finding.details.principle == "OCP", (
                f"Rule {finding.rule} has wrong principle: {finding.details.principle}"
            )

# ---------------------------------------------------------------------------
# Тест 4: корректность кандидатов (candidates)
# ---------------------------------------------------------------------------

class TestCandidatesIntegrity:

    def test_smelly_classes_are_candidates(self, heuristic_result):
        """Классы с нарушениями должны попасть в candidates."""
        expected_candidates = {
            "XmlSerializer",
            "SilentNotifier",
            "AppConfig",
            "ShapeRenderer",
            "ComplexProcessor",
            "HighPrioritySmell",
        }
        candidate_names = {c.class_name for c in heuristic_result.candidates}
        missing = expected_candidates - candidate_names
        assert not missing, f"Expected classes not in candidates: {missing}"

    def test_high_priority_class_ranked_first(self, heuristic_result):
        """HighPrioritySmell должен иметь высокий приоритет."""
        priorities = [c.priority for c in heuristic_result.candidates]
        assert priorities == sorted(priorities, reverse=True), (
            "Candidates are not sorted by priority in descending order"
        )
        top_names = [c.class_name for c in heuristic_result.candidates[:3]]
        assert "HighPrioritySmell" in top_names, (
            f"HighPrioritySmell not in top-3 candidates: {top_names}"
        )

    def test_candidates_have_valid_candidate_type(self, heuristic_result):
        valid_types = {"ocp", "lsp", "both"}
        for candidate in heuristic_result.candidates:
            assert candidate.candidate_type in valid_types

# ---------------------------------------------------------------------------
# OCP-H-002 (match/case): интеграционный тест 
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.version_info < (3, 10),
    reason="match/case syntax requires Python 3.10+"
)
class TestOcpH002Integration:

    @pytest.fixture
    def match_project_dir(self, tmp_path_factory):
        base = tmp_path_factory.mktemp("match_project")
        (base / "dispatcher.py").write_text(textwrap.dedent("""
        class Created: pass
        class Updated: pass
        class Deleted: pass

        class EventDispatcher:
            def dispatch(self, event):
                match event:
                    case Created(): pass
                    case Updated(): pass
                    case Deleted(): pass

        class TwoBranchDispatcher:
            def route(self, cmd):
                match cmd:
                    case Created(): pass
                    case Updated(): pass
        """), encoding="utf-8")
        return base

    def test_ocp_h002_finding_present(self, match_project_dir):
        pm = build_project_map([match_project_dir])
        result = identify_candidates(pm)
        rules = [f.rule for f in result.findings]
        assert "OCP-H-002" in rules

    def test_ocp_h002_candidate_registered(self, match_project_dir):
        pm = build_project_map([match_project_dir])
        result = identify_candidates(pm)
        candidate_names = [c.class_name for c in result.candidates]
        assert "EventDispatcher" in candidate_names

# ===========================================================================
# Интеграционный тест дедупликации (findings + candidates)
# ===========================================================================

class TestHeuristicsDedupIntegration:
    """
    Интеграционный тест: на одном и том же методе срабатывают OCP-H-001 и OCP-H-004.
    Проверяем, что findings дедуплицированы.
    """

    def test_findings_and_candidates_are_deduplicated(self, tmp_path: Path):
        source = """
        class Circle: pass
        class Square: pass
        class Triangle: pass
        class Hexagon: pass
        class Pentagon: pass
        
        class ShapeRenderer:
            def render(self, shape, value):
                if isinstance(shape, Circle): pass
                elif isinstance(shape, Square): pass
                elif isinstance(shape, Triangle): pass
                elif isinstance(shape, Hexagon): pass
                elif isinstance(shape, Pentagon): pass
                else: pass

                if value > 10: value += 1
                if value < -10: value -= 1
                if value == 0: value = 42
                if value % 2 == 0: value *= 2
                if value == 84: value //= 2

                return value
        """
        module_path = tmp_path / "shapes_module.py"
        module_path.write_text(textwrap.dedent(source), encoding="utf-8")

        pm = build_project_map([module_path])
        # Явно отключаем дефолтную фильтрацию путей (exclude_patterns=[]), 
        # иначе папка, сгенерированная pytest (содержащая "test_" в пути), 
        # приведет к полному игнорированию файла нашими эвристиками.
        result = identify_candidates(pm, exclude_patterns=[])

        ocp_h001 = [f for f in result.findings if f.rule == "OCP-H-001"]
        assert len(ocp_h001) == 1
        
        winner = ocp_h001[0]
        assert winner.details is not None
        assert "Also detected: OCP-H-004" in (winner.details.explanation or "")

        ocp_h004 = [f for f in result.findings if f.rule == "OCP-H-004"]
        assert ocp_h004 == []

        shape_candidates = [c for c in result.candidates if c.class_name == "ShapeRenderer"]
        assert len(shape_candidates) == 1
        
        candidate = shape_candidates[0]
        assert "OCP-H-001" in candidate.heuristic_reasons
        assert "OCP-H-004" in candidate.heuristic_reasons