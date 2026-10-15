"use client";

import { Fragment, useMemo, useState } from "react";
import type { SourceSheet, SourceSheetCell, SourceSheetCellValue } from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * Лист Excel-артефакта: строка адреса ячейки и строка формулы сверху, буквы
 * колонок, номера строк, вкладки листов снизу, выделение ячейки и прокрутка.
 *
 * Компонент только рисует — данные и смену листа отдаёт наружу: лист приходит
 * с бэкенда отдельным запросом (?sheet=...), поэтому переключение вкладки не
 * может быть локальным состоянием.
 *
 * ПРАВКИ ВРУЧНУЮ НЕ ВКЛЮЧЕНЫ (решение владельца): значения меняет агент по
 * просьбе в чате, поэтому ячейка выделяется, но не редактируется.
 */

/** A, B, ... Z, AA, AB — как нумерует колонки Excel. */
function colName(n: number): string {
  let s = "";
  while (n > 0) {
    const m = (n - 1) % 26;
    s = String.fromCharCode(65 + m) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

/** Ширина Excel считается в символах — переводим в пиксели по его же формуле. */
function pxWidth(chars: number | undefined): number {
  if (!chars || chars <= 0) return 96;
  return Math.min(320, Math.max(56, Math.round(chars * 7 + 10)));
}

/** В сетке число показываем с разделителями, в строке формул — как есть. */
function display(v: SourceSheetCellValue | undefined): string {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") {
    return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 6 }).format(v);
  }
  if (typeof v === "boolean") return v ? "ИСТИНА" : "ЛОЖЬ";
  return v;
}

const ROW_GUTTER = 46;

