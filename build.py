#!/usr/bin/env python3
"""
build.py — Валидация и проверка исходников расширения "Умная Дебиторка"

Использование:
    python build.py          # валидация всех файлов
    python build.py --check  # только проверка структуры
    python build.py --stats  # статистика по коду
"""

import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# ─── Конфигурация ────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
SRC_DIR = ROOT / "src" / "cf"

REQUIRED_FILES = [
    "ConfigDumpInfo.xml",
    "Configuration.xml",
    "CommonModules/СкорингКонтрагентов/СкорингКонтрагентов.xml",
    "CommonModules/СкорингКонтрагентов/Ext/Module.bsl",
    "DataProcessors/УмнаяДебиторка/УмнаяДебиторка.xml",
    "DataProcessors/УмнаяДебиторка/Ext/ObjectModule.bsl",
    "DataProcessors/УмнаяДебиторка/Forms/ОсновнаяФорма/ОсновнаяФорма.xml",
    "DataProcessors/УмнаяДебиторка/Forms/ОсновнаяФорма/Ext/Form.xml",
    "DataProcessors/УмнаяДебиторка/Forms/ОсновнаяФорма/Ext/Module.bsl",
]

BSL_EXPORTED_FUNCTIONS = [
    "РассчитатьСкоринг",
    "ПолучитьСкорингВсехКонтрагентов",
    "ПолучитьОписаниеСкоринга",
]

SCORING_FACTORS = [
    "РассчитатьФакторДнейПросрочки",
    "РассчитатьФакторДолиПросрочки",
    "РассчитатьФакторИсторииПлатежей",
    "РассчитатьФакторКоличестваПросроченных",
]

# ─── Утилиты ──────────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}✓{RESET} {msg}")
def warn(msg): print(f"  {YELLOW}⚠{RESET} {msg}")
def err(msg):  print(f"  {RED}✗{RESET} {msg}")
def head(msg): print(f"\n{BOLD}{msg}{RESET}")


# ─── Проверки ─────────────────────────────────────────────────────────────────

def check_structure() -> bool:
    head("1. Проверка структуры файлов")
    all_ok = True
    for rel in REQUIRED_FILES:
        path = SRC_DIR / rel
        if path.exists():
            ok(rel)
        else:
            err(f"ОТСУТСТВУЕТ: {rel}")
            all_ok = False
    return all_ok


def check_xml_valid() -> bool:
    head("2. Валидация XML-файлов")
    all_ok = True
    for path in SRC_DIR.rglob("*.xml"):
        try:
            ET.parse(path)
            ok(path.relative_to(SRC_DIR))
        except ET.ParseError as e:
            err(f"XML-ошибка в {path.relative_to(SRC_DIR)}: {e}")
            all_ok = False
    return all_ok


def check_bsl_exports() -> bool:
    head("3. Проверка экспортируемых функций скоринга")
    module_path = SRC_DIR / "CommonModules/СкорингКонтрагентов/Ext/Module.bsl"
    if not module_path.exists():
        err("Модуль скоринга не найден")
        return False

    source = module_path.read_text(encoding="utf-8")
    all_ok = True
    for fn_name in BSL_EXPORTED_FUNCTIONS:
        if f"Функция {fn_name}" in source and "Экспорт" in source:
            ok(f"Экспорт: {fn_name}")
        else:
            err(f"Не найдена экспортируемая функция: {fn_name}")
            all_ok = False

    return all_ok


def check_scoring_factors() -> bool:
    head("4. Проверка факторов скоринга")
    module_path = SRC_DIR / "CommonModules/СкорингКонтрагентов/Ext/Module.bsl"
    if not module_path.exists():
        return False

    source = module_path.read_text(encoding="utf-8")
    all_ok = True
    for factor in SCORING_FACTORS:
        if f"Процедура {factor}" in source:
            ok(f"Фактор: {factor}")
        else:
            err(f"Фактор не найден: {factor}")
            all_ok = False

    return all_ok


