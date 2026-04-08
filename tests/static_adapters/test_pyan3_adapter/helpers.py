# helpers.py — вспомогательные утилиты и assert-хелперы для пакета тестов test_pyan3_adapter.
# Единственная точка импорта внутренних символов адаптера для тестовых файлов —
# убирает необходимость повторять длинные пути импорта и type: ignore в каждом файле.
from solid_dashboard.adapters.pyan3_adapter import _detect_suspicious_blocks, _VALID_PY_NAME

__all__ = ["_detect_suspicious_blocks", "_VALID_PY_NAME", "make_raw_output", "assert_edge",
           "assert_success_schema", "assert_error_schema"]

# Ключи, которые обязаны присутствовать в успешном ответе run()
_SUCCESS_KEYS = frozenset({
    "is_success",
    "node_count",
    "edge_count",
    "edge_count_high",
    "edge_count_low",
    "nodes",
    "edges",
    "dead_node_count",
    "dead_nodes",
    "root_node_count",
    "root_nodes",
    "suspicious_blocks",
    "raw_output",
})

# Ключи, которые обязаны присутствовать в ответе с ошибкой run()
_ERROR_KEYS = frozenset({
    "is_success",
    "error",
    "node_count",
    "edge_count",
    "edge_count_high",
    "edge_count_low",
    "nodes",
    "edges",
    "dead_node_count",
    "dead_nodes",
    "root_node_count",
    "root_nodes",
    "suspicious_blocks",
    "raw_output",
})


def make_raw_output(
    edges: list[tuple[str, str]],
    extra_used: dict[str, list[str]] | None = None,
) -> str:
    """Строит минимальный валидный raw_output в формате pyan3 text-режима.

    Args:
        edges: список пар (источник, цель) — каждая пара генерирует строку блока
               и строку [U]-ребра внутри него.
        extra_used: словарь {блок: [имя, ...]} — дополнительные [U]-строки внутри
                    блока (например, для создания коллизий при тестировании
                    _detect_suspicious_blocks). Имена добавляются ПОСЛЕ ребра из edges.

    Returns:
        Строка raw_output, готовая к подаче в парсер или _detect_suspicious_blocks.

    Пример:
        make_raw_output([("A", "B"), ("A", "C")], extra_used={"A": ["B"]})
        →
        "A\n  [U] B\n  [U] B\nA\n  [U] C\n"
        (второй [U] B создаёт коллизию в блоке A)
    """
    extra = extra_used or {}
    lines: list[str] = []

    # группируем рёбра по источнику чтобы блок src появлялся один раз с несколькими [U]
    from collections import defaultdict
    grouped: dict[str, list[str]] = defaultdict(list)
    for src, dst in edges:
        grouped[src].append(dst)

    for src, dsts in grouped.items():
        lines.append(src)
        for dst in dsts:
            lines.append(f"  [U] {dst}")
        # добавляем extra_used для этого блока (если есть)
        for extra_name in extra.get(src, []):
            lines.append(f"  [U] {extra_name}")

    return "\n".join(lines) + "\n" if lines else ""


def assert_edge(
    edges: list[dict],
    frm: str,
    to: str,
    confidence: str,
) -> None:
    """Утверждает, что в списке edges есть ребро frm→to с заданным confidence.

    Падает с информативным сообщением, показывающим все существующие рёбра,
    если совпадения не найдено.
    """
    for e in edges:
        if e["from"] == frm and e["to"] == to and e["confidence"] == confidence:
            return
    existing = [(e["from"], e["to"], e["confidence"]) for e in edges]
    raise AssertionError(
        f"Ребро {frm!r}→{to!r} confidence={confidence!r} не найдено.\n"
        f"Существующие рёбра: {existing}"
    )


def assert_success_schema(result: dict) -> None:
    """Проверяет, что result содержит все обязательные ключи успешного ответа."""
    missing = _SUCCESS_KEYS - result.keys()
    assert not missing, f"Отсутствуют ключи в успешном ответе: {missing}"
    assert result["is_success"] is True, "is_success должен быть True"


def assert_error_schema(result: dict) -> None:
    """Проверяет, что result содержит все обязательные ключи ответа с ошибкой."""
    missing = _ERROR_KEYS - result.keys()
    assert not missing, f"Отсутствуют ключи в error-ответе: {missing}"
    assert result["is_success"] is False, "is_success должен быть False"
    assert isinstance(result["error"], str) and result["error"], "error должен быть непустой строкой"
    # все числовые поля обнулены
    for key in ("node_count", "edge_count", "edge_count_high", "edge_count_low",
                "dead_node_count", "root_node_count"):
        assert result[key] == 0, f"{key} должен быть 0 в error-ответе"
    # все списки пусты
    for key in ("nodes", "edges", "dead_nodes", "root_nodes", "suspicious_blocks"):
        assert result[key] == [], f"{key} должен быть [] в error-ответе"
