"""
Контракты и типы данных для LLM-функционала SOLID-анализатора.

Этот модуль содержит исключительно типы — никакой бизнес-логики.
Все остальные модули (buildProjectMap, identifyCandidates, LlmGateway, LlmSolidAdapter)
импортируют типы отсюда.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Вспомогательные литеральные типы (вместо Enum — для простоты сериализации)
# ---------------------------------------------------------------------------

# Источник finding: статический анализ / эвристика / LLM
SourceType = Literal["static", "heuristic", "llm"]

# Принцип-кандидат для LLM-анализа
CandidateType = Literal["ocp", "lsp", "both"]

# Уровень серьезности finding
SeverityLevel = Literal["error", "warning", "info"]


# ---------------------------------------------------------------------------
# Шаг 0: ProjectMap — граф классов и интерфейсов проекта
# ---------------------------------------------------------------------------

@dataclass
class MethodSignature:
    """Сигнатура метода класса (без тела)."""
    name: str
    parameters: str                 # строка параметров, например: "self, value: int"
    return_type: str | None        # строка аннотации возврата, например: "str | None"
    is_override: bool = False       # True, если метод переопределяет метод родителя
    is_abstract: bool = False       # NEW: метод помечен как абстрактный (@abstractmethod)

@dataclass
class ClassInfo:
    """
    Полная информация о классе, извлеченная из AST.
    source_code — полный исходный текст блока class, включая тело.
    """
    name: str
    file_path: str
    source_code: str
    parent_classes: list[str]            # имена родительских классов (текстовые)
    implemented_interfaces: list[str]    # ABC/Protocol-родители (подмножество parent_classes)
    methods: list[MethodSignature]
    dependencies: list[str]              # имена классов/модулей из импортов файла


@dataclass
class InterfaceInfo:
    """
    Информация об абстрактном базовом классе или Protocol.
    implementations — имена классов, наследующих этот интерфейс.
    """
    name: str
    file_path: str
    methods: list[MethodSignature]
    implementations: list[str]           # заполняется при построении ProjectMap


@dataclass
class ProjectMap:
    """
    Граф классов и интерфейсов проекта.
    Строится единожды (Шаг 0) и передается во все последующие компоненты.
    """
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    interfaces: dict[str, InterfaceInfo] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Шаг 1b: Результат эвристического анализа
# ---------------------------------------------------------------------------

@dataclass
class LlmCandidate:
    """
    Класс-кандидат для LLM-анализа, отобранный эвристиками.
    priority — чем выше, тем раньше обрабатывается при ограниченном бюджете.
    """
    class_name: str
    file_path: str
    source_code: str                 # полный исходный код класса
    candidate_type: CandidateType
    heuristic_reasons: list[str]     # коды эвристик: ["OCP-H-001", "LSP-H-001"]
    priority: int                    # вычисляется по формуле из архитектурного плана


@dataclass
class HeuristicResult:
    """
    Выход функции identify_candidates().
    findings идут напрямую в Report Aggregator (минуя LLM-адаптер).
    candidates идут в LlmSolidAdapter (если LLM включен).
    """
    findings: list[Finding] = field(default_factory=list)
    candidates: list[LlmCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Шаг 2: LLM-контракт
# ---------------------------------------------------------------------------

@dataclass
class LlmConfig:
    """
    Конфигурация LLM-функционала из .solid_config.json
    api_key может быть None для Ollama (локальный режим без аутентификации).
    """
    provider: str                    # "openrouter"
    model: str                       # например: "openai/gpt-4o-mini"
    api_key: str | None              # None для ollama
    endpoint: str | None             # для ollama: "http://localhost:11434"
    max_tokens_per_run: int          # бюджет в токенах за один запуск
    cache_dir: str                   # путь к директории кэша, например ".solid-cache/llm"
    prompts_dir: str                 # путь к директории с файлами промптов


@dataclass
class LlmAnalysisInput:
    """
    Вход LlmSolidAdapter.analyze()
    Содержит только данные анализа: карту проекта и список кандидатов
    Конфигурация LLM передается в адаптер через DI (LlmSolidAdapter.config)
    """
    project_map: ProjectMap
    candidates: list[LlmCandidate]


@dataclass
class LlmMetadata:
    """Метаданные выполнения LLM-анализа для summary в отчете."""
    candidates_processed: int
    candidates_skipped: int
    tokens_used: int
    cache_hits: int
    parse_failures: int = 0          # NEW: количество failure-ответов парсера
    parse_partials: int = 0          # NEW: количество partial-ответов
    parse_warnings: int = 0          # NEW: общее количество предупреждений ACL-B


@dataclass
class LlmAnalysisOutput:
    """Выход LlmSolidAdapter.analyze()."""
    findings: list[Finding] = field(default_factory=list)
    metadata: LlmMetadata = field(
        default_factory=lambda: LlmMetadata(
            candidates_processed=0,
            candidates_skipped=0,
            tokens_used=0,
            cache_hits=0,
            parse_failures=0,   # NEW: количество failure-ответов парсера
            parse_partials=0,   # NEW: количество partial-ответов
            parse_warnings=0,   # NEW: общее количество предупреждений ACL-B
        )
    )


# ---------------------------------------------------------------------------
# Общий формат Finding (используется для static, heuristic и llm findings)
# ---------------------------------------------------------------------------

@dataclass
class FindingDetails:
    """
    Расширенная информация для heuristic и llm findings.
    Все поля опциональны — статические findings details не используют.
    """
    principle: str | None = None                  # "OCP" | "LSP"
    explanation: str | None = None                # подробное объяснение нарушения
    suggestion: str | None = None                 # конкретная рекомендация
    analyzed_with: list[str] | None = None        # классы, участвовавшие в анализе
    heuristic_corroboration: bool | None = None   # True → severity=warning, False → info
    method_name: str | None = None                # NEW: имя метода, к которому относится finding


@dataclass
class Finding:
    """
    Единый формат finding для всех источников анализа.

    Именование rule:
      Существующие:  SRP-001, ISP-001, DIP-001
      Эвристики:     OCP-H-001, LSP-H-001 (см. архитектурный план)
      LLM:           OCP-LLM-001, LSP-LLM-001

    line=None для LLM-findings (LLM не может надежно указать строку).
    """
    rule: str
    file: str
    severity: SeverityLevel
    message: str
    source: SourceType
    class_name: str | None = None
    line: int | None = None
    details: FindingDetails | None = None

# Добавлен новый датакласс LlmResponse с frozen=True (в рамках Шага 1 ACL Strategy)

@dataclass(frozen=True)
class LlmResponse:
    """
    Контрактная граница (ACL-A) между провайдером и Gateway.
    Объект иммутабелен (frozen=True) и гарантированно содержит валидные данные.
    Сырые данные (dict от провайдера) не пересекают эту границу.
    """
    content: str
    tokens_used: int
    model: str = ""

# --- Типы для семантического барьера (ACL-B) ---

ParseStatus = Literal["success", "partial", "failure"]

@dataclass
class ParseResult:
    """Результат безопасного извлечения findings из ответа LLM."""
    findings: list[Finding]  # Ожидается, что тип Finding уже определен в файле
    warnings: list[str]
    status: ParseStatus