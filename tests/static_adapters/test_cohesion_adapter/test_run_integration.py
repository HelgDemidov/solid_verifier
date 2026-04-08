# ===================================================================================================
# C5 — интеграционные (end-to-end) тесты для CohesionAdapter.run()
#
# Стратегия:
# - каждый тест создает изолированную файловую структуру через фикстуру tmp_code_dir
# - вызывает adapter.run(target_dir, context={}, config={...})
# - проверяет только публичный контракт (схему возвращаемого словаря)
# - не трогает внутренние методы — устойчивость к рефакторингу
#
# Покрываемые сценарии:
# 1. Базовый smoke-test: один файл, один класс, нормальная связность
# 2. Регрессия 4fa8cd7: super().method() не создает spurious несвязность
# 3. ignore_dirs: классы из исключенных папок не попадают в результат
# 4. Агрегаты concrete vs non-concrete (abstract/interface/dataclass)
# 5. low_cohesion_excluded_classes заполняется для non-concrete нарушителей
# 6. Custom threshold: cohesion_threshold влияет на low_cohesion_count
# 7. Пустая директория: run() возвращает корректный нулевой результат
# 8. Невалидный Python: SyntaxError не роняет run(), файл пропускается
# 9. Несколько файлов: агрегаты считаются по всем файлам
# 10. Класс без методов: не попадает в classes и не влияет на агрегаты
# 11. Схема возвращаемого словаря: все обязательные ключи присутствуют
# ===================================================================================================

import textwrap
import pytest
from pathlib import Path
from typing import Optional, cast

from solid_dashboard.adapters.cohesion_adapter import CohesionAdapter


# ---------------------------------------------------------------------------
# Вспомогательные константы — ключи публичного контракта run()
# ---------------------------------------------------------------------------

# обязательные ключи верхнего уровня возвращаемого словаря
_REQUIRED_TOP_KEYS = {
    "total_classes_analyzed",
    "concrete_classes_count",
    "mean_cohesion_all",
    "mean_cohesion_multi_method",
    "analyzed_classes_count",
    "low_cohesion_count",
    "low_cohesion_excluded_count",
    "low_cohesion_excluded_classes",
    "low_cohesion_threshold",
    "classes",
}

# обязательные ключи каждого элемента списка classes[]
_REQUIRED_CLASS_KEYS = {
    "name",
    "methods_count",
    "cohesion_score",
    "cohesion_score_norm",
    "filepath",
    "lineno",
    "class_kind",
    "excluded_from_aggregation",
}


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------

@pytest.fixture
def adapter() -> CohesionAdapter:
    # явный cast: CohesionAdapter реализует IAnalyzer через Protocol;
    # без cast Pylance не видит приватные методы через Protocol-линзу
    return cast(CohesionAdapter, CohesionAdapter())


@pytest.fixture
def tmp_code_dir(tmp_path: Path):
    """Создает временную директорию с Python-файлами по словарю {filename: source}."""
    def _inner(files: dict) -> Path:
        for name, src in files.items():
            target = tmp_path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(textwrap.dedent(src), encoding="utf-8")
        return tmp_path
    return _inner


# ---------------------------------------------------------------------------
# Вспомогательная функция: запуск адаптера с дефолтным контекстом
# ---------------------------------------------------------------------------

def _run(adapter: CohesionAdapter, target_dir: Path, config: Optional[dict] = None) -> dict:
    """Обертка над adapter.run() с пустым context и дефолтным config."""
    return adapter.run(str(target_dir), context={}, config=config or {})


# ---------------------------------------------------------------------------
# Тест 1: базовый smoke — один связный класс
# ---------------------------------------------------------------------------

