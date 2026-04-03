from __future__ import annotations

from typing import Protocol, Optional

from .types import LlmResponse  # импортируем контрактный тип ответа LLM


class LlmCache(Protocol):
    """
    Протокол кэша для LlmGateway.

    Любая реализация кэша (файловая, in-memory, redis и т.п.)
    должна реализовать этот интерфейс, чтобы ее можно было
    прозрачно подставить в LlmGateway.
    """

    def get(self, key: str) -> Optional[LlmResponse]:
        """
        Вернуть закэшированный LlmResponse по ключу key
        или None, если записи нет или она недоступна.

        Контракт: метод не должен выбрасывать исключений наружу
        из-за внутренних ошибок хранения — кэш должен вести себя
        как «мягкая» оптимизация.
        """
        ...

    def set(self, key: str, value: LlmResponse) -> None:
        """
        Сохранить ответ value в кэш под ключом key.

        Контракт: при ошибках записи реализация может логировать
        предупреждения, но не должна ронять основной пайплайн.
        """
        ...