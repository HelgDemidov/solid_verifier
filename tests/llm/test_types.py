"""
Базовые тесты инстанциирования контрактов (Шаг 1).
Цель: убедиться, что все типы импортируются, создаются и содержат ожидаемые значения.
Бизнес-логика здесь не проверяется.
"""

import pytest

from solid_dashboard.llm.types import (
    MethodSignature,
    ClassInfo,
    InterfaceInfo,
    ProjectMap,
    LlmCandidate,
    HeuristicResult,
    LlmConfig,
    LlmAnalysisInput,
    LlmAnalysisOutput,
    LlmMetadata,
    Finding,
    FindingDetails,
)


# ---------------------------------------------------------------------------
# Фикстуры — минимальные валидные экземпляры каждого типа
# ---------------------------------------------------------------------------

@pytest.fixture
def method_sig():
    return MethodSignature(
        name="process",
        parameters="self, value: int",
        return_type="str",
        is_override=False,
    )


@pytest.fixture
def class_info(method_sig):
    return ClassInfo(
        name="PaymentProcessor",
        file_path="src/payment_processor.py",
        source_code="class PaymentProcessor:\n    pass",
        parent_classes=[],
        implemented_interfaces=[],
        methods=[method_sig],
        dependencies=["CreditCard", "PayPal"],
    )


@pytest.fixture
def interface_info(method_sig):
    return InterfaceInfo(
        name="IPaymentStrategy",
        file_path="src/interfaces.py",
        methods=[method_sig],
        implementations=["CreditCardStrategy", "PayPalStrategy"],
    )


@pytest.fixture
def project_map(class_info, interface_info):
    return ProjectMap(
        classes={"PaymentProcessor": class_info},
        interfaces={"IPaymentStrategy": interface_info},
    )


@pytest.fixture
def llm_candidate():
    return LlmCandidate(
        class_name="PaymentProcessor",
        file_path="src/payment_processor.py",
        source_code="class PaymentProcessor:\n    pass",
        candidate_type="ocp",
        heuristic_reasons=["OCP-H-001"],
        priority=3,
    )


@pytest.fixture
def llm_config():
    return LlmConfig(
        provider="openrouter",
        model="openai/gpt-4o-mini",
        api_key="test-key",
        endpoint=None,
        max_tokens_per_run=50000,
        cache_dir=".solid-cache/llm",
        prompts_dir="prompts/",
    )


# ---------------------------------------------------------------------------
# Тесты: инстанциирование
# ---------------------------------------------------------------------------

class TestMethodSignature:
    def test_basic_creation(self, method_sig):
        assert method_sig.name == "process"
        assert method_sig.is_override is False
        assert method_sig.is_abstract is False

    def test_override_flag(self):
        sig = MethodSignature(
            name="save",
            parameters="self",
            return_type="None",
            is_override=True,
        )
        assert sig.is_override is True
        assert sig.is_abstract is False  # NEW


class TestClassInfo:
    def test_basic_creation(self, class_info):
        assert class_info.name == "PaymentProcessor"
        assert class_info.parent_classes == []
        assert len(class_info.methods) == 1

    def test_with_parents(self):
        ci = ClassInfo(
            name="Dog",
            file_path="animals.py",
            source_code="class Dog(Animal): pass",
            parent_classes=["Animal"],
            implemented_interfaces=[],
            methods=[],
            dependencies=[],
        )
        assert "Animal" in ci.parent_classes

    def test_multiple_parents(self):
        ci = ClassInfo(
            name="Mixin",
            file_path="mixin.py",
            source_code="class Mixin(A, B): pass",
            parent_classes=["A", "B"],
            implemented_interfaces=["A"],
            methods=[],
            dependencies=[],
        )
        assert len(ci.parent_classes) == 2


class TestInterfaceInfo:
    def test_basic_creation(self, interface_info):
        assert interface_info.name == "IPaymentStrategy"
        assert "CreditCardStrategy" in interface_info.implementations


class TestProjectMap:
    def test_empty_map(self):
        pm = ProjectMap()
        assert pm.classes == {}
        assert pm.interfaces == {}

    def test_with_data(self, project_map):
        assert "PaymentProcessor" in project_map.classes
        assert "IPaymentStrategy" in project_map.interfaces

    def test_default_factory_isolation(self):
        # Проверяем, что два разных ProjectMap не делят один и тот же словарь
        pm1 = ProjectMap()
        pm2 = ProjectMap()
        pm1.classes["Foo"] = None  # type: ignore
        assert "Foo" not in pm2.classes


class TestLlmCandidate:
    def test_candidate_types(self):
        for ct in ("ocp", "lsp", "both"):
            c = LlmCandidate(
                class_name="Foo",
                file_path="foo.py",
                source_code="class Foo: pass",
                candidate_type=ct,  # type: ignore
                heuristic_reasons=[],
                priority=0,
            )
            assert c.candidate_type == ct

    def test_priority_is_int(self, llm_candidate):
        assert isinstance(llm_candidate.priority, int)


class TestHeuristicResult:
    def test_empty_result(self):
        hr = HeuristicResult()
        assert hr.findings == []
        assert hr.candidates == []

    def test_with_data(self, llm_candidate):
        finding = Finding(
            rule="OCP-H-001",
            file="foo.py",
            severity="warning",
            message="isinstance chain detected",
            source="heuristic",
        )
        hr = HeuristicResult(findings=[finding], candidates=[llm_candidate])
        assert len(hr.findings) == 1
        assert len(hr.candidates) == 1