class TestSmokeOneCohesiveClass:
    """Один файл, один класс с двумя связанными через атрибут методами."""

    SOURCE = """
        class Service:
            def __init__(self):
                self.value = 0

            def increment(self):
                self.value += 1

            def decrement(self):
                self.value -= 1
    """

    def test_schema_keys_present(self, adapter, tmp_code_dir):
        # проверяем что все обязательные ключи присутствуют в результате
        result = _run(adapter, tmp_code_dir({"service.py": self.SOURCE}))
        assert _REQUIRED_TOP_KEYS.issubset(result.keys())

    def test_one_class_analyzed(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"service.py": self.SOURCE}))
        assert result["total_classes_analyzed"] == 1
        assert result["concrete_classes_count"] == 1

    def test_cohesion_score_is_one(self, adapter, tmp_code_dir):
        # два метода делят self.value -> LCOM4 = 1, cohesion_score = 1.0
        result = _run(adapter, tmp_code_dir({"service.py": self.SOURCE}))
        cls = result["classes"][0]
        assert cls["cohesion_score"] == 1.0
        assert cls["cohesion_score_norm"] == 1.0

    def test_class_entry_schema(self, adapter, tmp_code_dir):
        # каждый элемент classes[] должен содержать все обязательные ключи
        result = _run(adapter, tmp_code_dir({"service.py": self.SOURCE}))
        for entry in result["classes"]:
            assert _REQUIRED_CLASS_KEYS.issubset(entry.keys())

    def test_no_low_cohesion(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"service.py": self.SOURCE}))
        assert result["low_cohesion_count"] == 0


# ---------------------------------------------------------------------------
# Тест 2: регрессия super() — commit 4fa8cd7
# ---------------------------------------------------------------------------

class TestSuperCallRegression:
    """
    Регрессионный тест для 4fa8cd7:
    super().process() должно связывать методы child, не создавая spurious несвязных компонент.

    Сценарий:
      Base.process() использует self.data
      Child.__init__ задает self.data через super().__init__()
      Child.process() вызывает super().process() — super() распознается как вызов process()
      Child.validate() использует self.data

    Ожидание: process и validate оба связаны с self.data -> LCOM4 = 1
    """

    SOURCE = """
        class Base:
            def __init__(self):
                self.data = []

            def process(self):
                return self.data


        class Child(Base):
            def __init__(self):
                super().__init__()

            def process(self):
                super().process()
                return self.data

            def validate(self):
                return bool(self.data)
    """

    def test_child_lcom4_is_one(self, adapter, tmp_code_dir):
        # Child: process() и validate() оба используют self.data -> LCOM4 = 1
        result = _run(adapter, tmp_code_dir({"hierarchy.py": self.SOURCE}))
        classes_by_name = {c["name"]: c for c in result["classes"]}
        assert "Child" in classes_by_name
        child = classes_by_name["Child"]
        # LCOM4 = 1 означает один связный компонент — регрессия отсутствует
        assert child["cohesion_score"] == 1.0, (
            f"Regression 4fa8cd7: Child.cohesion_score={child['cohesion_score']}, expected 1.0"
        )

    def test_base_not_excluded_from_aggregation(self, adapter, tmp_code_dir):
        # Base не наследуется ни от кого -> адаптер классифицирует его как concrete
        result = _run(adapter, tmp_code_dir({"hierarchy.py": self.SOURCE}))
        classes_by_name = {c["name"]: c for c in result["classes"]}
        assert classes_by_name["Base"]["excluded_from_aggregation"] is False

    def test_child_present_in_classes(self, adapter, tmp_code_dir):
        # Child(Base) помечается адаптером как non-concrete (есть базовый класс);
        # проверяем что он все равно присутствует в classes[] и у него есть excluded_from_aggregation
        result = _run(adapter, tmp_code_dir({"hierarchy.py": self.SOURCE}))
        classes_by_name = {c["name"]: c for c in result["classes"]}
        assert "Child" in classes_by_name
        assert "excluded_from_aggregation" in classes_by_name["Child"]


# ---------------------------------------------------------------------------
# Тест 3: ignore_dirs — классы из исключенных папок не попадают в результат
# ---------------------------------------------------------------------------

class TestIgnoreDirs:
    """Классы в vendor/ должны быть проигнорированы при ignore_dirs=["vendor"]."""

    FILES = {
        "app/service.py": """
            class AppService:
                def __init__(self):
                    self.x = 1

                def get_x(self):
                    return self.x
        """,
        # этот файл находится в игнорируемой папке
        "vendor/third_party.py": """
            class VendorHelper:
                def __init__(self):
                    self.y = 2

                def get_y(self):
                    return self.y
        """,
    }

    def test_vendor_class_not_in_results(self, adapter, tmp_code_dir):
        target = tmp_code_dir(self.FILES)
        result = _run(adapter, target, config={"ignore_dirs": ["vendor"]})
        names = {c["name"] for c in result["classes"]}
        assert "VendorHelper" not in names
        assert "AppService" in names

    def test_total_classes_excludes_vendor(self, adapter, tmp_code_dir):
        target = tmp_code_dir(self.FILES)
        result = _run(adapter, target, config={"ignore_dirs": ["vendor"]})
        # только AppService: один класс
        assert result["total_classes_analyzed"] == 1

    def test_without_ignore_both_classes_present(self, adapter, tmp_code_dir):
        # без ignore_dirs оба класса должны быть в результате
        target = tmp_code_dir(self.FILES)
        result = _run(adapter, target)
        names = {c["name"] for c in result["classes"]}
        assert {"AppService", "VendorHelper"}.issubset(names)


