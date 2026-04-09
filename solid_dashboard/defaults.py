# Централизованные пороги и дефолты SOLID-верификатора.
# Импортируется из report_aggregator.py и radon_adapter.py.
# При добавлении нового порога — добавлять сюда, не в вызывающий код.

CC_THRESHOLD: int = 10                      # цикломатическая сложность: warn если > CC_THRESHOLD
LCOM4_THRESHOLD: int = 1                    # LCOM4: warn если > LCOM4_THRESHOLD (не монолитный класс)
LOW_MI_RANK: str = "C"                      # rank C означает трудноподдерживаемый файл (MI < 10)
DEAD_CODE_CONFIDENCE_CUTOFF: float = 0.35   # collision_rate >= этого значения -> confidence=low
