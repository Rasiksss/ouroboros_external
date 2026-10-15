#!/usr/bin/env python3
"""
Добавляет в GT-файл Бенчмарки/GT_нефинансовые_24_компании.xlsx лист «Синонимы»
с русскоязычными синонимами каждого MetricEnum, собранными из JSON-разметки
(tests/Нефинансовые JSON/*_structured_output.json).

На выходе — тот же файл GT, но с двумя новыми листами:
  - «Синонимы» — MetricEnum | Синоним (русский) | Источник (JSON-файл)
                 Плюс маркер «неоднозначный» (если синоним встречается в >1 enum).
  - «Сводка по синонимам» — MetricEnum | Кол-во уникальных синонимов
"""
import json
from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

ROOT = Path(__file__).resolve().parent.parent
GT_XLSX = ROOT / "Бенчмарки " / "GT_нефинансовые_24_компании.xlsx"
JSON_DIR = ROOT / "tests" / "Нефинансовые JSON"


def collect_synonyms():
    """Returns:
      enum_to_names: dict[enum, set[(name, source_file)]]
      name_to_enums: dict[name.lower(), set[enum]]  (for ambiguity detection)
    """
    enum_to_names: dict[str, set] = defaultdict(set)
    name_to_enums: dict[str, set] = defaultdict(set)

    for jf in sorted(JSON_DIR.glob("*_structured_output*.json")):
        with open(jf, encoding="utf-8") as f:
            data = json.load(f)
        for t in data.get("tables", []):
            for p in t.get("parameters", []):
                enum = (p.get("parameter_enum") or "").strip()
                name = (p.get("parameter_name") or "").strip()
                if not (enum and name):
                    continue
                # отбрасываем служебные строки типа "Выручка--Прим."
                if "--" in name:
                    continue
                enum_to_names[enum].add((name, jf.name))
                name_to_enums[name.lower()].add(enum)

    return enum_to_names, name_to_enums


def main():
    print(f"Загружаю JSON-разметку из {JSON_DIR}...")
    enum_to_names, name_to_enums = collect_synonyms()
    total_synonyms = sum(len(v) for v in enum_to_names.values())
    distinct_names = sum(
        len({name for name, _ in pairs}) for pairs in enum_to_names.values()
    )
    print(f"  Найдено {len(enum_to_names)} enum-ов, "
          f"{distinct_names} уникальных синонимов "
          f"({total_synonyms} вхождений)")

    ambiguous = {n for n, es in name_to_enums.items() if len(es) > 1}
    print(f"  Неоднозначных (в >1 enum): {len(ambiguous)}")

    print(f"\nОткрываю GT-файл: {GT_XLSX}")
    wb = openpyxl.load_workbook(GT_XLSX)

    # Удаляем прежние листы синонимов, если они были
    for old in ("Синонимы", "Сводка по синонимам"):
        if old in wb.sheetnames:
            del wb[old]

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_align = Alignment(horizontal="center", wrap_text=True)
    ambig_fill = PatternFill("solid", fgColor="FFEB9C")  # жёлтый для неоднозначных

    # ============ Лист «Синонимы» ============
    ws = wb.create_sheet("Синонимы")
    ws.append(["MetricEnum", "Синоним (ru)", "Источник (JSON)", "Неоднозначный"])
    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    # Сортировка: сначала по enum, потом по имени
    for enum in sorted(enum_to_names.keys()):
        # дедуп по (name.lower(), source) для читаемости
        seen_name: dict[str, list[str]] = defaultdict(list)
        for name, src in enum_to_names[enum]:
            seen_name[name].append(src)
        for name in sorted(seen_name.keys()):
            sources = sorted(set(seen_name[name]))
            src_str = "; ".join(sources)
            is_ambig = name.lower() in ambiguous
            ws.append([enum, name, src_str, "да" if is_ambig else ""])
            if is_ambig:
                for cell in ws[ws.max_row]:
                    cell.fill = ambig_fill

    widths = [42, 60, 60, 16]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[1].height = 28
    ws.freeze_panes = "A2"

    # ============ Лист «Сводка по синонимам» ============
    ws2 = wb.create_sheet("Сводка по синонимам")
    ws2.append(["MetricEnum", "Кол-во уникальных синонимов",
                 "Неоднозначных", "Все синонимы (через ; )"])
    for cell in ws2[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align

    for enum in sorted(enum_to_names.keys()):
        names = sorted({name for name, _ in enum_to_names[enum]})
        n_ambig = sum(1 for n in names if n.lower() in ambiguous)
        ws2.append([enum, len(names), n_ambig, "; ".join(names)])

    widths2 = [42, 14, 14, 120]
    for i, w in enumerate(widths2, start=1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    ws2.row_dimensions[1].height = 28
    ws2.freeze_panes = "A2"

    wb.save(GT_XLSX)
    print(f"\n✅ GT-файл обновлён: {GT_XLSX}")
    print(f"   Лист «Синонимы» — построчный список")
    print(f"   Лист «Сводка по синонимам» — агрегат по enum")


if __name__ == "__main__":
    main()