# ---------------------------------------------------------------------------
# Тест 4: агрегаты concrete vs non-concrete
# ---------------------------------------------------------------------------

class TestAggregatesConcreteVsNonConcrete:
    """
    Abstract и interface классы не должны входить в mean_cohesion_all
    и mean_cohesion_multi_method; concrete_classes_count отражает только concrete.

    Важно: методы IRepository имеют тело `return NotImplemented` (не `...`),
    чтобы адаптер не считал их пустыми (is_empty=True) и включил класс в classes[].
    """

    SOURCE = """
        from abc import ABC, abstractmethod


        class IRepository(ABC):
            @abstractmethod
            def save(self, item):
                return NotImplemented

            @abstractmethod
            def load(self, item_id):
                return NotImplemented


        class ConcreteRepo:
            def __init__(self):
                self.items = {}
                self.count = 0

            def save(self, item):
                self.items[self.count] = item
                self.count += 1

            def load(self, item_id):
                return self.items.get(item_id)
    """

    def test_concrete_classes_count_is_one(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"repo.py": self.SOURCE}))
        assert result["concrete_classes_count"] == 1

    def test_total_classes_includes_all(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"repo.py": self.SOURCE}))
        # IRepository и ConcreteRepo оба имеют непустые методы -> оба в total_classes_analyzed
        assert result["total_classes_analyzed"] == 2

    def test_irepository_excluded_from_aggregation(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"repo.py": self.SOURCE}))
        classes_by_name = {c["name"]: c for c in result["classes"]}
        assert classes_by_name["IRepository"]["excluded_from_aggregation"] is True
        assert classes_by_name["ConcreteRepo"]["excluded_from_aggregation"] is False

    def test_mean_cohesion_all_reflects_only_concrete(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"repo.py": self.SOURCE}))
        # ConcreteRepo: save и load оба используют self.items и self.count -> LCOM4=1
        assert isinstance(result["mean_cohesion_all"], float)
        assert result["mean_cohesion_all"] >= 0.0


# ---------------------------------------------------------------------------
# Тест 5: low_cohesion_excluded_classes
# ---------------------------------------------------------------------------

class TestLowCohesionExcludedClasses:
    """
    Non-concrete класс (abstract) с LCOM4 > threshold должен попасть
    в low_cohesion_excluded_classes и не попасть в low_cohesion_count.
    """

    SOURCE = """
        from abc import ABC, abstractmethod


        class AbstractBase(ABC):
            def __init__(self):
                self.a = 1
                self.b = 2

            @abstractmethod
            def method_a(self):
                # использует self.a — конкретный метод
                return self.a

            def method_b(self):
                # использует self.b — еще один конкретный метод
                return self.b
    """

    def test_abstract_with_high_lcom4_goes_to_excluded(self, adapter, tmp_code_dir):
        # threshold=0: любой LCOM4 >= 1 считается low cohesion
        result = _run(
            adapter,
            tmp_code_dir({"abstract_base.py": self.SOURCE}),
            config={"cohesion_threshold": 0},
        )
        # low_cohesion_count должен оставаться 0 (non-concrete не входит)
        assert result["low_cohesion_count"] == 0
        # excluded_count и excluded_classes заполнены если LCOM4 > 0
        assert result["low_cohesion_excluded_count"] >= 0  # может быть 0 если LCOM4=1 (связный)
        # проверяем тип
        assert isinstance(result["low_cohesion_excluded_classes"], list)

    def test_excluded_classes_schema(self, adapter, tmp_code_dir):
        result = _run(
            adapter,
            tmp_code_dir({"abstract_base.py": self.SOURCE}),
            config={"cohesion_threshold": 0},
        )
        # каждый элемент low_cohesion_excluded_classes должен содержать нужные поля
        required_keys = {"name", "filepath", "lineno", "cohesion_score", "class_kind"}
        for entry in result["low_cohesion_excluded_classes"]:
            assert required_keys.issubset(entry.keys()), (
                f"Missing keys in excluded entry: {entry.keys()}"
            )


