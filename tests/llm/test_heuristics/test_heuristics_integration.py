"""
Интеграционные тесты Шага 3: полный цикл build_project_map → identify_candidates.

В отличие от unit-тестов отдельных эвристик в пакете test_heuristics/,
здесь мы проверяем всю цепочку на реальных Python-файлах:
build_project_map([paths]) → ProjectMap → identify_candidates(pm) → HeuristicResult

Каждый тест проверяет один аспект интеграции: что конкретный rule-код попадает в findings,
что чистый код не порождает ложных срабатываний, что метаданные корректны и т.д.
"""
import textwrap
import pytest
import sys
import ast
from pathlib import Path

from solid_dashboard.llm.types import HeuristicResult
from solid_dashboard.llm.ast_parser import build_project_map
from solid_dashboard.llm.heuristics import lsp_h_001, lsp_h_002, identify_candidates
from solid_dashboard.llm.heuristics._runner import _deduplicate_findings
from solid_dashboard.llm.class_role import ClassRole, classify_class

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Фикстуры: один тестовый Python-файл на несколько тестов
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def smelly_project_dir(tmp_path_factory):
    """
    Создает временную директорию с двумя Python-файлами:
    - smells.py — классы с намеренными нарушениями для каждой эвристики
      (DOMAIN-классы, проходящие через весь пайплайн).
    - clean.py — классы без нарушений (для проверки отсутствия false positives).

    scope="module" — файлы создаются один раз для всего модуля тестов
    для экономии времени.
    """
    base = tmp_path_factory.mktemp("integration_project")

    # --- smells.py: по одному устойчивому нарушению на каждую эвристику ---
    (base / "smells.py").write_text(textwrap.dedent("""
        # Классы-«носители» нарушений для интеграционных тестов.
        # Каждый класс моделирует реальный DOMAIN-кейс, а не CONFIG/INFRA.

        # === Общая доменная база для LSP-сценариев ===
        class BaseSerializer:
            def serialize(self, data):
                return str(data)

        class BaseNotifier:
            def notify(self, message):
                return f"notify:{message}"

        class BaseService:
            def __init__(self):
                self.enabled = True

            def execute(self, payload):
                return payload

        # === LSP-H-001: override метод бросает NotImplementedError ===
        class XmlSerializer(BaseSerializer):
            def __init__(self):
                self.format = "xml"  # <-- ДОБАВЛЕНО: Делает класс DOMAIN, а не PURE_INTERFACE

            def serialize(self, data):
                raise NotImplementedError("XML not supported")

        # === LSP-H-002: override метод с пустым телом ===
        class SilentNotifier(BaseNotifier):
            def __init__(self):
                self.muted = True  # <-- ДОБАВЛЕНО: Делает класс DOMAIN, а не PURE_INTERFACE

            def notify(self, message):
                pass  # Намеренно пустой override

        # === LSP-H-004: __init__ без super().__init__() у DOMAIN-класса ===
        class BrokenService(BaseService):
            def __init__(self):
                # Намеренно не вызываем super().__init__()
                self.service_name = "broken"

            def execute(self, payload):
                return payload

        # === OCP-H-001: цепочка if/elif с isinstance >= 4 ветвей ===
        class Circle:
            pass

        class Square:
            pass

        class Triangle:
            pass

        class Hexagon:
            pass

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
                return "unknown"

        # === OCP-H-004: высокая CC + isinstance ===
        class BaseReport:
            pass

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

            def _validate(self, item):
                return None

            def _transform(self, item):
                return None

            def _enrich(self, item):
                return None

            def _special_report_handling(self, item):
                return None

        # === Класс с несколькими нарушениями: высокий приоритет ===
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
    # OCP-H-002 (match/case) покрывается отдельным интеграционным тестом.
    # Здесь проверяем, что основной sample-project стабильно генерирует
    # все ожидаемые rule-коды для базового набора OCP/LSP эвристик.
    EXPECTED_RULES = {
        "LSP-H-001",
        "LSP-H-002",
        "LSP-H-004",
        "OCP-H-001",
        "OCP-H-004",
    }

    def test_all_expected_rules_present(self, heuristic_result):
        """Каждый ожидаемый rule-код должен встретиться хотя бы в одном finding."""
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
        """Все smell-классы из интеграционного sample должны попасть в candidates."""
        expected_candidates = {
            "XmlSerializer",
            "SilentNotifier",
            "BrokenService",
            "ShapeRenderer",
            "ComplexProcessor",
            "HighPrioritySmell",
        }

        candidate_names = {c.class_name for c in heuristic_result.candidates}
        missing = expected_candidates - candidate_names
        assert not missing, f"Expected classes not in candidates: {missing}"

    def test_high_priority_class_ranked_first(self, heuristic_result):
        """Класс с несколькими нарушениями должен быть среди верхних кандидатов."""
        priorities = [c.priority for c in heuristic_result.candidates]
        assert priorities == sorted(priorities, reverse=True), (
            "Candidates are not sorted by priority in descending order"
        )

        top_names = [c.class_name for c in heuristic_result.candidates[:3]]
        assert "HighPrioritySmell" in top_names, (
            f"HighPrioritySmell not in top-3 candidates: {top_names}"
        )

    def test_candidates_have_valid_candidate_type(self, heuristic_result):
        """Каждый кандидат должен иметь валидный агрегированный тип."""
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


# ===========================================================================
# Тесты Шага 1 (классификатор ролей):
# Интеграция classify_class() + логика фильтрации эвристик
# ===========================================================================
# ---------------------------------------------------------------------------
# TestHeuristicsClassRoleIntegration
#
# Интеграционные тесты для трех специфических граничных случаев, обнаруженных
# при анализе работы эвристик LSP и OCP на реальных кодовых базах:
#
#   Кейс A (LSP-H-004): ABC-интерфейс без собственного __init__ давал
#     false positive в LSP-эвристике, т.к. конкретизация сигнатуры
#     в подклассе — легитимная реализация, не нарушение LSP
#
#   Кейс B (OCP / INFRA_MODEL): Pydantic BaseModel и SQLAlchemy Base
#     создавали шум в кандидатах OCP с priority:1, тогда как
#     INFRA_MODEL должен быть исключен до передачи в LLM
#
#   Кейс C (OCP / CONFIG): Settings(BaseSettings) — конфигурационный класс,
#     не подходит для SOLID-анализа
#
# Каждый тест проверяет не только classify_class() в изоляции,
# но и логику фильтрации эвристик (role != DOMAIN -> skip)
#
# Запуск:
#   pytest tools/solid_verifier/tests/llm/test_heuristics/test_heuristics_integration.py \
#          -v -k "TestHeuristicsClassRoleIntegration"
# ---------------------------------------------------------------------------


def _first_class(source: str) -> ast.ClassDef:
    """Парсит исходник и возвращает первый ClassDef верхнего уровня."""
    tree = ast.parse(textwrap.dedent(source))
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            return node
    raise ValueError("ClassDef не найден в исходнике")


def _should_skip_for_solid(
    class_node: ast.ClassDef,
    import_aliases: dict[str, str] | None = None,
) -> bool:
    """
    Имитирует логику фильтрации на входе эвристик LSP/OCP.
    Возвращает True, если класс должен быть исключен из SOLID-анализа
    (не является DOMAIN-классом).
    """
    # Эвристики пропускают все, что не DOMAIN
    role = classify_class(class_node, import_aliases=import_aliases)
    return role != ClassRole.DOMAIN


class TestHeuristicsClassRoleIntegration:

    # -----------------------------------------------------------------------
    # Кейс A: LSP-H-004 — ABC-интерфейс без __init__
    # -----------------------------------------------------------------------

    def test_lsp_h004_abc_without_init_is_pure_interface(self):
        """
        Кейс A-1: ABC с только @abstractmethod и без __init__
        должен получать роль PURE_INTERFACE, а не DOMAIN.
        LSP-эвристика должна пропускать таких кандидатов.
        """
        node = _first_class("""
            from abc import ABC, abstractmethod
            class IPaymentGateway(ABC):
                @abstractmethod
                def charge(self, amount: float) -> bool: ...
                @abstractmethod
                def refund(self, transaction_id: str) -> bool: ...
        """)
        # Ожидаем PURE_INTERFACE — не DOMAIN
        assert classify_class(node) == ClassRole.PURE_INTERFACE
        # LSP-эвристика должна пропустить этот класс
        assert _should_skip_for_solid(node) is True

    def test_lsp_h004_abc_with_docstrings_only_is_pure_interface(self):
        """
        Кейс A-2: ABC-методы с только docstring (без pass / ... / raise)
        тоже считаются тривиальными — PURE_INTERFACE.
        Дополнительный граничный случай для ABC-контрактов с docstring-описанием.
        """
        node = _first_class("""
            from abc import ABC, abstractmethod
            class INotifier(ABC):
                @abstractmethod
                def send(self, message: str) -> None:
                    \"\"\"Отправляет уведомление получателю.\"\"\"
                @abstractmethod
                def is_connected(self) -> bool:
                    \"\"\"Возвращает статус соединения.\"\"\"
        """)
        assert classify_class(node) == ClassRole.PURE_INTERFACE
        assert _should_skip_for_solid(node) is True

    def test_lsp_h004_abc_with_real_init_is_domain(self):
        """
        Кейс A-3: ABC с реальным __init__ — это конкретный базовый класс,
        НЕ чистый интерфейс. LSP-эвристика должна его анализировать (DOMAIN).
        Гарантируем, что реальный __init__ не скрывается как PURE_INTERFACE.
        """
        node = _first_class("""
            from abc import ABC, abstractmethod
            class BaseValidator(ABC):
                def __init__(self, strict: bool = False):
                    self.strict = strict  # реальная инициализация состояния
                @abstractmethod
                def validate(self, data: dict) -> bool: ...
        """)
        # Не PURE_INTERFACE — есть реальный __init__
        assert classify_class(node) != ClassRole.PURE_INTERFACE
        # DOMAIN — LSP-эвристика должна его проверять
        assert classify_class(node) == ClassRole.DOMAIN
        assert _should_skip_for_solid(node) is False

    def test_lsp_h004_concrete_subclass_without_override_is_domain(self):
        """
        Кейс A-4: Конкретный подкласс ABC-интерфейса — DOMAIN.
        LSP-эвристика должна проверять именно таких кандидатов.
        """
        node = _first_class("""
            class StripeGateway(IPaymentGateway):
                def __init__(self, api_key: str):
                    self.api_key = api_key  # конкретная реализация
                def charge(self, amount: float) -> bool:
                    return True
                def refund(self, transaction_id: str) -> bool:
                    return True
        """)
        assert classify_class(node) == ClassRole.DOMAIN
        assert _should_skip_for_solid(node) is False

    # -----------------------------------------------------------------------
    # Кейс B: OCP — Pydantic BaseModel и SQLAlchemy Base как шум
    # -----------------------------------------------------------------------

    def test_ocp_pydantic_base_model_is_infra_not_domain(self):
        """
        Кейс B-1: Pydantic BaseModel — типичный источник шума в кандидатах OCP.
        Должен получать роль INFRA_MODEL и исключаться из SOLID-анализа.
        """
        node = _first_class("""
            class OrderCreateSchema(BaseModel):
                product_id: int
                quantity: int
                discount: float = 0.0
        """)
        assert classify_class(node) == ClassRole.INFRA_MODEL
        # OCP-эвристика должна пропустить этот класс
        assert _should_skip_for_solid(node) is True

    def test_ocp_pydantic_base_model_via_alias_is_infra(self):
        """
        Кейс B-2: Pydantic BaseModel через алиас (from pydantic import BaseModel as BM).
        Алиас должен разрешаться, класс должен получать INFRA_MODEL.
        Без корректной обработки алиаса — даст ложный DOMAIN и шум в OCP.
        """
        node = _first_class("""
            class ProductResponseSchema(BM):
                id: int
                name: str
                price: float
                in_stock: bool
        """)
        # Без алиаса — DOMAIN (BM неизвестен классификатору)
        assert classify_class(node, import_aliases={}) == ClassRole.DOMAIN
        # С алиасом — должен стать INFRA_MODEL (шум устранен)
        assert classify_class(node, import_aliases={"BM": "BaseModel"}) == ClassRole.INFRA_MODEL
        assert _should_skip_for_solid(node, {"BM": "BaseModel"}) is True

    def test_ocp_sqlalchemy_orm_via_tablename_is_infra(self):
        """
        Кейс B-3: SQLAlchemy ORM через Base (не в KNOWN_INFRA_BASES).
        Детектируется через InfraScore: __tablename__ (+1) + Column() (+1) = 2.
        Должен получать INFRA_MODEL и исключаться из OCP-кандидатов.
        """
        node = _first_class("""
            class Invoice(Base):
                __tablename__ = 'invoices'
                id = Column(Integer, primary_key=True)
                amount = Column(Numeric(10, 2))
                paid = Column(Boolean, default=False)
        """)
        # "Base" не в KNOWN_INFRA_BASES, но InfraScore >= 2 через сигналы
        assert classify_class(node) == ClassRole.INFRA_MODEL
        assert _should_skip_for_solid(node) is True

    def test_ocp_domain_class_with_many_fields_is_still_domain(self):
        """
        Кейс B-4: Доменный класс с большим количеством аннотированных атрибутов
        НЕ должен ошибочно попадать в INFRA_MODEL.
        Высокий AnnAssign-ratio сам по себе не дает INFRA_MODEL (порог < 2).
        """
        node = _first_class("""
            class ReportConfig:
                title: str
                author: str
                date: str
                include_charts: bool
                max_rows: int
        """)
        # Только AnnAssign ratio (+1) — недостаточно для INFRA_MODEL (порог 2)
        assert classify_class(node) == ClassRole.DOMAIN
        assert _should_skip_for_solid(node) is False

    # -----------------------------------------------------------------------
    # Кейс C: Settings(BaseSettings) — конфигурационные классы
    # -----------------------------------------------------------------------

    def test_ocp_base_settings_is_config_not_domain(self):
        """
        Кейс C-1: Прямой наследник BaseSettings получает роль CONFIG
        и должен исключаться из SOLID-анализа OCP/LSP.
        """
        node = _first_class("""
            class AppSettings(BaseSettings):
                database_url: str
                redis_url: str
                debug: bool = False
                secret_key: str = ''
        """)
        assert classify_class(node) == ClassRole.CONFIG
        assert _should_skip_for_solid(node) is True

    def test_ocp_settings_subclass_chain_is_config(self):
        """
        Кейс C-2: Подкласс Settings (через цепочку BaseSettings → Settings).
        Прямое наследование от Settings также должно давать CONFIG.
        Гарантируем, что цепочка конфиг-иерархии полностью исключается.
        """
        node = _first_class("""
            class ProductionSettings(Settings):
                debug: bool = False
                allowed_hosts: list = []
        """)
        assert classify_class(node) == ClassRole.CONFIG
        assert _should_skip_for_solid(node) is True

    def test_ocp_base_config_is_config(self):
        """
        Кейс C-3: Pydantic BaseConfig / собственный BaseConfig проекта.
        Оба паттерна должны давать CONFIG и исключаться из анализа.
        """
        node = _first_class("""
            class DatabaseConfig(BaseConfig):
                host: str = 'localhost'
                port: int = 5432
                name: str = 'mydb'
        """)
        assert classify_class(node) == ClassRole.CONFIG
        assert _should_skip_for_solid(node) is True

    def test_ocp_config_class_name_without_config_base_is_domain(self):
        """
        Кейс C-4: Класс с 'Config' в имени, но без Config-базы в наследовании —
        это НЕ конфигурационный класс в смысле SOLID.
        Проверяем, что классификация идет по базовым классам, а не по имени класса.
        """
        node = _first_class("""
            class ReportConfig:
                def __init__(self, output_format: str):
                    self.output_format = output_format
                def to_dict(self) -> dict:
                    return {'format': self.output_format}
        """)
        # Имя содержит "Config", но наследования нет — должен быть DOMAIN
        assert classify_class(node) == ClassRole.DOMAIN
        assert _should_skip_for_solid(node) is False

class TestIntegrationProjectMapDiagnostics:
    def test_lsp_sample_classes_have_expected_parser_metadata(self, smelly_project_dir):
        """Проверяем, что интеграционный sample действительно строит корректный ProjectMap."""
        pm = build_project_map([
            str(smelly_project_dir / "smells.py"),
            str(smelly_project_dir / "clean.py"),
        ])

        # Проверяем наличие классов в карте проекта
        assert "XmlSerializer" in pm.classes
        assert "SilentNotifier" in pm.classes

        xml_info = pm.classes["XmlSerializer"]
        notifier_info = pm.classes["SilentNotifier"]

        # Проверяем базовые классы
        assert "BaseSerializer" in xml_info.parent_classes
        assert "BaseNotifier" in notifier_info.parent_classes

        # Проверяем наличие методов
        xml_methods = {m.name: m for m in xml_info.methods}
        notifier_methods = {m.name: m for m in notifier_info.methods}

        assert "serialize" in xml_methods
        assert "notify" in notifier_methods

        # Ключевая диагностика: реально ли parser считает их override
        assert xml_methods["serialize"].is_override is True, (
            "XmlSerializer.serialize должен быть override для интеграционного LSP-H-001"
        )
        assert notifier_methods["notify"].is_override is True, (
            "SilentNotifier.notify должен быть override для интеграционного LSP-H-002"
        )

class TestLspPipelineDiagnostics:
    def test_xml_and_silent_notifier_survive_full_lsp_pipeline(self, smelly_project_dir):
        """
        Диагностика полного LSP-пути:
        1) классы есть в ProjectMap
        2) raw-check функции дают findings
        3) findings не теряются до финального HeuristicResult
        """

        pm = build_project_map([
            str(smelly_project_dir / "smells.py"),
            str(smelly_project_dir / "clean.py"),
        ])

        assert "XmlSerializer" in pm.classes
        assert "SilentNotifier" in pm.classes

        xml_info = pm.classes["XmlSerializer"]
        silent_info = pm.classes["SilentNotifier"]

        xml_node = ast.parse(xml_info.source_code).body[0]
        silent_node = ast.parse(silent_info.source_code).body[0]

        assert isinstance(xml_node, ast.ClassDef)
        assert isinstance(silent_node, ast.ClassDef)

        # Сырые findings отдельных эвристик
        xml_findings = lsp_h_001.check(xml_node, xml_info, pm)
        silent_findings = lsp_h_002.check(silent_node, silent_info, pm)

        assert xml_findings, "XmlSerializer не дал raw finding для LSP-H-001"
        assert silent_findings, "SilentNotifier не дал raw finding для LSP-H-002"

        # Проверяем, что дедупликация не убивает их полностью
        merged = _deduplicate_findings(xml_findings + silent_findings)
        merged_keys = {(f.class_name, f.rule) for f in merged}

        assert ("XmlSerializer", "LSP-H-001") in merged_keys
        assert ("SilentNotifier", "LSP-H-002") in merged_keys

        # Финальный результат пайплайна тоже обязан их содержать
        result = identify_candidates(pm)
        result_rules = {(f.class_name, f.rule) for f in result.findings}
        candidate_names = {c.class_name for c in result.candidates}

        assert ("XmlSerializer", "LSP-H-001") in result_rules
        assert ("SilentNotifier", "LSP-H-002") in result_rules
        assert "XmlSerializer" in candidate_names
        assert "SilentNotifier" in candidate_names
