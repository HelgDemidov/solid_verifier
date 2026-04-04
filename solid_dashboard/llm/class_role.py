# ===================================================================================================
# Классификатор ролей классов для эвристического анализа SOLID
#
# Определяет ClassRole — категорию класса по его структуре в AST — и функцию classify_class(),
# которую используют эвристики LSP/OCP, чтобы не запускаться на инфраструктурных или конфигурационных классах
#
# Классы ролей:
#   PURE_INTERFACE — только абстрактные методы (ABC без __init__)
#   INFRA_MODEL    — Pydantic BaseModel / SQLAlchemy ORM / аналоги
#   CONFIG         — конфигурационные классы (BaseSettings, Config)
#   DOMAIN         — прикладной класс, пригодный для SOLID-анализа
# ===================================================================================================

import ast
from enum import Enum, auto


# ---------------------------------------------------------------------------
# Роли классов
# ---------------------------------------------------------------------------

class ClassRole(Enum):
    # Класс является чистым интерфейсом / контрактом (только abstractmethod)
    PURE_INTERFACE = auto()
    # Инфраструктурная модель: Pydantic, SQLAlchemy, аналоги
    INFRA_MODEL = auto()
    # Конфигурационный класс: BaseSettings, Config, Settings
    CONFIG = auto()
    # Прикладной доменный класс — подходит для SOLID-анализа
    DOMAIN = auto()


# ---------------------------------------------------------------------------
# Известные инфраструктурные базовые классы
# ---------------------------------------------------------------------------

# Базы Pydantic / pydantic-settings / SQLAlchemy.
# Класс, наследующий от любого из них, получает роль INFRA_MODEL или CONFIG.
#
# Политика включения имён в этот список:
#   - Имя должно быть достаточно специфичным, чтобы не давать false positive
#     на доменные классы с совпадающим именем базы
#   - Слишком generic-имена ("Base", "Model", "Schema") намеренно исключены:
#     такие классы корректно детектируются через InfraScore (сигналы __tablename__,
#     Column(), Field(), AnnAssign ratio), а не по имени базы
_KNOWN_INFRA_BASES: frozenset[str] = frozenset({
    # Pydantic v1 / v2
    "BaseModel",
    "GenericModel",
    # pydantic-settings — также присутствует в _KNOWN_CONFIG_BASES;
    # дублирование намеренное: CONFIG-проверка идет раньше в classify_class(),
    # но InfraScore должен знать об этой базе для нестандартных иерархий
    "BaseSettings",
    # SQLAlchemy declarative API (современный стиль)
    "DeclarativeBase",
    "DeclarativeBaseNoMeta",
    "MappedAsDataclass",
    # "Base" намеренно не включен: слишком generic
    # "Model" намеренно не включен: слишком generic, риск false positive
    #   на доменные классы вида class PaymentModel(Model)
    #   Django/SQLAlchemy Model детектируется через InfraScore:
    #   __tablename__ (+1) + Column()/mapped_column() (+1) >= порога
    # "Schema" намеренно не включен: слишком generic для marshmallow;
    #   marshmallow-классы детектируются через AnnAssign ratio + Field() сигналы
})

_KNOWN_CONFIG_BASES: frozenset[str] = frozenset({
    "BaseSettings",
    "Settings",
    "Config",
    "AppConfig",
    "BaseConfig",
})


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _extract_base_names(class_node: ast.ClassDef) -> list[str]:
    """Возвращает имена базовых классов из AST-ноды."""
    names: list[str] = []
    for base in class_node.bases:
        # Простой случай: class Foo(Bar)
        if isinstance(base, ast.Name):
            names.append(base.id)
        # Случай с атрибутом: class Foo(pkg.Bar)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _is_docstring_node(node: ast.stmt) -> bool:
