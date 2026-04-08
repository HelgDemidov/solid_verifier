from typing import Any, List, Dict, Optional
from pydantic import BaseModel


class RadonFunctionMetrics(BaseModel):
    name: str
    type: str
    complexity: int
    rank: str
    lineno: int
    # НОВЫЕ ПОЛЯ ИЗ LIZARD (опциональные, на случай если lizard упадет)
    parameter_count: Optional[int] = None

class RadonResult(BaseModel): # Зарезервировано для Report Aggregator
    total_items: int
    mean_cc: float
    high_complexity_count: int
    items: List[RadonFunctionMetrics]

class CohesionClassMetrics(BaseModel):
    name: str
    methods_count: int
    cohesion_score: float

class CohesionResult(BaseModel): # Зарезервировано для Report Aggregator
    total_classes_analyzed: int
    mean_cohesion: float
    low_cohesion_count: int
    classes: List[CohesionClassMetrics]