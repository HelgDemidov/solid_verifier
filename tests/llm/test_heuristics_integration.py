"""
Интеграционные тесты Шага 3: полный цикл build_project_map → identify_candidates.

В отличие от test_heuristics.py (unit-тесты отдельных эвристик через _pm_from_source),
здесь мы проверяем всю цепочку на реальных Python-файлах:
  build_project_map([paths]) → ProjectMap → identify_candidates(pm) → HeuristicResult

Каждый тест проверяет один аспект интеграции: что конкретный rule-код попадает в findings,
что чистый код не порождает ложных срабатываний, что метаданные корректны и т.д.
"""
# ---------------------------------------------------------------------------
# НАПОМИНАЛКА ПО КОМАНДАМ ЗАПУСКА ТЕСТОВ
# ---------------------------------------------------------------------------

# Только unit-тесты (быстро): pytest tools/solid_verifier/tests/llm/test_heuristics.py -v
# Только интеграционные тесты: pytest -m integration -v
# Всё вместе: pytest tools/solid_verifier/tests/llm/ -v

import textwrap
import pytest

from tools.solid_verifier.solid_dashboard.llm.ast_parser import build_project_map
from tools.solid_verifier.solid_dashboard.llm.heuristics import identify_candidates
from tools.solid_verifier.solid_dashboard.llm.types import HeuristicResult

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Фикстуры: один тестовый Python-файл на несколько тестов
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def smelly_project_dir(tmp_path_factory):
    """
    Создаёт временную директорию с двумя Python-файлами:
    - smells.py — классы с намеренными нарушениями для каждой эвристики.
    - clean.py  — классы без нарушений (для проверки отсутствия false positives).

    scope="module" — файлы создаются один раз для всего модуля тестов.
    Это экономит время: build_project_map вызывается не на каждый тест.
    """
    base = tmp_path_factory.mktemp("integration_project")

    # --- smells.py: по одному нарушению на каждую эвристику ---
    # Намеренно пишем с реальными именами методов и структурами,
    # чтобы интеграционный тест был ближе к боевым условиям.
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


        # === LSP-H-003: isinstance на параметре базового типа ===

        class BaseReport:
            def render(self): ...


        class ReportExporter:
            def export(self, report: BaseReport) -> None:
                # Метод принимает BaseReport, но явно проверяет конкретный тип
                if isinstance(report, BaseReport):
                    report.render()


        # === LSP-H-004: __init__ без super().__init__() ===

        class BaseConfig:
            def __init__(self):
                self.debug = False


        class AppConfig(BaseConfig):
            def __init__(self):
                # Намеренно не вызываем super().__init__() — нарушение LSP
                self.app_name = "MyApp"


        # === OCP-H-001: цепочка if/elif с isinstance >= 3 ветвей ===

        class ShapeRenderer:
            def render(self, shape):
                if isinstance(shape, BaseReport):
                    self._draw_report(shape)
                elif isinstance(shape, BaseSerializer):
                    self._draw_serial(shape)
                elif isinstance(shape, BaseNotifier):
                    self._draw_notify(shape)

            def _draw_report(self, s): ...
            def _draw_serial(self, s): ...
            def _draw_notify(self, s): ...


        # === OCP-H-003: словарь-диспетчер с ключами-типами ===

        class DispatchEngine:
            def setup(self):
                # Словарь с тремя ключами-классами из проекта — OCP-запах
                self._handlers = {
                    BaseSerializer: self._handle_serializer,
                    BaseNotifier:   self._handle_notifier,
                    BaseReport:     self._handle_report,
                }

            def _handle_serializer(self, obj): ...
            def _handle_notifier(self, obj): ...
            def _handle_report(self, obj): ...


        # === OCP-H-004: высокая CC + isinstance ===

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

    Результат переиспользуется всеми тестами модуля без повторного вызова
    build_project_map — так мы проверяем одно состояние системы во всех тестах.
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

    # OCP-H-002 (match/case) намеренно исключён из smells.py —
    # он требует Python 3.10+ и проверен отдельно в unit-тестах.
    EXPECTED_RULES = {
        "LSP-H-001",
        "LSP-H-002",
        "LSP-H-003",
        "LSP-H-004",
        "OCP-H-001",
        "OCP-H-003",
        "OCP-H-004",
    }

    def test_all_expected_rules_present(self, heuristic_result):
        """
        Каждый из 7 rule-кодов должен встретиться хотя бы в одном finding.
        Если хоть один отсутствует — какая-то эвристика перестала работать
        или build_project_map перестал корректно парсить нужный класс.
        """
        found_rules = {f.rule for f in heuristic_result.findings}
        missing = self.EXPECTED_RULES - found_rules
        # Выводим, каких именно rules не хватает, чтобы падение было информативным
        assert not missing, f"Missing rule codes in findings: {missing}"

    def test_no_unexpected_rule_codes(self, heuristic_result):
        """
        В findings не должно появляться неизвестных rule-кодов.
        Защита от случайных «фантомных» эвристик при рефакторинге.
        """
        known_rules = {
            "LSP-H-001", "LSP-H-002", "LSP-H-003", "LSP-H-004",
            "OCP-H-001", "OCP-H-002", "OCP-H-003", "OCP-H-004",
        }
        found_rules = {f.rule for f in heuristic_result.findings}
        unknown = found_rules - known_rules
        assert not unknown, f"Unknown rule codes appeared: {unknown}"