# ---------------------------------------------------------------------------
# Возвращает True, если узел является docstring-выражением
# Docstring — это Expr, содержащий строковую константу
# ---------------------------------------------------------------------------
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_trivial_body(body: list[ast.stmt]) -> bool:
# ---------------------------------------------------------------------------
# Проверяет, является ли тело метода тривиальным — то есть допустимым
# для абстрактного метода в интерфейсе
#
# Тривиальные паттерны (после необязательного пропуска начальной docstring):
#   - pass
#   - ... (Ellipsis)
#   - raise NotImplementedError
#   - raise NotImplementedError("сообщение")
#   - только docstring (тело = одна строковая константа)
# ---------------------------------------------------------------------------
    # Пропускаем начальную docstring если она есть — стандартный паттерн
    # документирования абстрактных методов: def foo(self): """..."""\n pass
    effective = list(body)
    if effective and _is_docstring_node(effective[0]):
        effective = effective[1:]

    # Тело состояло только из docstring — тривиально
    if not effective:
        return True

    # Допускаем ровно один оставшийся тривиальный узел
    if len(effective) != 1:
        return False

    stmt = effective[0]

    # pass
    if isinstance(stmt, ast.Pass):
        return True

    # ... (Ellipsis-константа)
    if (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and stmt.value.value is ...
    ):
        return True

    # raise NotImplementedError или raise NotImplementedError("msg")
    if isinstance(stmt, ast.Raise) and stmt.exc is not None:
        exc = stmt.exc
        if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
            return True
        if (
            isinstance(exc, ast.Call)
            and isinstance(exc.func, ast.Name)
            and exc.func.id == "NotImplementedError"
        ):
            return True

    return False


