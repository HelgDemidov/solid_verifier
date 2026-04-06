# helpers.py — вспомогательные типы, реэкспортируемые для пакета тестов test_cohesion_adapter.
# Единственная точка импорта для тестовых файлов — убирает необходимость повторять
# type: ignore в каждом файле при импорте из production-модулей напрямую.
from solid_dashboard.adapters.cohesion_adapter import ClassInfo
from solid_dashboard.adapters.class_classifier import classify_class

__all__ = ["ClassInfo", "classify_class"]
