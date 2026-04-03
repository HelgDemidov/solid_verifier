# Оркестратор эвристического анализа — публичная функция identify_candidates().
#
# Шаг 1b пайплайна: принимает ProjectMap, прогоняет все 7 эвристик по каждому
# классу, дедуплицирует findings и кандидатов, возвращает HeuristicResult.
#
# Функция остается чистой: не обращается к файловой системе.
# Весь исходный код уже находится в ProjectMap.source_code.

import logging
from collections import defaultdict
from typing import List

from ..types import (
    CandidateType,
    ClassInfo,
    Finding,
    HeuristicResult,
    LlmCandidate,
    ProjectMap,
)
from ._shared import (
    _DEFAULT_EXCLUDE_PATTERNS,
    _parse_class_ast,
    _should_exclude_path,
)
from . import lsp_h_001, lsp_h_002, lsp_h_003, lsp_h_004
from . import ocp_h_001, ocp_h_002, ocp_h_004

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Приоритеты при конфликте findings для одного метода
# ---------------------------------------------------------------------------

# Правило с более высоким числом вытесняет менее специфичное при дедупликации.
# OCP-H-001 специфичнее OCP-H-004; LSP-H-001 специфичнее LSP-H-002.
_FINDING_PRIORITY: dict[str, int] = {
    "OCP-H-001": 2,
    "OCP-H-004": 1,
    "LSP-H-001": 2,
    "LSP-H-002": 1,
}

# ---------------------------------------------------------------------------
# Дедупликация findings: один метод — не более одного finding по принципу
# ---------------------------------------------------------------------------

def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    # Группируем по (file, class_name, method_name).
    # Внутри группы оставляем finding с максимальным приоритетом.
    # Правила вытесненных добавляем в explanation победителя для полноты картины
    groups: dict[tuple[str, str, str | None], list[Finding]] = defaultdict(list)
    for f in findings:
        method_name: str | None = None
        if f.details is not None:
            method_name = f.details.method_name
        key = (f.file, f.class_name or "", method_name)
        groups[key].append(f)

    result: list[Finding] = []

    for _key, group in groups.items():
        if len(group) == 1:
            result.append(group[0])
            continue

        def _priority(f: Finding) -> int:
            return _FINDING_PRIORITY.get(f.rule, 0)

        group_sorted = sorted(group, key=_priority, reverse=True)
        winner = group_sorted[0]
        losers = group_sorted[1:]

        # Добавляем правила вытесненных в explanation победителя
        extra_rules = [f.rule for f in losers if f.rule != winner.rule]
        if extra_rules and winner.details is not None:
            suffix = " Also detected: " + ", ".join(sorted(set(extra_rules))) + "."
            base_expl = winner.details.explanation or ""
            winner.details.explanation = base_expl + suffix

        result.append(winner)

    return result

# ---------------------------------------------------------------------------
# Дедупликация кандидатов: один (file, class) — один LlmCandidate
# ---------------------------------------------------------------------------

def _deduplicate_candidates(candidates: list[LlmCandidate]) -> list[LlmCandidate]:
    # Объединяем кандидатов с одинаковым (file_path, class_name):
    # reasons объединяются, priority берётся максимальный,
    # candidate_type агрегируется ("ocp"+"lsp" → "both")
    by_class: dict[tuple[str, str], LlmCandidate] = {}

    for c in candidates:
        key = (c.file_path, c.class_name)
        existing = by_class.get(key)
        if existing is None:
            by_class[key] = c
            continue

        existing.heuristic_reasons = sorted(
            set(existing.heuristic_reasons + c.heuristic_reasons)
        )
        existing.priority = max(existing.priority, c.priority)

        if existing.candidate_type != c.candidate_type:
            # Любая комбинация ocp/lsp/both → both
            existing.candidate_type = "both"  # type: ignore[assignment]

    return list(by_class.values())

# ---------------------------------------------------------------------------
# Вычисление приоритета и типа кандидата
# ---------------------------------------------------------------------------

def _compute_priority(
    reasons: List[str],
    inheritance_depth: int,
    interface_count: int,
) -> int:
    # Приоритет: больше эвристических попаданий и глубже иерархия → выше
    return (len(reasons) * 2) + inheritance_depth + interface_count