def _is_pure_interface(class_node: ast.ClassDef) -> bool:
# ---------------------------------------------------------------------------
# Возвращает True, если класс является чистым интерфейсом/контрактом
# Критерии (все должны выполняться):
#   1. Хотя бы один FunctionDef в теле класса
#   2. Все FunctionDef задекорированы @abstractmethod ИЛИ имеют тривиальное тело
#      (pass / ... / raise NotImplementedError / docstring / docstring + одно из перечисленного)
#   3. Нет не-абстрактных методов с реальной логикой
#
# Отличает:
#   class IFoo(ABC):           # PURE_INTERFACE — все методы абстрактны
#       @abstractmethod
#       def process(self): ...
#
#   class IFoo(ABC):           # PURE_INTERFACE — docstring + pass допустимы
#       @abstractmethod
#       async def process(self) -> None:
#           """Docstring."""
#           pass
#
# от:
#   class Base(ABC):           # DOMAIN — есть реальный __init__
#       def __init__(self): self.x = 1
#       @abstractmethod
#       def process(self): ...
# ---------------------------------------------------------------------------
    method_nodes = [
        node for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    # Нет методов — не интерфейс (может быть namespace-классом)
    if not method_nodes:
        return False

    for func in method_nodes:
        # Проверяем декораторы: ищем @abstractmethod
        has_abstractmethod = any(
            (isinstance(dec, ast.Name) and dec.id == "abstractmethod")
            or (isinstance(dec, ast.Attribute) and dec.attr == "abstractmethod")
            for dec in func.decorator_list
        )
        if has_abstractmethod:
            continue  # Этот метод — контрактный, окей

        # Метод без @abstractmethod — проверяем тело на тривиальность
        # Тривиальные тела (pass, ..., raise NotImplementedError, docstring,
        # docstring + одно из перечисленного) допустимы в интерфейсе
        if _is_trivial_body(func.body):
            continue

        # Тело нетривиальное — это реальный метод, класс не интерфейс
        return False

    return True


def _compute_infra_score(
    class_node: ast.ClassDef,
    base_names: list[str],
) -> int:
# ---------------------------------------------------------------------------
# Вычисляет InfraScore — сумму сигналов, указывающих на инфраструктурный класс
#
# Каждый сигнал дает +1 балл. Порог для классификации INFRA_MODEL: >= 2
#
# Сигналы:
#   +2  Прямое наследование от известной инфра-базы (_KNOWN_INFRA_BASES)
#   +1  Наличие атрибута __tablename__ (SQLAlchemy ORM)
#   +1  Наличие атрибута model_config (Pydantic v2)
#   +1  Более 70% тела класса составляют AnnAssign (аннотированные поля)
#   +1  Наличие вызовов Column(), Field(), relationship() в теле
# ---------------------------------------------------------------------------
    score = 0

    # Сигнал 1: известная инфра-база (+2 — самый сильный сигнал)
    if any(name in _KNOWN_INFRA_BASES for name in base_names):
        score += 2

    # Сигнал 2: __tablename__ (SQLAlchemy)
    for node in class_node.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "__tablename__"
                for t in node.targets
            )
        ):
            score += 1
            break

    # Сигнал 3: model_config (Pydantic v2)
    for node in class_node.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "model_config"
                for t in node.targets
            )
        ):
            score += 1
            break

    # Сигнал 4: >70% тела — аннотированные поля (AnnAssign)
    body_stmts = [
        n for n in class_node.body
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if body_stmts:
        ann_count = sum(1 for n in body_stmts if isinstance(n, ast.AnnAssign))
        if ann_count / len(body_stmts) > 0.7:
            score += 1

    # Сигнал 5: Column(), Field(), relationship() в теле
    _ORM_FIELD_NAMES = frozenset({"Column", "Field", "relationship", "Mapped", "mapped_column"})
    for node in ast.walk(class_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _ORM_FIELD_NAMES
        ):
            score += 1
            break

    return score


# ---------------------------------------------------------------------------
# Главная функция классификации
# ---------------------------------------------------------------------------

def classify_class(
    class_node: ast.ClassDef,
    import_aliases: dict[str, str] | None = None,
) -> ClassRole:
# ---------------------------------------------------------------------------
# Определяет роль класса по его AST-ноде
# Аргументы:
#   class_node:     AST-нода ClassDef анализируемого класса
#   import_aliases: словарь алиасов импортов {алиас: оригинальное_имя}, напр.: {"BM": "BaseModel", "Base": "DeclarativeBase"}
#                   Используется для обработки `from pydantic import BaseModel as BM`
#
# Возвращает ClassRole: одна из PURE_INTERFACE, INFRA_MODEL, CONFIG, DOMAIN
#
# Порядок проверок (от самого специфичного к наиболее общему):
#   1. PURE_INTERFACE — чистый интерфейс/контракт (все методы абстрактны)
#   2. CONFIG         — конфигурационный класс (BaseSettings и аналоги)
#   3. INFRA_MODEL    — инфраструктурная модель (InfraScore >= 2)
#   4. DOMAIN         — всё остальное
# ---------------------------------------------------------------------------
    aliases = import_aliases or {}

    # Разрешаем алиасы: если база называется BM, а алиас BM -> BaseModel,
    # подставляем оригинальное имя для корректного матчинга с _KNOWN_INFRA_BASES
    raw_bases = _extract_base_names(class_node)
    resolved_bases = [aliases.get(name, name) for name in raw_bases]

    # --- Шаг 1: PURE_INTERFACE ---
    # Проверяем до CONFIG/INFRA_MODEL, так как абстрактный интерфейс
    # может наследовать от ABC (который не в нашем infra-листе), и нам
    # важно сначала определить, что все его методы абстрактны
    if _is_pure_interface(class_node):
        return ClassRole.PURE_INTERFACE

    # --- Шаг 2: CONFIG ---
    # Конфиг-класс определяем до INFRA_MODEL: BaseSettings тоже входит
    # в _KNOWN_INFRA_BASES, но семантически Config != Model.
    # Приоритет CONFIG позволяет точнее маршрутизировать исключение.
    if any(name in _KNOWN_CONFIG_BASES for name in resolved_bases):
        return ClassRole.CONFIG

    # --- Шаг 3: INFRA_MODEL (InfraScore >= 2) ---
    infra_score = _compute_infra_score(class_node, resolved_bases)
    if infra_score >= 2:
        return ClassRole.INFRA_MODEL

    # --- Шаг 4: DOMAIN ---
    return ClassRole.DOMAIN
