from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from ..types import LlmResponse  # импортируем актуальный контрактный тип ответа LLM

logger = logging.getLogger(__name__)


class FileCache:
    """
    Файловый кэш для LLM-ответов.
    Реализует протокол LlmCache для LlmGateway.
    Сохраняет ответы в виде JSON-файлов: <cache_dir>/<key>.json
    """

    def __init__(self, cache_dir: str | Path) -> None:
        # Приводим путь к директории кэша к объекту Path
        self.cache_dir = Path(cache_dir)
        # Гарантируем, что директория кэша существует (создаем при необходимости)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> Optional[LlmResponse]:
        """
        Пытается прочитать закэшированный ответ по ключу key.
        В случае любой проблемы с файлом/данными возвращает None,
        чтобы кэш не ронял основной пайплайн.
        """
        # Формируем путь к файлу кэша
        cache_file = self.cache_dir / f"{key}.json"
        # Если файла нет — кэш-промах
        if not cache_file.is_file():
            return None

        try:
            # Читаем JSON из файла
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Восстанавливаем объект LlmResponse из сериализованных полей
            # Обратите внимание: поле raw принципиально не используется
            # (оно удалено из контракта LlmResponse в рамках ACL-A)
            return LlmResponse(
                content=data["content"],
                tokens_used=data["tokens_used"],
                model=data["model"],
            )
        except (json.JSONDecodeError, KeyError) as e:
            # Любые проблемы с кэшем логируем как warning и считаем кэш-промахом
            logger.warning("Failed to read cache file %s: %s", cache_file, e)
            return None

    def set(self, key: str, value: LlmResponse) -> None:
        """
        Сохраняет ответ value в кэш под ключом key.
        Ошибки файловой системы не должны ронять пайплайн:
        в случае OSError просто логируем предупреждение.
        """
        # Путь к целевому файлу кэша
        cache_file = self.cache_dir / f"{key}.json"

        # Формируем минимальный JSON-словарь для сериализации ответа
        # Сырые данные (raw) принципиально не сохраняем — это часть ACL-дизайна
        data = {
            "content": value.content,
            "tokens_used": value.tokens_used,
            "model": value.model,
        }

        try:
            # Пишем сначала во временный файл, затем атомарно переименовываем
            # Это защищает от частично записанных файлов при сбоях
            temp_file = cache_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            temp_file.replace(cache_file)
        except OSError as e:
            # Ошибки записи кэша считаем мягкими: логируем и продолжаем без кэша
            logger.warning("Failed to write cache to %s: %s", cache_file, e)