from typing import Any, List, Dict, Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Radon: метрики цикломатической сложности (CC) — пофункционально
# ---------------------------------------------------------------------------

class RadonFunctionMetrics(BaseModel):
    name: str
    type: str
    complexity: int
    rank: str
    lineno: int
    filepath: str
    # опциональное поле из Lizard — отсутствует если lizard недоступен
    # или совпадение по lineno не найдено; включает self для методов
    parameter_count: Optional[int] = None


# ---------------------------------------------------------------------------
# Radon: Maintainability Index (MI) — поуфайлово
# ---------------------------------------------------------------------------

class MaintainabilityFileMetrics(BaseModel):
    filepath: str
    mi: float        # 0.0–100.0: A >= 20, B in [10,20), C < 10
    rank: str        # A / B / C


class MaintainabilityResult(BaseModel):
    total_files: int
    mean_mi: float
    low_mi_count: int        # количество файлов с rank C (MI < 10)
    files: List[MaintainabilityFileMetrics]  # отсортировано по mi ASC


# ---------------------------------------------------------------------------
# Radon: итоговый результат адаптера — зарезервировано для Report Aggregator
# ---------------------------------------------------------------------------

class RadonResult(BaseModel):
    total_items: int
    mean_cc: float
    high_complexity_count: int
    items: List[RadonFunctionMetrics]
    # maintainability — пустой dict {} если radon mi завершился с ошибкой
    maintainability: Optional[MaintainabilityResult] = None
    # lizard_used фиксирует факт обогащения parameter_count в текущем прогоне
    lizard_used: bool = False


# ---------------------------------------------------------------------------
# Cohesion
# ---------------------------------------------------------------------------

class CohesionClassMetrics(BaseModel):
    name: str
    methods_count: int
    cohesion_score: float


class CohesionResult(BaseModel):  # Зарезервировано для Report Aggregator
    total_classes_analyzed: int
    mean_cohesion: float
    low_cohesion_count: int
    classes: List[CohesionClassMetrics]