class TestLlmConfig:
    def test_basic_creation(self, llm_config):
        assert llm_config.provider == "openrouter"
        assert llm_config.api_key == "test-key"
        assert llm_config.endpoint is None

    def test_ollama_config(self):
        # Ollama не требует api_key
        cfg = LlmConfig(
            provider="ollama",
            model="llama3",
            api_key=None,
            endpoint="http://localhost:11434",
            max_tokens_per_run=20000,
            cache_dir=".solid-cache/llm",
            prompts_dir="prompts/",
        )
        assert cfg.api_key is None
        assert cfg.endpoint is not None


class TestLlmAnalysisInput:
    def test_creation(self, project_map, llm_candidate, llm_config):
        inp = LlmAnalysisInput(
            project_map=project_map,
            candidates=[llm_candidate],
        )
        assert len(inp.candidates) == 1
        # Убеждаемся, что static findings в контракте отсутствуют
        assert not hasattr(inp, "static_findings")


class TestLlmAnalysisOutput:
    def test_default_output(self):
        out = LlmAnalysisOutput()
        assert out.findings == []
        assert out.metadata.candidates_processed == 0
        assert out.metadata.cache_hits == 0

    def test_with_metadata(self):
        meta = LlmMetadata(
            candidates_processed=5,
            candidates_skipped=2,
            tokens_used=12000,
            cache_hits=3,
        )
        out = LlmAnalysisOutput(findings=[], metadata=meta)
        assert out.metadata.tokens_used == 12000


class TestFinding:
    def test_static_finding(self):
        # Симулируем finding от существующего адаптера
        f = Finding(
            rule="SRP-001",
            file="src/user_service.py",
            severity="warning",
            message="Class has too many responsibilities",
            source="static",
            class_name="UserService",
            line=15,
        )
        assert f.source == "static"
        assert f.details is None
        assert f.line == 15

    def test_heuristic_finding(self):
        f = Finding(
            rule="OCP-H-001",
            file="src/payment.py",
            severity="warning",
            message="isinstance chain detected in process()",
            source="heuristic",
            class_name="PaymentProcessor",
            line=None,
            details=FindingDetails(
                principle="OCP",
                explanation="Method contains if/elif with isinstance checks",
                suggestion="Consider Strategy pattern",
                heuristic_corroboration=None,
            ),
        )
        assert f.source == "heuristic"
        assert f.details is not None
        assert f.details.principle == "OCP"

    def test_llm_finding(self):
        f = Finding(
            rule="OCP-LLM-001",
            file="src/payment.py",
            severity="warning",
            message="Class requires modification to add new payment types",
            source="llm",
            class_name="PaymentProcessor",
            line=None,
            details=FindingDetails(
                principle="OCP",
                explanation="switch-like pattern detected",
                suggestion="Extract to strategy classes",
                analyzed_with=["PaymentProcessor", "CreditCard"],
                heuristic_corroboration=True,
            ),
        )
        assert f.severity == "warning"
        assert f.details is not None
        assert f.details.heuristic_corroboration is True

    def test_severity_info_for_unconfirmed_llm(self):
        # heuristic_corroboration=False → severity должен быть 'info'
        # (это правило назначается адаптером, не типом — тест проверяет корректность поля)
        f = Finding(
            rule="LSP-LLM-001",
            file="src/repo.py",
            severity="info",
            message="Potential LSP violation in external hierarchy",
            source="llm",
            details=FindingDetails(heuristic_corroboration=False),
        )
        assert f.severity == "info"
        assert f.details is not None  # <-- Успокаиваем Pylance
        assert f.details.heuristic_corroboration is False

    def test_finding_without_class_name(self):
        # Некоторые findings могут быть на уровне файла, без класса
        f = Finding(
            rule="DIP-001",
            file="src/module.py",
            severity="info",
            message="Direct dependency on concrete class",
            source="static",
        )
        assert f.class_name is None


class TestFindingDetails:
    def test_all_none_defaults(self):
        fd = FindingDetails()
        assert fd.principle is None
        assert fd.explanation is None
        assert fd.suggestion is None
        assert fd.analyzed_with is None
        assert fd.heuristic_corroboration is None
        assert fd.method_name is None  # NEW: дефолт для нового поля

    def test_partial_fill(self):
        fd = FindingDetails(principle="LSP", heuristic_corroboration=True)
        assert fd.principle == "LSP"
        assert fd.suggestion is None
        assert fd.method_name is None  # NEW: дефолт для нового поля

# LДобавляем тесты на иммутабельность LlmResponse и базовую инстанциацию ParseResult

from dataclasses import FrozenInstanceError
import pytest
from solid_dashboard.llm.types import LlmResponse, ParseResult

def test_llm_response_is_immutable():
    """Тест контракта ACL-A: LlmResponse должен быть заморожен (frozen=True)."""
    response = LlmResponse(content="test", tokens_used=10, model="openai/gpt-4o-mini")
    
    with pytest.raises(FrozenInstanceError):
        response.content = "new content"  # type: ignore
        
    with pytest.raises(FrozenInstanceError):
        response.tokens_used = 20  # type: ignore

def test_llm_response_defaults():
    """Тест дефолтных значений LlmResponse."""
    response = LlmResponse(content="test", tokens_used=10)
    assert response.model == ""

def test_parse_result_instantiation():
    """Тест контракта ACL-B: ParseResult корректно инстанциируется."""
    result = ParseResult(
        findings=[],
        warnings=["Some warning"],
        status="failure"
    )
    assert result.status == "failure"
    assert len(result.warnings) == 1
    assert result.findings == []