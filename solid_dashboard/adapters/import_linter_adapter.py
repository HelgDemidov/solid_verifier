# ===================================================================================================
# Адаптер Import Linter (Import Linter Adapter)
#
# Ключевая роль: Проверка строгих архитектурных контрактов (API Layered Architecture) с использованием утилиты import-linter.
#
# Основные архитектурные задачи:
# 1. Динамическая генерация временного конфига (.importlinter_auto) на основе
#    базового файла .importlinter и актуального списка слоев из solid_config.json.
# 2. Изолированный запуск import-linter CLI в подпроцессе с передачей правильного
#    контекста (PYTHONPATH), охватывающего директорию анализа.
# 3. Порядок слоев берется из layer_order (единственный источник истины);
#    dict.keys() секции layers используется только как fallback.
# 4. Парсинг текстового вывода линтера (ANSI-очистка) для подсчета нарушенных/
#    соблюденных контрактов и извлечения списка конкретных нарушений.
# 5. Структурированное поле violation_details: List[Dict] содержит разобранные
#    нарушения в виде {contract_name, status, broken_imports} — готово для
#    кросс-адаптерной агрегации (report_aggregator, будущий этап).
# ===================================================================================================


import configparser  # стандартный INI-парсер для работы с .importlinter
import os             # работа с путями и файлами
import re             # разбор текста и ANSI-кодов
import subprocess     # запуск lint-imports как отдельного процесса
from typing import Any, Dict, List  # типы для аннотаций

from solid_dashboard.interfaces.analyzer import IAnalyzer  # базовый интерфейс адаптера

# Регулярное выражение для очистки вывода от ANSI-кодов (цветной вывод, рамки и т.п.)
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Паттерн строки с нарушенным импортом: "    some.module -> other.module"
# import-linter выводит такие строки с отступом под именем контракта
_BROKEN_IMPORT_RE = re.compile(r"^\s+(\S+)\s*->\s*(\S+)\s*$")


