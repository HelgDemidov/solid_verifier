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
    # collision_rate — часть публичного контракта адаптера: всегда присутствует в обоих ветвях ответа
    "collision_rate",
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
    # collision_rate в _error() всегда 0.0: вычислений нет, возвращается хардкодом
    "collision_rate",
    "raw_output",
})


def make_raw_output(
    edges: list[tuple[str, str]],
    extra_used: dict[str, list[str]] | None = None,
) -> str:
    """\u0421\u0442\u0440\u043e\u0438\u0442 \u043c\u0438\u043d\u0438\u043c\u0430\u043b\u044c\u043d\u044b\u0439 \u0432\u0430\u043b\u0438\u0434\u043d\u044b\u0439 raw_output \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435 pyan3 text-\u0440\u0435\u0436\u0438\u043c\u0430.

    Args:
        edges: \u0441\u043f\u0438\u0441\u043e\u043a \u043f\u0430\u0440 (\u0438\u0441\u0442\u043e\u0447\u043d\u0438\u043a, \u0446\u0435\u043b\u044c) \u2014 \u043a\u0430\u0436\u0434\u0430\u044f \u043f\u0430\u0440\u0430 \u0433\u0435\u043d\u0435\u0440\u0438\u0440\u0443\u0435\u0442 \u0441\u0442\u0440\u043e\u043a\u0443 \u0431\u043b\u043e\u043a\u0430
               \u0438 \u0441\u0442\u0440\u043e\u043a\u0443 [U]-\u0440\u0435\u0431\u0440\u0430 \u0432\u043d\u0443\u0442\u0440\u0438 \u043d\u0435\u0433\u043e.
        extra_used: \u0441\u043b\u043e\u0432\u0430\u0440\u044c {\u0431\u043b\u043e\u043a: [\u0438\u043c\u044f, ...]} \u2014 \u0434\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 [U]-\u0441\u0442\u0440\u043e\u043a\u0438 \u0432\u043d\u0443\u0442\u0440\u0438
                    \u0431\u043b\u043e\u043a\u0430 (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440, \u0434\u043b\u044f \u0441\u043e\u0437\u0434\u0430\u043d\u0438\u044f \u043a\u043e\u043b\u043b\u0438\u0437\u0438\u0439 \u043f\u0440\u0438 \u0442\u0435\u0441\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0438
                    _detect_suspicious_blocks). \u0418\u043c\u0435\u043d\u0430 \u0434\u043e\u0431\u0430\u0432\u043b\u044f\u044e\u0442\u0441\u044f \u041f\u041e\u0421\u041b\u0415 \u0440\u0435\u0431\u0440\u0430 \u0438\u0437 edges.

    Returns:
        \u0421\u0442\u0440\u043e\u043a\u0430 raw_output, \u0433\u043e\u0442\u043e\u0432\u0430\u044f \u043a \u043f\u043e\u0434\u0430\u0447\u0435 \u0432 \u043f\u0430\u0440\u0441\u0435\u0440 \u0438\u043b\u0438 _detect_suspicious_blocks.

    \u041f\u0440\u0438\u043c\u0435\u0440:
        make_raw_output([("A", "B"), ("A", "C")], extra_used={"A": ["B"]})
        \u2192
        "A\\n  [U] B\\n  [U] B\\nA\\n  [U] C\\n"
        (\u0432\u0442\u043e\u0440\u043e\u0439 [U] B \u0441\u043e\u0437\u0434\u0430\u0451\u0442 \u043a\u043e\u043b\u043b\u0438\u0437\u0438\u044e \u0432 \u0431\u043b\u043e\u043a\u0435 A)
    """
    extra = extra_used or {}
    lines: list[str] = []

    # группируем ребра по источнику чтобы блок src появлялся один раз с несколькими [U]
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
    """\u0423\u0442\u0432\u0435\u0440\u0436\u0434\u0430\u0435\u0442, \u0447\u0442\u043e \u0432 \u0441\u043f\u0438\u0441\u043a\u0435 edges \u0435\u0441\u0442\u044c \u0440\u0435\u0431\u0440\u043e frm\u2192to \u0441 \u0437\u0430\u0434\u0430\u043d\u043d\u044b\u043c confidence.

    \u041f\u0430\u0434\u0430\u0435\u0442 \u0441 \u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0442\u0438\u0432\u043d\u044b\u043c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435\u043c, \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u044e\u0449\u0438\u043c \u0432\u0441\u0435 \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0435 \u0440\u0435\u0431\u0440\u0430,
    \u0435\u0441\u043b\u0438 \u0441\u043e\u0432\u043f\u0430\u0434\u0435\u043d\u0438\u044f \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e.
    """
    for e in edges:
        if e["from"] == frm and e["to"] == to and e["confidence"] == confidence:
            return
    existing = [(e["from"], e["to"], e["confidence"]) for e in edges]
    raise AssertionError(
        f"\u0420\u0435\u0431\u0440\u043e {frm!r}\u2192{to!r} confidence={confidence!r} \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d\u043e.\n"
        f"\u0421\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0435 \u0440\u0435\u0431\u0440\u0430: {existing}"
    )