# ---------------------------------------------------------------------------
# Тест 2: отсутствие ложных срабатываний на чистом коде
# ---------------------------------------------------------------------------

class TestNoFalsePositivesOnCleanCode:

    def test_clean_classes_produce_no_findings(self, smelly_project_dir):
        """
        Классы из clean.py не должны давать ни одного finding.

        Строим отдельный ProjectMap только из clean.py — так мы изолируем
        «чистый» контекст от smells.py и проверяем именно false positives.
        """
        pm = build_project_map([str(smelly_project_dir / "clean.py")])
        result = identify_candidates(pm)
        assert result.findings == [], (
            f"Expected no findings for clean code, got: "
            f"{[f.rule for f in result.findings]}"
        )

    def test_clean_classes_not_in_candidates_as_findings(self, smelly_project_dir):
        """
        DataTransformer и StringNormalizer не должны попасть в candidates
        с heuristic_reasons (только в иерархии могут быть как «потенциальные»).
        """
        pm = build_project_map([str(smelly_project_dir / "clean.py")])
        result = identify_candidates(pm)
        # У чистых standalone-классов не должно быть heuristic_reasons
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
        """
        Каждый finding должен иметь все обязательные поля с непустыми значениями.
        Защита от случайного изменения _make_finding() или Finding-dataclass.
        """
        for finding in heuristic_result.findings:
            assert finding.rule,                 f"Empty rule in finding: {finding}"
            assert finding.file,                 f"Empty file in finding: {finding}"
            assert finding.message,              f"Empty message in finding: {finding}"
            assert finding.source == "heuristic", f"Wrong source: {finding.source}"
            assert finding.severity == "warning", f"Wrong severity: {finding.severity}"
            assert finding.details is not None,   f"Missing details in: {finding}"

    def test_lsp_rules_have_lsp_principle(self, heuristic_result):
        """
        Все findings с rule=LSP-H-* должны иметь details.principle == 'LSP'.
        Несоответствие — признак copy-paste ошибки при добавлении новой эвристики.
        """
        lsp_findings = [f for f in heuristic_result.findings if f.rule.startswith("LSP")]
        assert lsp_findings, "Expected at least one LSP finding"
        for finding in lsp_findings:
            assert finding.details.principle == "LSP", (
                f"Rule {finding.rule} has wrong principle: {finding.details.principle}"
            )

    def test_ocp_rules_have_ocp_principle(self, heuristic_result):
        """
        Все findings с rule=OCP-H-* должны иметь details.principle == 'OCP'.
        """
        ocp_findings = [f for f in heuristic_result.findings if f.rule.startswith("OCP")]
        assert ocp_findings, "Expected at least one OCP finding"
        for finding in ocp_findings:
            assert finding.details.principle == "OCP", (
                f"Rule {finding.rule} has wrong principle: {finding.details.principle}"
            )

    def test_findings_reference_existing_files(self, heuristic_result, smelly_project_dir):
        """
        Поле finding.file должно указывать на реально существующий файл.
        Защита от случайного дрейфа путей между build_project_map и findings.
        """
        import os
        for finding in heuristic_result.findings:
            assert os.path.isfile(finding.file), (
                f"Finding references non-existent file: {finding.file}"
            )


# ---------------------------------------------------------------------------
# Тест 4: корректность кандидатов (candidates)
# ---------------------------------------------------------------------------

class TestCandidatesIntegrity:

    def test_smelly_classes_are_candidates(self, heuristic_result):
        """
        Классы с нарушениями должны попасть в candidates.
        Проверяем конкретные имена из smells.py.
        """
        # Эти классы имеют хотя бы одно эвристическое срабатывание
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
        """
        HighPrioritySmell (два нарушения: LSP-H-001 + LSP-H-004) должен иметь
        более высокий приоритет, чем классы с одним нарушением.

        Проверяем инвариант сортировки: приоритет строго убывает (или не растёт).
        """
        priorities = [c.priority for c in heuristic_result.candidates]
        assert priorities == sorted(priorities, reverse=True), (
            "Candidates are not sorted by priority in descending order"
        )

        # HighPrioritySmell должен быть в первой тройке по приоритету
        top_names = [c.class_name for c in heuristic_result.candidates[:3]]
        assert "HighPrioritySmell" in top_names, (
            f"HighPrioritySmell not in top-3 candidates: {top_names}"
        )

    def test_candidates_have_valid_candidate_type(self, heuristic_result):
        """
        Поле candidate_type должно быть одним из допустимых значений.
        """
        valid_types = {"ocp", "lsp", "both"}
        for candidate in heuristic_result.candidates:
            assert candidate.candidate_type in valid_types, (
                f"Invalid candidate_type '{candidate.candidate_type}' "
                f"for class '{candidate.class_name}'"
            )