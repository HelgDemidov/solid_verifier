# Пакет heuristics — модуляризованные эвристики LSP/OCP.
# Публичный API: identify_candidates — единственная функция, которую
# вызывает пайплайн (_runner.py Step 1b).
from ._runner import identify_candidates  # noqa: F401
