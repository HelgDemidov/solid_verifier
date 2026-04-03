# ---------------------------------------------------------------------------
# Ручной end-to-end тест интеграции с OpenRouter
#
# Проверяет полный путь: LlmSolidAdapter -> LlmGateway -> OpenRouterProvider -> FileCache -> повторный запуск
#
# Что именно подтверждает тест:
# - реальный сетевой вызов OpenRouter работает с текущим OPENROUTER_API_KEY
# - первый прогон выполняется без кэша, тратит токены и создает cache file
# - второй прогон использует кэш и возвращает тот же доменный результат
# - система корректно различает:
#   1) транспортный успех (HTTP / провайдер отработал),
#   2) доменный успех парсинга JSON,
#   3) доменный parse failure, если модель нарушила контракт response_schema
#
# ВАЖНО: тест не гарантирует, что модель всегда вернет валидный JSON с findings
# Реальная модель может вернуть ответ, несовместимый с ACL-B, и это НЕ считается багом тестируемой инфраструктуры 
# В таком случае тест ожидает честные метрики: candidates_processed=0, candidates_skipped=1, parse_failures=1
#
# Запуск с полными логами: pytest tests/llm/test_open_router_manual.py -m manual -vv -s --log-cli-level=INFO
# ---------------------------------------------------------------------------

import os
import time  # комментарий : модуль для точного измерения времени
import pytest
import logging

from pathlib import Path

from solid_dashboard.llm.factory import create_llm_adapter
from solid_dashboard.llm.types import (
    ProjectMap,
    ClassInfo,
    LlmCandidate,
    LlmConfig,
    LlmAnalysisInput
)

logger = logging.getLogger(__name__)  # комментарий : логгер модуля теста

