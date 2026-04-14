import argparse
import json
import logging

from pathlib import Path
from typing import Any

from dotenv import load_dotenv  # подгружаем .env на старте CLI
from dataclasses import asdict, is_dataclass
from .pipeline import run_pipeline
from .config import load_config

from .adapters.radon_adapter import RadonAdapter
from .adapters.cohesion_adapter import CohesionAdapter
from .adapters.import_graph_adapter import ImportGraphAdapter
from .adapters.import_linter_adapter import ImportLinterAdapter
from .adapters.pyan3_adapter import Pyan3Adapter
from .adapters.heuristics_adapter import HeuristicsAdapter


def _to_jsonable(value: Any) -> Any:
    # -------------------------------------------------------------------
    # Базовые JSON-совместимые типы
    # -------------------------------------------------------------------
    # примитивы и None уже совместимы с json.dumps
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    # -------------------------------------------------------------------
    # Path -> str
    # -------------------------------------------------------------------
    # pathlib.Path явно переводим в строку
    if isinstance(value, Path):
        return str(value)

    # -------------------------------------------------------------------
    # Dataclass-инстанс -> dict -> рекурсивная нормализация
    # -------------------------------------------------------------------
    # asdict работает только с экземплярами dataclass
    if is_dataclass(value) and not isinstance(value, type):
        return _to_jsonable(asdict(value))

    # -------------------------------------------------------------------
    # dict -> рекурсивная нормализация значений
    # -------------------------------------------------------------------
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}

    # -------------------------------------------------------------------
    # list / tuple / set -> JSON-массив
    # -------------------------------------------------------------------
    # set и tuple приводим к list
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(item) for item in value]

    # -------------------------------------------------------------------
    # Объекты с __dict__ -> рекурсивная нормализация полей
    # -------------------------------------------------------------------
    # защитная ветка для не-dataclass объектов
    if hasattr(value, "__dict__"):
        return _to_jsonable(vars(value))

    # -------------------------------------------------------------------
    # Последний fallback
    # -------------------------------------------------------------------
    # безопасное строковое представление неизвестного типа
    return str(value)


def main() -> None:

    # -------------------------------------------------------------------
    # Загрузка переменных окружения из .env: поднимаем значения из .env в os.environ до чтения конфига
    # -------------------------------------------------------------------

    load_dotenv()

    # -------------------------------------------------------------------
    # Базовая настройка логирования пайплайна
    # -------------------------------------------------------------------
    # пишем все WARNING/INFO/ERROR в отдельный лог-файл,чтобы не терять сообщения LLM-адаптера и AST-парсера
    
    base_dir = Path(__file__).resolve().parent
    report_dir = base_dir / "report"
    report_dir.mkdir(exist_ok=True)
    log_path = report_dir / "solid_pipeline.log"

    logging.basicConfig(
        level=logging.INFO,  # WARNING и выше точно попадут
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(),  # чтобы видеть то же самое в терминале
        ],
    )
    # перехватываем все warnings.warn(..., RuntimeWarning) из адаптеров
    # (radon_adapter, pyan3_adapter и будущих) и направляем их в logging-пайплайн
    logging.captureWarnings(True)

    parser = argparse.ArgumentParser(description="SOLID Verifier Dashboard")
    parser.add_argument(
        "--target-dir",
        required=True,
        help="Path to analyzed project (Python package root)",
    )
    parser.add_argument(
        "--config",
        required=False,
        help="Path to solid_config.json (search performed in current catalog by default)",
    )

    args = parser.parse_args()

    # -------------------------------------------------------------------
    # Загружаем конфиг верификатора
    # -------------------------------------------------------------------
    config = load_config(args.config)

    # -------------------------------------------------------------------
    # Инициализируем адаптеры статического анализа
    # -------------------------------------------------------------------
    # HeuristicsAdapter обязателен для LLM-слоя
    adapters = [
        RadonAdapter(),
        CohesionAdapter(),
        ImportGraphAdapter(),
        ImportLinterAdapter(),
        Pyan3Adapter(),
        HeuristicsAdapter(),
    ]

    # -------------------------------------------------------------------
    # Временный диагностический вывод состава пайплайна
    # -------------------------------------------------------------------
    # помогает быстро проверить наличие heuristics-адаптера
    print("\n=== Enabled Adapters ===")
    for adapter in adapters:
        print(f"- {adapter.name}")

    # -------------------------------------------------------------------
    # Запускаем пайплайн
    # -------------------------------------------------------------------
    results = run_pipeline(args.target_dir, config, adapters)

    # -------------------------------------------------------------------
    # Нормализуем результат к JSON-совместимому виду
    # -------------------------------------------------------------------
    json_ready_results = _to_jsonable(results)

    # -------------------------------------------------------------------
    # Форматируем результат в JSON-строку
    # -------------------------------------------------------------------
    report_text = json.dumps(
        json_ready_results,
        indent=2,
        ensure_ascii=False,
    )

    # -------------------------------------------------------------------
    # Печатаем результат в консоль
    # -------------------------------------------------------------------
    print("\n=== Pipeline Result ===")
    print(report_text)

    # -------------------------------------------------------------------
    # Сохраняем результат в файл report/solid_report.log
    # -------------------------------------------------------------------
    base_dir = Path(__file__).resolve().parent
    report_dir = base_dir / "report"
    report_dir.mkdir(exist_ok=True)

    report_path = report_dir / "solid_report.log"
    with report_path.open("w", encoding="utf-8") as f:
        f.write(report_text)

    print(f"\nReport successfully saved to: {report_path}")


if __name__ == "__main__":
    main()