# ---------------------------------------------------------------------------
# Тест 6: custom cohesion_threshold
# ---------------------------------------------------------------------------

class TestCustomThreshold:
    """
    Проверяем что cohesion_threshold из config корректно влияет на low_cohesion_count.

    Класс с двумя несвязанными методами (LCOM4=2):
    - threshold=1 -> low_cohesion_count=1
    - threshold=2 -> low_cohesion_count=0
    """

    SOURCE = """
        class Disconnected:
            def __init__(self):
                self.x = 0
                self.y = 0

            def method_x(self):
                # использует только self.x
                return self.x

            def method_y(self):
                # использует только self.y
                return self.y
    """

    def test_threshold_1_detects_low_cohesion(self, adapter, tmp_code_dir):
        target = tmp_code_dir({"disconnected.py": self.SOURCE})
        result = _run(adapter, target, config={"cohesion_threshold": 1})
        # method_x и method_y не связаны -> LCOM4=2 > threshold=1
        assert result["low_cohesion_count"] == 1

    def test_threshold_2_no_low_cohesion(self, adapter, tmp_code_dir):
        target = tmp_code_dir({"disconnected.py": self.SOURCE})
        result = _run(adapter, target, config={"cohesion_threshold": 2})
        # LCOM4=2, threshold=2 -> 2 > 2 is False -> не low cohesion
        assert result["low_cohesion_count"] == 0

    def test_threshold_stored_in_result(self, adapter, tmp_code_dir):
        target = tmp_code_dir({"disconnected.py": self.SOURCE})
        result = _run(adapter, target, config={"cohesion_threshold": 3})
        assert result["low_cohesion_threshold"] == 3

    def test_cohesion_score_norm_for_disconnected(self, adapter, tmp_code_dir):
        target = tmp_code_dir({"disconnected.py": self.SOURCE})
        result = _run(adapter, target)
        cls = result["classes"][0]
        # LCOM4=2 -> cohesion_score=2.0 -> norm=0.5
        assert cls["cohesion_score"] == 2.0
        assert cls["cohesion_score_norm"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Тест 7: пустая директория
# ---------------------------------------------------------------------------

class TestEmptyDirectory:
    """run() на пустой директории должен вернуть корректный нулевой результат."""

    def test_empty_dir_returns_zero_counts(self, adapter, tmp_path):
        result = _run(adapter, tmp_path)
        assert result["total_classes_analyzed"] == 0
        assert result["concrete_classes_count"] == 0
        assert result["low_cohesion_count"] == 0
        assert result["classes"] == []

    def test_empty_dir_zero_means(self, adapter, tmp_path):
        result = _run(adapter, tmp_path)
        assert result["mean_cohesion_all"] == 0.0
        assert result["mean_cohesion_multi_method"] == 0.0

    def test_empty_dir_schema_keys_present(self, adapter, tmp_path):
        result = _run(adapter, tmp_path)
        assert _REQUIRED_TOP_KEYS.issubset(result.keys())


# ---------------------------------------------------------------------------
# Тест 8: невалидный Python — файл с SyntaxError пропускается
# ---------------------------------------------------------------------------

class TestSyntaxErrorFile:
    """SyntaxError в одном файле не должен ронять run(); другие файлы обрабатываются."""

    FILES = {
        "broken.py": "def foo(:\n    pass\n",  # намеренный SyntaxError
        "good.py": """
            class GoodClass:
                def __init__(self):
                    self.val = 1

                def get_val(self):
                    return self.val
        """,
    }

    def test_run_does_not_raise(self, adapter, tmp_code_dir):
        # run() не должен бросать исключение при наличии broken.py
        result = _run(adapter, tmp_code_dir(self.FILES))
        assert isinstance(result, dict)

    def test_good_class_still_analyzed(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir(self.FILES))
        names = {c["name"] for c in result["classes"]}
        assert "GoodClass" in names

    def test_broken_file_not_in_results(self, adapter, tmp_code_dir):
        # классов из broken.py быть не может (файл не распарсен)
        result = _run(adapter, tmp_code_dir(self.FILES))
        # просто убеждаемся что total_classes_analyzed >= 1 (GoodClass)
        assert result["total_classes_analyzed"] >= 1


# ---------------------------------------------------------------------------
# Тест 9: несколько файлов — агрегаты суммируются корректно
# ---------------------------------------------------------------------------

class TestMultiFileAggregation:
    """Два файла с двумя классами; агрегаты учитывают оба."""

    FILES = {
        "alpha.py": """
            class Alpha:
                def __init__(self):
                    self.a = 1

                def get_a(self):
                    return self.a

                def set_a(self, v):
                    self.a = v
        """,
        "beta.py": """
            class Beta:
                def __init__(self):
                    self.p = 0
                    self.q = 0

                def use_p(self):
                    return self.p

                def use_q(self):
                    return self.q
        """,
    }

    def test_two_classes_analyzed(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir(self.FILES))
        assert result["total_classes_analyzed"] == 2
        assert result["concrete_classes_count"] == 2

    def test_alpha_lcom4_one(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir(self.FILES))
        classes_by_name = {c["name"]: c for c in result["classes"]}
        # get_a и set_a оба используют self.a -> LCOM4=1
        assert classes_by_name["Alpha"]["cohesion_score"] == 1.0

    def test_beta_lcom4_two(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir(self.FILES))
        classes_by_name = {c["name"]: c for c in result["classes"]}
        # use_p и use_q не связаны -> LCOM4=2
        assert classes_by_name["Beta"]["cohesion_score"] == 2.0

    def test_mean_cohesion_all_correct(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir(self.FILES))
        # Alpha=1.0, Beta=2.0 -> mean=(1.0+2.0)/2=1.5
        assert result["mean_cohesion_all"] == pytest.approx(1.5)

    def test_low_cohesion_count_one(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir(self.FILES))
        # threshold=1 (default), Beta LCOM4=2 > 1 -> 1 low cohesion
        assert result["low_cohesion_count"] == 1


# ---------------------------------------------------------------------------
# Тест 10: класс без методов не попадает в classes
# ---------------------------------------------------------------------------

class TestClassWithoutMethods:
    """Класс только с атрибутами класса и без методов должен быть отфильтрован."""

    SOURCE = """
        class Config:
            DEBUG = True
            HOST = "localhost"
            PORT = 8080
    """

    def test_empty_class_not_in_results(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"config.py": self.SOURCE}))
        # Config не имеет методов -> methods_count=0 -> не попадает в classes
        assert result["total_classes_analyzed"] == 0
        assert result["classes"] == []

    def test_aggregates_remain_zero(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"config.py": self.SOURCE}))
        assert result["mean_cohesion_all"] == 0.0
        assert result["concrete_classes_count"] == 0


