# Публичный API llm-пакета — экспортируем все контракты одним импортом
from .types import (
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
    SourceType,
    CandidateType,
    SeverityLevel,
)

__all__ = [
    "MethodSignature",
    "ClassInfo",
    "InterfaceInfo",
    "ProjectMap",
    "LlmCandidate",
    "HeuristicResult",
    "LlmConfig",
    "LlmAnalysisInput",
    "LlmAnalysisOutput",
    "LlmMetadata",
    "Finding",
    "FindingDetails",
    "SourceType",
    "CandidateType",
    "SeverityLevel",
]

from .ast_parser import build_project_map

__all__ = [
    # ... существующие ...
    "build_project_map",
]