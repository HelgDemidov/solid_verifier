from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from .provider import LlmResponse

logger = logging.getLogger(__name__)


class FileCache:
    """
    Файловый кэш для LLM-ответов.
    Реализует протокол LlmCache для LlmGateway.
    Сохраняет ответы в виде JSON файлов: <cache_dir>/<key>.json
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        # Гарантируем, что директория существует
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[LlmResponse]:
        """Пытается прочитать закэшированный ответ."""
        cache_file = self.cache_dir / f"{key}.json"
        if not cache_file.is_file():
            return None

        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            # Восстанавливаем объект LlmResponse
            return LlmResponse(
                content=data["content"],
                tokens_used=data["tokens_used"],
                model=data["model"],
                raw=data.get("raw")
            )
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to read cache file %s: %s", cache_file, e)
            return None

    def set(self, key: str, value: LlmResponse) -> None:
        """Сохраняет ответ в кэш."""
        cache_file = self.cache_dir / f"{key}.json"
        
        data = {
            "content": value.content,
            "tokens_used": value.tokens_used,
            "model": value.model,
            "raw": value.raw,
        }
        
        try:
            # Записываем сначала во временный файл, затем переименовываем
            # для защиты от прерывания процесса во время записи
            temp_file = cache_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(cache_file)
        except OSError as e:
            logger.warning("Failed to write cache to %s: %s", cache_file, e)