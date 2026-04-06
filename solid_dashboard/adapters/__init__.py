from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter as CohesionAdapter
from solid_dashboard.adapters.cohesion_adapter import ClassInfo as ClassInfo
from solid_dashboard.adapters.cohesion_adapter import MethodInfo as MethodInfo
from solid_dashboard.adapters.class_classifier import classify_class as classify_class

# явно объявляем публичный API модуля — Pylance и другие линтеры увидят оба символа
__all__ = ["CohesionAdapter", "ClassInfo", "MethodInfo", "classify_class"]