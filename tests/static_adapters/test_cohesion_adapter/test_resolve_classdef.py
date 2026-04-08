# ==============================================================================
# Блок C: unit-тесты для CohesionAdapter._resolve_classdef
#
# Проверяем логику разрешения имени базового класса -> ClassDef:
#   1. Имя не найдено в индексе -> None (внешняя зависимость)
#   2. Ровно одно определение -> возвращает его безусловно
#   3. Несколько определений, одно в том же файле -> возвращает совпадающее
#   4. Несколько определений в разных файлах, ни одно не совпадает -> None + logger.warning
# ==============================================================================

import ast
import logging
import pytest

from typing import cast
from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter


def _make_classdef(name: str) -> ast.ClassDef:
    # cast необходим: body[0] типизирован как ast.stmt,
    # но мы гарантируем ClassDef через детерминированный f"class {name}: pass"
    return cast(ast.ClassDef, ast.parse(f"class {name}: pass").body[0])

class TestResolveClassdef:
    # имя отсутствует в индексе — внешняя зависимость, молча возвращаем None
    def test_not_found_returns_none(self, adapter):
        index = {}  # пустой индекс
        result = adapter._resolve_classdef("BaseModel", index, "/some/file.py")
        assert result is None

    # ровно одно определение в индексе — возвращает его безусловно
    def test_single_entry_returned(self, adapter):
        classdef = _make_classdef("Repo")
        index = {"Repo": [("/app/repo.py", classdef)]}
        result = adapter._resolve_classdef("Repo", index, "/app/service.py")
        assert result is classdef

    # два определения, одно в том же файле что и caller — выбирает same-file
    def test_same_file_wins(self, adapter):
        classdef_a = _make_classdef("Base")
        classdef_b = _make_classdef("Base")
        index = {
            "Base": [
                ("/app/models.py", classdef_a),   # тот же файл, что и caller
                ("/app/other.py",  classdef_b),
            ]
        }
        result = adapter._resolve_classdef("Base", index, "/app/models.py")
        assert result is classdef_a

    # два определения в разных файлах, ни одно не совпадает с caller — None + WARNING
    def test_ambiguous_returns_none_and_warns(self, adapter, caplog):
        classdef_a = _make_classdef("Mixin")
        classdef_b = _make_classdef("Mixin")
        index = {
            "Mixin": [
                ("/app/mixins_a.py", classdef_a),
                ("/app/mixins_b.py", classdef_b),
            ]
        }
        # caller не совпадает ни с одним из файлов в индексе
        with caplog.at_level(logging.WARNING, logger="solid_dashboard.adapters.cohesion_adapter"):
            result = adapter._resolve_classdef("Mixin", index, "/app/service.py")

        assert result is None
        # проверяем, что в логе появился WARNING с упоминанием имени класса
        assert any(
            "Mixin" in record.message and record.levelno == logging.WARNING
            for record in caplog.records
        )

    # одно определение в том же файле что и caller, второе в другом —
    # WARNING не должен появляться, возвращает совпадающий ClassDef
    def test_same_file_no_warning(self, adapter, caplog):
        classdef_a = _make_classdef("Helper")
        classdef_b = _make_classdef("Helper")
        index = {
            "Helper": [
                ("/app/utils.py",  classdef_a),
                ("/app/other.py",  classdef_b),
            ]
        }
        with caplog.at_level(logging.WARNING, logger="solid_dashboard.adapters.cohesion_adapter"):
            result = adapter._resolve_classdef("Helper", index, "/app/utils.py")

        assert result is classdef_a
        # при однозначном разрешении WARNING не должен выдаваться
        assert not any(
            "Helper" in record.message and record.levelno == logging.WARNING
            for record in caplog.records
        )