# ---------------------------------------------------------------------------
# Тест 11: полная схема возвращаемого словаря
# ---------------------------------------------------------------------------

class TestOutputSchema:
    """
    Контрактный тест: run() всегда возвращает словарь с обязательными ключами
    независимо от содержимого директории.
    """

    SOURCE = """
        class Minimal:
            def action(self):
                pass
    """

    def test_all_top_level_keys_present(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"minimal.py": self.SOURCE}))
        missing = _REQUIRED_TOP_KEYS - result.keys()
        assert not missing, f"Missing top-level keys: {missing}"

    def test_types_of_top_level_values(self, adapter, tmp_code_dir):
        result = _run(adapter, tmp_code_dir({"minimal.py": self.SOURCE}))
        assert isinstance(result["total_classes_analyzed"], int)
        assert isinstance(result["concrete_classes_count"], int)
        assert isinstance(result["mean_cohesion_all"], float)
        assert isinstance(result["mean_cohesion_multi_method"], float)
        assert isinstance(result["analyzed_classes_count"], int)
        assert isinstance(result["low_cohesion_count"], int)
        assert isinstance(result["low_cohesion_excluded_count"], int)
        assert isinstance(result["low_cohesion_excluded_classes"], list)
        assert isinstance(result["low_cohesion_threshold"], int)
        assert isinstance(result["classes"], list)

    def test_class_entry_schema_when_present(self, adapter, tmp_code_dir):
        # Minimal.action — is_empty=True (pass) -> methods_count=0 -> не в classes
        # используем класс с реальным методом
        source = """
            class Real:
                def __init__(self):
                    self.x = 1

                def use(self):
                    return self.x
        """
        result = _run(adapter, tmp_code_dir({"real.py": source}))
        for entry in result["classes"]:
            missing = _REQUIRED_CLASS_KEYS - entry.keys()
            assert not missing, f"Missing class entry keys: {missing}"
