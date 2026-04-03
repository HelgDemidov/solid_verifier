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
# 3. Применение фильтра ignore_dirs (настройка ignore_imports) для исключения инфраструктурного кода из архитектурных проверок.
# 4. Парсинг текстового вывода линтера (ANSI-очистка) для подсчета нарушенных/
#    соблюденных контрактов и извлечения списка конкретных нарушений.
# ===================================================================================================


import os  # работа с путями и файлами
import re  # разбор текста и ANSI-кодов
import subprocess  # запуск lint-imports как отдельного процесса
from typing import Any, Dict, List  # типы для аннотаций

from solid_dashboard.interfaces.analyzer import IAnalyzer  # базовый интерфейс адаптера

# Регулярное выражение для очистки вывода от ANSI-кодов (цветной вывод, рамки и т.п.)
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class ImportLinterAdapter(IAnalyzer):
    # Синхронизирует базовый конфигурационный файл .importlinter с единой моделью solid_config.json:
    # - Читает существующий базовый файл .importlinter
    # - Динамически перезаписывает параметр root_packages под целевую директорию (package_name)
    # - Обновляет архитектурный контракт (блок 'layers') актуальными слоями проекта.
    # - Автоматически генерирует правила ignore_imports для исключения папок из ignore_dirs
    # - Сохраняет результат во временный файл (например, .importlinter_auto_app)
    # - Запускает lint-imports --config <temp_file> и безопасно удаляет его после работы

    @property
    def name(self) -> str:
        # Имя адаптера для JSON-отчета
        return "import_linter"

    def run(self, target_dir: str, context: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
        # комментарий (ru): параметр context требуется интерфейсом
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
            # Извлекаем ignore_dirs
            ignore_dirs_cfg = config.get("ignore_dirs") or []
            ignore_dirs = [d.strip() for d in ignore_dirs_cfg if d and d.strip()]

            # Передаем package_name и ignore_dirs в генератор
            self.generate_synced_config(
                base_config_path=base_config_path,
                solid_config=config,
                outpath=temp_config_path,
                package_name=package_name,
                ignore_dirs=ignore_dirs
            )

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

            if completed.returncode not in (0, 1):
                return self._error_message(
                    f"lint-imports exited with code {completed.returncode}.\n{clean_output}"
                )

            linting_passed = (completed.returncode == 0)
            kept, broken = self._parse_contract_stats(clean_output, linting_passed)

            violations: List[str] = []
            for line in clean_output.splitlines():
                stripped = line.strip()
                if stripped.endswith(" BROKEN"):
                    name_part = stripped[:-len(" BROKEN")].rstrip()
                    if name_part:
                        violations.append(name_part)

            return {
                "is_success": linting_passed,
                "contracts_checked": kept + broken,
                "broken_contracts": broken,
                "kept_contracts": kept,
                "violations": violations,
                "raw_output": clean_output,
            }
        except FileNotFoundError:
            return self._error_message(
                "Command lint-imports not found. Ensure import-linter is installed."
            )
        except Exception as exc:
            return self._error_message(f"ImportLinterAdapter failed: {exc}")
        finally:
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
        ignore_dirs: List[str]
    ) -> None:
        """
        Читает базовый .importlinter, заменяет root_packages на package_name, 
        заменяет слои на слои из solid_config, добавляет ignore_imports и сохраняет в outpath.
        """
        with open(base_config_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        layer_config: Dict[str, Any] = solid_config.get("layers", {})
        layer_names = list(layer_config.keys())

        if not layer_names:
            with open(outpath, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return

        result_lines: List[str] = []
        in_layers_contract = False
        in_layers_block = False

        for line in lines:
            stripped = line.strip()

            # комментарий (ru): динамически перезаписываем root_packages под пакет из пайплайна
            if stripped.startswith("root_packages"):
                result_lines.append(f"root_packages = {package_name}\n")
                continue

            if stripped.startswith("[importlinter:contract:") and not in_layers_contract:
                result_lines.append(line)
                in_layers_contract = True
                in_layers_block = False
                continue

            if in_layers_contract:
                if stripped.lower().startswith("type ") and "layers" not in stripped.lower():
                    result_lines.append(line)
                    in_layers_contract = False
                    in_layers_block = False
                    continue

                if stripped.lower().startswith("layers:"):
                    result_lines.append("layers:\n")
                    for layer in layer_names:
                        result_lines.append(f"    {package_name}.{layer}\n")
                    
                    # комментарий (ru): добавляем ignore_imports для игнорируемых директорий
                    if ignore_dirs:
                        result_lines.append("\nignore_imports:\n")
                        for d in ignore_dirs:
                            result_lines.append(f"    {package_name}.{d}.* -> *\n")
                            result_lines.append(f"    * -> {package_name}.{d}.*\n")
                            
                    in_layers_block = True
                    continue

                if in_layers_block:
                    if stripped.startswith("[importlinter:contract:") or stripped.startswith("[importlinter:"):
                        in_layers_contract = stripped.startswith("[importlinter:contract:")
                        in_layers_block = False
                        result_lines.append(line)
                    continue

                result_lines.append(line)
                if stripped.startswith("[importlinter:contract:") and not stripped.lower().startswith("layers:"):
                    in_layers_contract = False
                    in_layers_block = False
                    continue
            else:
                result_lines.append(line)

        if not result_lines:
            result_lines = lines

        with open(outpath, "w", encoding="utf-8") as f:
            f.writelines(result_lines)

    @staticmethod
    def _parse_contract_stats(output: str, linting_passed: bool) -> tuple[int, int]:
        """
        Пытается вытащить из вывода строки вида 'Contracts: 1 kept, 0 broken.'
        или похожие вариации. В крайнем случае — fallback: 1 kept/1 broken.
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
            if linting_passed:
                kept = 1
            else:
                broken = 1

        return kept, broken

    @staticmethod
    def _error_message(msg: str) -> Dict[str, Any]:
        # Унифицированный формат ошибки адаптера
        return {
            "is_success": False,
            "error": msg,
            "contracts_checked": 0,
            "broken_contracts": 0,
            "kept_contracts": 0,
            "violations": [],
            "raw_output": "",
        }