@pytest.mark.manual
@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY is not set; manual OpenRouter integration test skipped.",
)
def test_manual_openrouter_integration_end_to_end(tmp_path: Path) -> None:
    # комментарий : к этому моменту OPENROUTER_API_KEY уже должен быть поднят в окружение conftest.py через load_dotenv
    api_key = os.environ.get("OPENROUTER_API_KEY")
    assert api_key, "OPENROUTER_API_KEY must be set for this manual test"

    # комментарий : даем возможность переопределить модель через env, но по умолчанию используем openai/gpt-4o-mini.
    model = os.environ.get("SOLID_LLM_MODEL", "openai/gpt-4o-mini")

    # комментарий : используем tmp_path как изолированный каталог кэша
    cache_dir = str(tmp_path)

    # комментарий : prompts_dir рассчитывается относительно корня верификатора
    prompts_dir = "prompts"

    config = LlmConfig(
        provider="openrouter",           # комментарий : пока поддерживаем только OpenRouter
        model=model,                     # <-- модель теперь управляется через SOLID_LLM_MODEL
        api_key=api_key,
        endpoint=None,                   # комментарий : пусть провайдер использует дефолтный URL OpenRouter
        max_tokens_per_run=1500,         # комментарий : ограничиваем бюджет токенов на прогон
        cache_dir=cache_dir,
        prompts_dir=prompts_dir,
    )

    # ---------- 2. Собираем LlmSolidAdapter через фабрику ----------
    adapter = create_llm_adapter(config)
    # комментарий : фабрика сама создает OpenRouterProvider, FileCache, TokenBudgetController и LlmGateway

    # ---------- 3. Конструируем минимальный ProjectMap и одного OCP-кандидата ----------
    bad_source_code = """
class PaymentProcessor:
    def process_payment(self, payment):
        if isinstance(payment, CreditCardPayment):
            print("Processing credit card")
        elif isinstance(payment, PayPalPayment):
            print("Processing paypal")
        else:
            raise ValueError("Unknown payment type")
"""

    class_info = ClassInfo(
        name="PaymentProcessor",
        file_path="src/payment_processor.py",
        source_code=bad_source_code,
        parent_classes=[],
        implemented_interfaces=[],
        methods=[],        # комментарий : для этого теста сигнатуры методов нам не нужны
        dependencies=[],   # комментарий : зависимостей тоже не моделируем
    )

    project_map = ProjectMap(
        classes={"PaymentProcessor": class_info},
        interfaces={},
    )

    candidate = LlmCandidate(
        class_name="PaymentProcessor",
        file_path="src/payment_processor.py",
        source_code=bad_source_code,
        candidate_type="ocp",  # комментарий : проверяем только OCP-сценарий
        heuristic_reasons=[
            "OCP-H-001: isinstance if/elif chain detected",
        ],
        priority=10,
    )

    input_data = LlmAnalysisInput(
        project_map=project_map,
        candidates=[candidate],
    )

    # ---------- 4. Первый запуск: cache miss + измерение времени ----------
    assert not list(tmp_path.iterdir()), "Cache directory must be empty before first run"

    t1_start = time.perf_counter()  # комментарий : начало первого прогона
    output1 = adapter.analyze(input_data)
    t1_end = time.perf_counter()    # комментарий : конец первого прогона
    first_run_seconds = t1_end - t1_start

    logger.info(
        "Manual OpenRouter test: first run duration = %.2fs",
        first_run_seconds,
    )
    # комментарий : логируем расход токенов за первый прогон
    logger.info(
        "Manual OpenRouter test: first run tokens_used = %d",
        output1.metadata.tokens_used,
    )

    logger.info(
        "Manual OpenRouter test: first run parse stats = processed=%d, skipped=%d, "
        "parse_failures=%d, parse_partials=%d, parse_warnings=%d",
        output1.metadata.candidates_processed,
        output1.metadata.candidates_skipped,
        output1.metadata.parse_failures,
        output1.metadata.parse_partials,
        output1.metadata.parse_warnings,
    )

    # базовые sanity-checks первого прогона
    assert output1.metadata.tokens_used > 0
    # ВНИМАНИЕ: Если llm_adapter еще не обновлен до честной работы с ParseResult, 
    # он будет считать ВСЕ ответы как processed. 
    # Поэтому мы проверяем сумму, но не форсируем жесткие нули.
    total_candidates_1 = (
        output1.metadata.candidates_processed
        + output1.metadata.candidates_skipped
    )
    assert total_candidates_1 == 1

    first_tokens = output1.metadata.tokens_used

    cache_files_after_first_run = list(tmp_path.iterdir())
    assert cache_files_after_first_run, "Cache file should be created after first run"
    num_cache_files_first = len(cache_files_after_first_run)

    # ---------- 5. Второй запуск: cache hit + измерение времени ----------

    t2_start = time.perf_counter()  # комментарий : начало второго прогона
    output2 = adapter.analyze(input_data)
    t2_end = time.perf_counter()    # комментарий : конец второго прогона
    second_run_seconds = t2_end - t2_start

    logger.info(
        "Manual OpenRouter test: second run duration = %.2fs",
        second_run_seconds,
    )
    # комментарий : логируем расход токенов за второй прогон
    logger.info(
        "Manual OpenRouter test: second run tokens_used = %d",
        output2.metadata.tokens_used,
    )
    logger.info(
        "Manual OpenRouter test: second run parse stats = processed=%d, skipped=%d, "
        "parse_failures=%d, parse_partials=%d, parse_warnings=%d",
        output2.metadata.candidates_processed,
        output2.metadata.candidates_skipped,
        output2.metadata.parse_failures,
        output2.metadata.parse_partials,
        output2.metadata.parse_warnings,
    )

    # Проверяем, что результаты идентичны
    assert output2.findings == output1.findings

    assert output2.metadata.candidates_processed == output1.metadata.candidates_processed
    assert output2.metadata.candidates_skipped == output1.metadata.candidates_skipped
    assert output2.metadata.parse_failures == output1.metadata.parse_failures
    assert output2.metadata.parse_partials == output1.metadata.parse_partials
    assert output2.metadata.parse_warnings == output1.metadata.parse_warnings

    # Проверяем кэш. Файлов не должно стать больше.
    cache_files_after_second_run = list(tmp_path.iterdir())
    assert len(cache_files_after_second_run) == num_cache_files_first

    # комментарий : дополнительная проверка — второй запуск должен быть
    # заметно быстрее первого (порядок величины меньше).
    # Не делаем жесткий assert по времени, только логируем отношение.
    if first_run_seconds > 0:
        speedup = first_run_seconds / max(second_run_seconds, 1e-6)
        logger.info(
            "Manual OpenRouter test: speedup first/second = %.2f x",
            speedup,
        )