def check_form_handlers() -> bool:
    head("5. Проверка обработчиков формы")
    form_module = (
        SRC_DIR
        / "DataProcessors/УмнаяДебиторка/Forms/ОсновнаяФорма/Ext/Module.bsl"
    )
    if not form_module.exists():
        err("Модуль формы не найден")
        return False

    source = form_module.read_text(encoding="utf-8")
    handlers = [
        "ПриСозданииНаСервере",
        "КомандаОбновить",
        "КомандаВыгрузитьExcel",
        "ОбновитьТаблицуСкоринга",
        "НастроитьУсловноеОформление",
    ]
    all_ok = True
    for h in handlers:
        if h in source:
            ok(f"Обработчик: {h}")
        else:
            warn(f"Обработчик не найден: {h}")
            all_ok = False

    return all_ok


def print_stats():
    head("Статистика проекта")
    total_lines = 0
    bsl_lines   = 0
    xml_lines   = 0

    for path in SRC_DIR.rglob("*"):
        if path.is_file():
            lines = len(path.read_text(encoding="utf-8").splitlines())
            total_lines += lines
            if path.suffix == ".bsl":
                bsl_lines += lines
            elif path.suffix == ".xml":
                xml_lines += lines

    print(f"  BSL (код 1С):    {bsl_lines:>5} строк")
    print(f"  XML (метаданные):{xml_lines:>5} строк")
    print(f"  Итого:           {total_lines:>5} строк")


def print_build_instructions():
    head("Инструкции по сборке расширения (.cfe)")
    print("""
  Способ 1 — через 1C:Designer (рекомендуется):
  ──────────────────────────────────────────────
  1. Откройте пустую базу в 1C:Enterprise 8.3 (режим Конфигуратор)
  2. Меню «Конфигурация» → «Расширения конфигурации»
  3. Кнопка «Создать» → задайте имя «УмнаяДебиторка»
  4. В открывшемся расширении: меню «Конфигурация» →
     «Загрузить конфигурацию из файлов» → укажите папку src/cf/
  5. После загрузки: меню «Конфигурация» →
     «Сохранить конфигурацию в файл» → сохранить как УмнаяДебиторка.cfe

  Способ 2 — через 1C:EDT (современный способ):
  ────────────────────────────────────────────────
  1. Установите 1C:EDT (https://edt.1c.ru/)
  2. Импортируйте проект: File → Import → 1C Extension project
  3. Укажите папку src/cf/
  4. Соберите расширение: Project → Build → Export .cfe

  Способ 3 — через ring (CI/CD):
  ────────────────────────────────────────────────
  # ring.exe designer /DisableStartupDialogs /N admin /P ""
  #   /LoadConfigFromFiles src/cf /Extension ext_name /SaveCfg output.cfe

  После сборки — тестирование:
  ────────────────────────────────────────────────
  1. Откройте рабочую базу (УТ 11, КА 2, Бухгалтерия 3.x) → Конфигуратор
  2. Конфигурация → Расширения конфигурации → Добавить
  3. Выберите файл УмнаяДебиторка.cfe
  4. Запустите предприятие → Меню «Все функции» → Обработки → «Умная Дебиторка»
""")


# ─── Точка входа ──────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    print(f"\n{BOLD}{'='*55}{RESET}")
    print(f"{BOLD}  Умная Дебиторка — Проверка исходников расширения 1С{RESET}")
    print(f"{BOLD}{'='*55}{RESET}")

    if "--stats" in args:
        print_stats()
        return

    results = [
        check_structure(),
        check_xml_valid(),
        check_bsl_exports(),
        check_scoring_factors(),
        check_form_handlers(),
    ]

    print_stats()
    print_build_instructions()

    head("Итог")
    if all(results):
        print(f"  {GREEN}{BOLD}Все проверки пройдены — расширение готово к сборке!{RESET}")
        sys.exit(0)
    else:
        failed = results.count(False)
        print(f"  {RED}{BOLD}Найдено проблем: {failed}{RESET}")
        sys.exit(1)


if __name__ == "__main__":
    main()
