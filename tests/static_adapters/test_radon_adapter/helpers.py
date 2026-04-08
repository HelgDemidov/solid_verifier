# helpers.py — assert-хелперы и вспомогательные утилиты для пакета тестов test_radon_adapter.
# Фиксирует schema-контракт run() как единственный источник правды для всех тестовых файлов.

__all__ = ["assert_success_schema", "assert_error_schema", "assert_item_fields"]

# Обязательные ключи успешного ответа run()
_SUCCESS_KEYS = frozenset({
    "total_items",
    "mean_cc",
    "high_complexity_count",
    "items",
    "lizard_used",
})


def assert_success_schema(result: dict) -> None:
    """Проверяет, что result содержит все обязательные ключи успешного ответа.

    Верифицирует:
    - наличие всех обязательных ключей (_SUCCESS_KEYS)
    - отсутствие ключа "error" (успешный путь не должен его содержать)
    - lizard_used — bool
    - items — список
    """
    missing = _SUCCESS_KEYS - result.keys()
    assert not missing, f"Отсутствуют ключи в успешном ответе: {missing}"
    # ключ "error" не должен присутствовать в успешном ответе
    assert "error" not in result, (
        f'"error" не должен присутствовать в успешном ответе, но получено: {result["error"]!r}'
    )
    assert isinstance(result["lizard_used"], bool), "lizard_used должен быть bool"
    assert isinstance(result["items"], list), "items должен быть списком"


def assert_error_schema(result: dict) -> None:
    """Проверяет schema-контракт ответа с ошибкой.

    RadonAdapter в error-случае возвращает минималистичный {"error": "..."},
    БЕЗ числовых полей success-схемы — в отличие от Pyan3Adapter, у которого
    есть _error() метод с полным набором обнуленных полей.

    Верифицирует:
    - присутствие ключа "error"
    - error — непустая строка
    - отсутствие ключей success-схемы (total_items, mean_cc, items и др.)
    """
    assert "error" in result, f'Ключ "error" отсутствует в ответе: {result}'
    assert isinstance(result["error"], str) and result["error"], (
        "error должен быть непустой строкой"
    )
    # success-ключи не должны присутствовать в error-ответе
    unexpected = _SUCCESS_KEYS & result.keys()
    assert not unexpected, (
        f"В error-ответе не должно быть success-ключей, но найдены: {unexpected}"
    )


def assert_item_fields(item: dict) -> None:
    """Проверяет, что item содержит все обязательные поля.

    Обязательные поля каждого элемента items[]: name, type, complexity,
    rank, lineno, filepath. Поле parameter_count опционально (только при
    наличии lizard и совпадении в индексе).
    """
    required = {"name", "type", "complexity", "rank", "lineno", "filepath"}
    missing = required - item.keys()
    assert not missing, f"Отсутствуют поля в item: {missing}. Item: {item}"
