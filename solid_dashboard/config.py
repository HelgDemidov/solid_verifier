import os
import json
from pathlib import Path
from typing import Any, Dict

# типы LLM-конфига импортируем здесь, чтобы не тянуть лишнюю логику в остальные модули
# config.py остается тонким адаптером между JSON и кодом.
from .llm.types import LlmConfig  


def load_config(path: str | None) -> Dict[str, Any]:
    """
    Загружает конфиг верификатора из JSON-файла.
    Если путь не передан, ищет solid_config.json в той же директории, где лежит сам скрипт.
    """
    # если путь явно указан через --config, используем его
    if path:
        config_path = Path(path).resolve()
    else:
        # привязываем поиск дефолтного конфига к расположению исходного кода дашборда
        base_dir = Path(__file__).resolve().parent
        config_path = base_dir / "solid_config.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        data: Dict[str, Any] = json.load(f)

    # Минимальная валидация ключей
    if "package_root" not in data:
        raise ValueError("Config must contain 'package_root'")
    if "layers" not in data or not isinstance(data["layers"], dict):
        raise ValueError("Config must contain 'layers' dict")
    if "ignore_dirs" not in data or not isinstance(data["ignore_dirs"], list):
        raise ValueError("Config must contain 'ignore_dirs' list")

    # Проверяем согласованность layer_order и ключей layers:
    # расхождение между ними — тихая бомба замедленного действия (неверный порядок
    # в layers-контракте import-linter), поэтому превращаем его в явный сбой на старте
    layer_order = data.get("layer_order")
    layer_keys = set(data["layers"].keys())
    if layer_order is not None:
        if not isinstance(layer_order, list):
            raise ValueError("Config key 'layer_order' must be a list if present")
        if set(layer_order) != layer_keys:
            missing_in_order = layer_keys - set(layer_order)
            extra_in_order = set(layer_order) - layer_keys
            raise ValueError(
                f"'layer_order' и ключи 'layers' не совпадают. "
                f"Отсутствуют в layer_order: {missing_in_order}. "
                f"Лишние в layer_order: {extra_in_order}."
            )

    data["__config_path__"] = str(config_path)

    return data


def _resolve_path_from_config(raw_path: str, config_dir: Path) -> str:
    """
    Преобразует путь из конфига в абсолютный путь.

    Правила:
    - абсолютный путь оставляем как есть
    - относительный путь трактуем относительно директории solid_config.json
    """
    # поддерживаем только непустые строковые пути
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError("Path value in config must be a non-empty string")

    path_obj = Path(raw_path)

    # если путь уже абсолютный, просто нормализуем его
    if path_obj.is_absolute():
        return str(path_obj.resolve())

    # относительные пути всегда резолвим от директории конфига,
    # а не от текущей рабочей директории процесса
    return str((config_dir / path_obj).resolve())


def load_llm_config(raw_config: Dict[str, Any]) -> LlmConfig:
    """
    Строит LlmConfig из общего JSON-конфига solid_config.json.

    Ожидается секция вида:
        "llm": {
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": null,
            "endpoint": null,
            "max_tokens_per_run": 1000,
            "cache_dir": ".solid-cache/llm",
            "prompts_dir": "tools/solid_verifier/prompts"
        }

    Все файловые пути резолвятся относительно директории solid_config.json
    """
    # достаем вложенный словарь "llm", если он есть, иначе используем пустой dict и полагаемся на дефолты ниже
    llm_section = raw_config.get("llm") or {}

    if not isinstance(llm_section, dict):
        raise ValueError("Config key 'llm' must be an object if present")

    # путь к конфигу должен быть сохранен функцией load_config
    config_path_raw = raw_config.get("__config_path__")
    if not isinstance(config_path_raw, str) or not config_path_raw.strip():
        raise ValueError("Internal config error: '__config_path__' is missing")

    config_dir = Path(config_path_raw).resolve().parent

    # аккуратно извлекаем поля с дефолтами
    # модель по умолчанию — openai/gpt-4o-mini, с подключением через OpenRouter
    provider = llm_section.get("provider", "openrouter")
    model = llm_section.get("model", "openai/gpt-4o-mini")
    api_key = llm_section.get("api_key") or os.getenv("OPENROUTER_API_KEY")
    endpoint = llm_section.get("endpoint")
    max_tokens_per_run = int(llm_section.get("max_tokens_per_run", 1000))

    # оба пути резолвим централизованно через директорию конфига
    raw_cache_dir = llm_section.get("cache_dir", ".solid-cache/llm")
    raw_prompts_dir = llm_section.get("prompts_dir", "prompts")

    cache_dir = _resolve_path_from_config(raw_cache_dir, config_dir)
    prompts_dir = _resolve_path_from_config(raw_prompts_dir, config_dir)

    return LlmConfig(
        provider=provider,
        model=model,
        api_key=api_key,
        endpoint=endpoint,
        max_tokens_per_run=max_tokens_per_run,
        cache_dir=cache_dir,
        prompts_dir=prompts_dir,
    )