class ImportLinterAdapter(IAnalyzer):
    # Синхронизирует базовый конфигурационный файл .importlinter с единой моделью solid_config.json:
    # - Читает существующий базовый файл .importlinter через configparser (нечувствительно к порядку полей)
    # - Динамически перезаписывает параметр root_packages под целевую директорию (package_name)
    # - Обновляет архитектурный контракт (блок 'layers') актуальными слоями из layer_order
    # - Сохраняет результат во временный файл (например, .importlinter_auto_app)
    # - Запускает lint-imports --config <temp_file> и безопасно удаляет его после работы

    @property
    def name(self) -> str:
        # Имя адаптера для JSON-отчета
        return "import_linter"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # параметр context требуется интерфейсом IAnalyzer, в этом адаптере не используется
        _ = context

        target_path = os.path.abspath(target_dir)
        project_root = os.path.dirname(target_path)
        # Извлекаем реальное имя пакета (например 'app' или 'src')
        package_name = os.path.basename(target_path)

        base_config_path = os.path.join(project_root, ".importlinter")
        # Делаем имя временного файла уникальным для предотвращения коллизий
        temp_config_path = os.path.join(project_root, f".importlinter_auto_{package_name}")

        if not os.path.exists(base_config_path):
            return self._error_message(f".importlinter not found at {base_config_path}")

        try:
            # Генерируем синхронизированный временный конфиг через configparser
            self.generate_synced_config(
                base_config_path=base_config_path,
                solid_config=config,
                outpath=temp_config_path,
                package_name=package_name,
            )

            # Пробрасываем корень проекта в PYTHONPATH для корректного разрешения импортов
            env = os.environ.copy()
            if "PYTHONPATH" in env:
                env["PYTHONPATH"] = f"{project_root}{os.pathsep}{env['PYTHONPATH']}"
            else:
                env["PYTHONPATH"] = project_root

            cmd = ["lint-imports", "--config", temp_config_path]
            completed = subprocess.run(
                cmd,
                cwd=project_root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            raw_console = completed.stdout or completed.stderr or ""
            clean_output = ANSI_ESCAPE.sub("", raw_console).strip()

            # returncode=0 — все контракты соблюдены, returncode=1 — есть нарушения;
            # любой другой код — ошибка среды выполнения (не найден пакет, синтаксис конфига и т.п.)
            if completed.returncode not in (0, 1):
                return self._error_message(
                    f"lint-imports exited with code {completed.returncode}.\n{clean_output}"
                )

            linting_passed = completed.returncode == 0
            kept, broken = self._parse_contract_stats(clean_output, linting_passed)

            # Извлекаем имена нарушенных контрактов и строим оба поля:
            # violations: List[str] — для обратной совместимости JSON-отчета
            # violation_details: List[Dict] — структурированный формат для будущей агрегации
            violations: List[str] = []
            violation_details: List[Dict[str, Any]] = []

            violations, violation_details = self._parse_violations(clean_output)

            return {
                "is_success": linting_passed,
                "contracts_checked": kept + broken,
                "broken_contracts": broken,
                "kept_contracts": kept,
                "violations": violations,
                "violation_details": violation_details,
                "raw_output": clean_output,
            }
        except FileNotFoundError:
            return self._error_message(
                "Command lint-imports not found. Ensure import-linter is installed."
            )
        except Exception as exc:
            return self._error_message(f"ImportLinterAdapter failed: {exc}")
        finally:
            # Гарантированно удаляем временный файл даже при исключении
            if os.path.exists(temp_config_path):
                try:
                    os.remove(temp_config_path)
                except OSError:
                    pass

    def generate_synced_config(
        self,
        base_config_path: str,
        solid_config: Dict[str, Any],
        outpath: str,
        package_name: str,
    ) -> None:
        """
        Читает базовый .importlinter через configparser, заменяет root_packages
        на package_name, обновляет блок layers во всех контрактах типа layers
        и сохраняет результат в outpath.

        Порядок слоев определяется исключительно из layer_order (единственный источник
        истины). Порядок ключей dict из секции 'layers' JSON-конфига намеренно
        игнорируется — он зависит от порядка вставки и нестабилен при редактировании.
        Если layer_order отсутствует, используется dict.keys() как аварийный fallback.

        ignore_imports в сгенерированном конфиге не проставляются: utility_layers
        (core, schemas, demo и т.п.) не участвуют в layers-контракте, поэтому
        паттерны ignore_imports для них порождали unmatched-предупреждения и
        приводили к returncode=1 в отдельных версиях import-linter.
        При необходимости ignore_imports следует вести вручную в базовом .importlinter.
        """
        cfg = configparser.RawConfigParser()
        # Сохраняем регистр ключей — configparser по умолчанию приводит к нижнему
        cfg.optionxform = str  # type: ignore[assignment]
        cfg.read(base_config_path, encoding="utf-8")

        if cfg.has_section("importlinter"):
            # Записываем root_packages как multiline INI-значение — критично для import-linter:
            # при однострочном значении ('app') import-linter итерирует строку посимвольно
            # и получает ['a','p','p']; multiline-форма гарантирует разбор через splitlines()
            cfg.set("importlinter", "root_packages", f"\n    {package_name}")
            # unmatched_ignore_imports=warn: страховочный слой на случай будущих
            # ручных правок базового .importlinter (паттерн без совпадений → warn, не error)
            cfg.set("importlinter", "unmatched_ignore_imports", "warn")

        # layer_order — единственный источник истины для порядка слоев;
        # dict.keys() из 'layers' используется только как аварийный fallback,
        # так как порядок ключей JSON-объекта нестабилен при ручном редактировании
        layer_names: List[str] = solid_config.get("layer_order") or list(
            solid_config.get("layers", {}).keys()
        )

        # Итерируемся по всем секциям — обрабатываем каждый контракт независимо
        for section in cfg.sections():
            if not section.startswith("importlinter:contract:"):
                continue

            # Определяем тип контракта; пропускаем секции без поля type
            try:
                contract_type = cfg.get(section, "type").strip().lower()
            except configparser.NoOptionError:
                continue

            # Обновляем только контракты типа layers; forbidden/independence не трогаем
            if contract_type != "layers" or not layer_names:
                continue

            # Формируем multiline-строку слоев в формате INI (отступ = 4 пробела);
            # имена слоев без префикса пакета — containers в .importlinter задает пространство имен
            layers_value = "\n" + "\n".join(
                f"    {layer}" for layer in layer_names
            )
            cfg.set(section, "layers", layers_value)

        # Записываем итоговый конфиг во временный файл
        with open(outpath, "w", encoding="utf-8") as f:
            cfg.write(f)

    @staticmethod
    def _parse_violations(
        output: str,
    ) -> tuple[List[str], List[Dict[str, Any]]]:
        """
        Разбирает вывод lint-imports и возвращает два представления нарушений:

        violations: List[str]
            Плоский список имен нарушенных контрактов — для обратной совместимости
            существующего JSON-отчета. Пример: ["Scopus API layered architecture"]

        violation_details: List[Dict]
            Структурированный список нарушений вида:
            [
              {
                "contract_name": "Scopus API layered architecture",
                "status": "BROKEN",
                "broken_imports": [
                  {"importer": "app.routers.search", "imported": "app.models.paper"}
                ]
              }
            ]
            Используется для кросс-адаптерной агрегации (будущий report_aggregator).
            При отсутствии строк с '->' broken_imports остается пустым списком —
            это корректное состояние: факт нарушения подтвержден returncode, детали недоступны.
        """
        violations: List[str] = []
        violation_details: List[Dict[str, Any]] = []

        # Текущий контракт, для которого собираем broken_imports
        current_detail: Dict[str, Any] | None = None

        for line in output.splitlines():
            stripped = line.strip()

            # Строка вида "Contract Name BROKEN" — начало нового нарушенного контракта
            if stripped.endswith(" BROKEN"):
                # Сохраняем предыдущий незакрытый контракт перед началом нового
                if current_detail is not None:
                    violation_details.append(current_detail)

                contract_name = stripped[: -len(" BROKEN")].rstrip()
                if contract_name:
                    violations.append(contract_name)
                    current_detail = {
                        "contract_name": contract_name,
                        "status": "BROKEN",
                        "broken_imports": [],
                    }
                else:
                    current_detail = None
                continue

            # Строка вида "    importer -> imported" — конкретный сломанный импорт
            if current_detail is not None:
                match = _BROKEN_IMPORT_RE.match(line)
                if match:
                    current_detail["broken_imports"].append({
                        "importer": match.group(1),
                        "imported": match.group(2),
                    })

        # Сохраняем последний незакрытый контракт после конца вывода
        if current_detail is not None:
            violation_details.append(current_detail)

        return violations, violation_details

    @staticmethod
    def _parse_contract_stats(output: str, linting_passed: bool) -> tuple[int, int]:
        """
        Извлекает количество kept/broken контрактов из строки вида
        'Contracts: 1 kept, 0 broken.' или похожих вариаций.
        Fallback при несовпадении: 1 kept или 1 broken по returncode.
        """
        kept = 0
        broken = 0

        stats_match = re.search(
            r"(?:contracts?\s*:?[^0-9]*kept[^0-9]*([0-9]+)[^0-9]*broken[^0-9]*([0-9]+))",
            output,
            re.IGNORECASE,
        )
        if stats_match:
            kept = int(stats_match.group(1))
            broken = int(stats_match.group(2))
        else:
            # Fallback: формат вывода не распознан, но returncode однозначен
            if linting_passed:
                kept = 1
            else:
                broken = 1

        return kept, broken

    @staticmethod
    def _error_message(msg: str) -> Dict[str, Any]:
        # Унифицированный формат ошибки адаптера — совместим с IAnalyzer
        return {
            "is_success": False,
            "error": msg,
            "contracts_checked": 0,
            "broken_contracts": 0,
            "kept_contracts": 0,
            "violations": [],
            "violation_details": [],
            "raw_output": "",
        }