def assert_success_schema(result: dict) -> None:
    """\u041f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442, \u0447\u0442\u043e result \u0441\u043e\u0434\u0435\u0440\u0436\u0438\u0442 \u0432\u0441\u0435 \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u043a\u043b\u044e\u0447\u0438 \u0443\u0441\u043f\u0435\u0448\u043d\u043e\u0433\u043e \u043e\u0442\u0432\u0435\u0442\u0430."""
    missing = _SUCCESS_KEYS - result.keys()
    assert not missing, f"\u041e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u044e\u0442 \u043a\u043b\u044e\u0447\u0438 \u0432 \u0443\u0441\u043f\u0435\u0448\u043d\u043e\u043c \u043e\u0442\u0432\u0435\u0442\u0435: {missing}"
    assert result["is_success"] is True, "is_success \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c True"


def assert_error_schema(result: dict) -> None:
    """\u041f\u0440\u043e\u0432\u0435\u0440\u044f\u0435\u0442, \u0447\u0442\u043e result \u0441\u043e\u0434\u0435\u0440\u0436\u0438\u0442 \u0432\u0441\u0435 \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u043a\u043b\u044e\u0447\u0438 \u043e\u0442\u0432\u0435\u0442\u0430 \u0441 \u043e\u0448\u0438\u0431\u043a\u043e\u0439,
    \u0432\u043a\u043b\u044e\u0447\u0430\u044f collision_rate == 0.0 (\u0441\u0442\u0440\u0443\u043a\u0442\u0443\u0440\u043d\u044b\u0439 \u0438\u043d\u0432\u0430\u0440\u0438\u0430\u043d\u0442 _error())."""
    missing = _ERROR_KEYS - result.keys()
    assert not missing, f"\u041e\u0442\u0441\u0443\u0442\u0441\u0442\u0432\u0443\u044e\u0442 \u043a\u043b\u044e\u0447\u0438 \u0432 error-\u043e\u0442\u0432\u0435\u0442\u0435: {missing}"
    assert result["is_success"] is False, "is_success \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c False"
    assert isinstance(result["error"], str) and result["error"], "error \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c \u043d\u0435\u043f\u0443\u0441\u0442\u043e\u0439 \u0441\u0442\u0440\u043e\u043a\u043e\u0439"
    # collision_rate в _error() хардкодом 0.0: вычислений нет, инвариант не зависит от входных данных
    assert result["collision_rate"] == 0.0, "collision_rate \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c 0.0 \u0432 error-\u043e\u0442\u0432\u0435\u0442\u0435"
    # все числовые поля обнулены
    for key in ("node_count", "edge_count", "edge_count_high", "edge_count_low",
                "dead_node_count", "root_node_count"):
        assert result[key] == 0, f"{key} \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c 0 \u0432 error-\u043e\u0442\u0432\u0435\u0442\u0435"
    # все списки пусты
    for key in ("nodes", "edges", "dead_nodes", "root_nodes", "suspicious_blocks"):
        assert result[key] == [], f"{key} \u0434\u043e\u043b\u0436\u0435\u043d \u0431\u044b\u0442\u044c [] \u0432 error-\u043e\u0442\u0432\u0435\u0442\u0435"