export function ExcelGrid({
  data,
  onPickSheet,
}: {
  data: SourceSheet;
  onPickSheet: (sheet: string) => void;
}) {
  const [sel, setSel] = useState<{ r: number; c: number }>({ r: 1, c: 1 });

  // Ответ разрежённый, а рисуем плотную сетку — нужен доступ по координате.
  const byKey = useMemo(() => {
    const m = new Map<string, SourceSheetCell>();
    for (const cell of data.cells) m.set(`${cell.r}:${cell.c}`, cell);
    return m;
  }, [data.cells]);

  // n_rows/n_cols — настоящий размер листа, а cells могут покрывать меньше
  // (кап ручки; у Excel max_row раздувается пустым форматированием). Рисуем
  // ровно то, что пришло: иначе сетка ушла бы в тысячи пустых строк, и было
  // бы непонятно, лист пустой или данные не доехали.
  const covered = useMemo(() => {
    let r = 0;
    let c = 0;
    for (const cell of data.cells) {
      if (cell.r > r) r = cell.r;
      if (cell.c > c) c = cell.c;
    }
    return { r, c };
  }, [data.cells]);

  const cols = useMemo(
    () =>
      Array.from(
        { length: Math.max(1, Math.min(data.n_cols || 1, covered.c || 1)) },
        (_, i) => i + 1,
      ),
    [data.n_cols, covered.c],
  );
  const rows = useMemo(
    () =>
      Array.from(
        { length: Math.max(1, Math.min(data.n_rows || 1, covered.r || 1)) },
        (_, i) => i + 1,
      ),
    [data.n_rows, covered.r],
  );

  const template = useMemo(
    () =>
      `${ROW_GUTTER}px ` +
      cols.map((c) => `${pxWidth(data.col_widths?.[c - 1])}px`).join(" "),
    [cols, data.col_widths],
  );

  const selCell = byKey.get(`${sel.r}:${sel.c}`);
  const selRef = `${colName(sel.c)}${sel.r}`;
  const selText =
    selCell?.f ?? (selCell && selCell.v !== null ? String(selCell.v) : "");

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Строка адреса ячейки + строка формулы. */}
      <div className="flex shrink-0 items-stretch border border-[var(--color-divider)] border-b-0 text-[11.5px]">
        <span className="min-w-[52px] shrink-0 border-r border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1.5 text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
          {selRef}
        </span>
        <span className="shrink-0 border-r border-[var(--color-divider)] px-2 py-1.5 italic text-[var(--color-neutral-500)]">
          fx
        </span>
        <span
          className="min-w-0 flex-1 truncate px-2 py-1.5 [font-variant-numeric:tabular-nums]"
          title={selText}
        >
          {selText}
        </span>
      </div>

      {/* Подсказка про способ правки — не декоративная, объясняет, почему
          ячейка не редактируется. */}
      <div className="flex shrink-0 items-baseline gap-2 border-x border-[var(--color-divider)] bg-[var(--color-accent-100)] px-2 py-1.5 text-[11px] text-[var(--color-accent-800)]">
        <span className="uppercase tracking-[0.12em]">правка через агента</span>
        <span className="min-w-0 flex-1 text-[var(--color-accent-700)]">
          Вручную ячейки не редактируются. Напишите агенту в чат, например:
          «в листе „{data.sheet}“ поставь в {selRef} …» — он перезапишет файл.
        </span>
      </div>

      {/* Ответ разрежённый: ни одной ячейки — лист действительно пуст. Без этой
          строки пустая сетка читалась бы как «данные не доехали». */}
      {data.cells.length === 0 && (
        <p className="shrink-0 border-x border-[var(--color-divider)] px-2 py-1.5 text-[11.5px] text-[var(--color-neutral-500)]">
          Лист «{data.sheet}» пуст — ячеек со значениями в нём нет.
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-auto border border-[var(--color-divider)] text-[11.5px]">
        <div className="grid w-max" style={{ gridTemplateColumns: template }}>
          {/* Уголок — липнет и по вертикали, и по горизонтали, поэтому выше всех. */}
          <div className="sticky left-0 top-0 z-30 border-b border-r border-[var(--color-divider)] bg-[var(--color-surface)]" />
          {cols.map((c) => (
            <div
              key={`h${c}`}
              className={cn(
                "sticky top-0 z-20 border-b border-r border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1 text-center text-[10px] uppercase tracking-[0.1em]",
                c === sel.c
                  ? "text-[var(--color-accent-700)]"
                  : "text-[var(--color-neutral-600)]",
              )}
            >
              {colName(c)}
            </div>
          ))}

          {rows.map((r) => (
            <Fragment key={`r${r}`}>
              <div
                className={cn(
                  "sticky left-0 z-10 border-b border-r border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1 text-right text-[10px] [font-variant-numeric:tabular-nums]",
                  r === sel.r
                    ? "text-[var(--color-accent-700)]"
                    : "text-[var(--color-neutral-500)]",
                )}
              >
                {r}
              </div>
              {cols.map((c) => {
                const cell = byKey.get(`${r}:${c}`);
                const selected = sel.r === r && sel.c === c;
                const numeric = cell?.t === "n" || typeof cell?.v === "number";
                // v === null у формульной ячейки значит, что книга не хранит
                // посчитанного значения (так пишет openpyxl). Показываем саму
                // формулу серым — иначе на месте живой цифры была бы пустота,
                // а придумывать результат за Excel нельзя.
                const raw = cell?.v ?? null;
                const uncomputed = raw === null && !!cell?.f;
                return (
                  <button
                    key={`c${r}:${c}`}
                    type="button"
                    onClick={() => setSel({ r, c })}
                    title={cell?.f ?? undefined}
                    className={cn(
                      "min-w-0 cursor-cell truncate border-b border-r border-[var(--color-divider)] px-2 py-1 text-left [font-variant-numeric:tabular-nums]",
                      numeric && "text-right",
                      cell?.b && "font-medium",
                      uncomputed && "text-[var(--color-neutral-500)]",
                      selected
                        ? "bg-[var(--color-accent-100)] shadow-[inset_0_0_0_1px_var(--color-accent)]"
                        : "hover:bg-[var(--color-neutral-100)]",
                    )}
                  >
                    {uncomputed ? cell?.f : display(raw)}
                  </button>
                );
              })}
            </Fragment>
          ))}
        </div>
      </div>

      {/* Вкладки листов снизу — как в Excel. */}
      <div className="flex shrink-0 items-stretch border border-t-0 border-[var(--color-divider)] bg-[var(--color-surface)]">
        <div className="flex min-w-0 flex-1 overflow-x-auto [scrollbar-width:none]">
          {data.sheets.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => {
                if (name === data.sheet) return;
                setSel({ r: 1, c: 1 });
                onPickSheet(name);
              }}
              className={cn(
                "shrink-0 border-r border-t-2 border-r-[var(--color-divider)] px-3 py-1.5 text-[11px] transition-colors",
                name === data.sheet
                  ? "border-t-[var(--color-accent)] bg-[var(--color-bg)] text-[var(--color-text)]"
                  : "border-t-transparent text-[var(--color-neutral-600)] hover:bg-[var(--color-accent-100)]",
              )}
            >
              {name}
            </button>
          ))}
        </div>
        <span className="flex shrink-0 items-center px-2 text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
          {rows.length} × {cols.length}
          {data.truncated ? " · обрезано" : ""}
        </span>
      </div>

      {data.truncated && (
        <p className="shrink-0 pt-1.5 text-[10.5px] italic text-[var(--color-neutral-500)]">
          Показаны первые {rows.length} строк и {cols.length} колонок; на листе
          их {data.n_rows} и {data.n_cols}. Остальное — в скачанном файле.
        </p>
      )}
    </div>
  );
}