def _determine_candidate_type(
    has_ocp_reasons: bool,
    has_lsp_reasons: bool,
    has_hierarchy: bool,
) -> CandidateType:
    # "both" если есть оба типа причин или класс в иерархии без конкретных хитов
    if has_ocp_reasons and has_lsp_reasons:
        return "both"
    if has_lsp_reasons:
        return "lsp"
    if has_ocp_reasons:
        return "ocp"
    # Класс в иерархии, но без конкретных хитов: LLM смотрит обе стороны
    return "both" if has_hierarchy else "ocp"

# ---------------------------------------------------------------------------
# Главная публичная функция
# ---------------------------------------------------------------------------

def identify_candidates(
    project_map: ProjectMap,
    exclude_patterns: list[str] | None = None,
) -> HeuristicResult:
    # Прогоняет все 7 эвристик по всем классам ProjectMap.
    #
    # Параметры:
    #   project_map      — полная карта проекта (классы, иерархия, интерфейсы)
    #   exclude_patterns — подстроки путей для исключения; None = дефолтный набор
    #
    # Возвращает HeuristicResult:
    #   findings   — дедуплицированные Finding с source="heuristic"
    #   candidates — список LlmCandidate, отсортированный по приоритету (убывание)
    all_findings: List[Finding] = []
    candidates: List[LlmCandidate] = []

    for class_name, class_info in project_map.classes.items():
        # Фильтруем нерелевантные пути (тесты, миграции, venv и т.д.)
        if _should_exclude_path(class_info.file_path, exclude_patterns):
            continue

        # Классы с динамическими базами пропускаем — эвристики на них ненадёжны
        if "" in class_info.parent_classes:
            continue

        # Парсим AST из source_code, сохранённого на Шаге 0 (buildProjectMap)
        class_node = _parse_class_ast(class_info.source_code, class_name)
        if class_node is None:
            continue

        # --- Прогон всех 7 эвристик для одного класса ---
        class_findings: List[Finding] = []

        # LSP-эвристики
        class_findings.extend(lsp_h_001.check(class_node, class_info, project_map))
        class_findings.extend(lsp_h_002.check(class_node, class_info, project_map))
        class_findings.extend(lsp_h_004.check(class_node, class_info))

        # OCP-эвристики
        class_findings.extend(ocp_h_001.check(class_node, class_info))
        class_findings.extend(ocp_h_002.check(class_node, class_info))
        # OCP-H-004 идёт последней: она "шире" — находит isinstance в сложных
        # методах, которые OCP-H-001/002 могли пропустить
        class_findings.extend(ocp_h_004.check(class_node, class_info))

        # LSP-H-003 требует ProjectMap для проверки аннотаций типов
        class_findings.extend(lsp_h_003.check(class_node, class_info, project_map))

        all_findings.extend(class_findings)

        # --- Определение: является ли класс кандидатом для LLM ---
        has_hierarchy = (
            len(class_info.parent_classes) > 0
            or len(class_info.implemented_interfaces) > 0
        )
        # Кандидат: есть хоть одно эвристическое попадание ИЛИ класс в иерархии
        is_candidate = bool(class_findings) or has_hierarchy
        if not is_candidate:
            continue

        reasons = [f.rule for f in class_findings]
        has_ocp = any("OCP" in r for r in reasons)
        has_lsp = any("LSP" in r for r in reasons)
        candidate_type = _determine_candidate_type(has_ocp, has_lsp, has_hierarchy)

        depth = len([p for p in class_info.parent_classes if p != ""])
        interface_count = len(class_info.implemented_interfaces)
        priority = _compute_priority(reasons, depth, interface_count)

        candidates.append(
            LlmCandidate(
                class_name=class_name,
                file_path=class_info.file_path,
                source_code=class_info.source_code,
                candidate_type=candidate_type,
                heuristic_reasons=reasons,
                priority=priority,
            )
        )

    # Дедупликация кандидатов по (file_path, class_name)
    candidates = _deduplicate_candidates(candidates)

    # Сортируем: наибольший приоритет — первым
    candidates.sort(key=lambda c: c.priority, reverse=True)

    # Дедупликация findings по (file, class, method): более специфичное правило побеждает
    deduped_findings = _deduplicate_findings(all_findings)

    return HeuristicResult(
        findings=deduped_findings,
        candidates=candidates,
    )
