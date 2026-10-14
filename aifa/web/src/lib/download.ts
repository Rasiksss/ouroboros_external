import type { AnalysisCardKpi } from "@/lib/api";

const DIRECT_API =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const KPI_FLAG_LABELS: Record<AnalysisCardKpi["flag"], string> = {
  good: "позитивный",
  ok: "норма",
  warn: "внимание",
  risk: "риск",
  crit: "критический",
  na: "нет данных",
};

/** Свести матрицу ячеек в TSV — формат, который Excel вставляет по колонкам
 * без диалога импорта. Внутри ячейки табуляций и переводов строк быть не
 * может: они сдвинули бы всю сетку, поэтому любой пробельный набор схлопываем
 * в один пробел. Единственный сериализатор на всё приложение — второй разошёлся
 * бы с первым по экранированию. */
export function rowsToTSV(rows: (string | null | undefined)[][]): string {
  return rows
    .map((r) => r.map((c) => String(c ?? "").replace(/\s+/g, " ").trim()).join("\t"))
    .join("\n");
}

/** Serialise analysis-card KPIs to a TSV block for pasting into Excel:
 * a header row plus one tab-separated row per metric. */
export function kpiToTSV(kpi: AnalysisCardKpi[]): string {
  return rowsToTSV([
    ["Показатель", "Значение", "Предыдущий", "Δ", "Статус"],
    ...kpi.map((k) => [k.name, k.value, k.prev, k.delta, KPI_FLAG_LABELS[k.flag] ?? k.flag]),
  ]);
}

/** Минимум, который нужен от таблицы, чтобы снять с неё TSV. Именно структурный
 * тип, а не HTMLTableElement: так функция берёт и живой DOM, и обычный объект —
 * иначе её нельзя проверить тестом без браузера. */
export type TableLike = {
  rows: ArrayLike<{ cells: ArrayLike<{ textContent: string | null }> }>;
};

/** Снять TSV с УЖЕ ОТРИСОВАННОЙ таблицы. Разбирать исходный markdown второй
 * раз мы не хотим: пользователь копирует то, что видит, а видит он результат
 * работы remark-gfm (объединённые ячейки, экранирование, инлайновая разметка). */
export function tableToTSV(table: TableLike): string {
  const rows: string[][] = [];
  for (let i = 0; i < table.rows.length; i++) {
    const tr = table.rows[i];
    const cells: string[] = [];
    for (let j = 0; j < tr.cells.length; j++) cells.push(tr.cells[j].textContent ?? "");
    rows.push(cells);
  }
  return rowsToTSV(rows);
}

export function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** A GFM table = a header row plus a |---|---| separator row. */
export function hasTable(md: string): boolean {
  return /^\s*\|.*\|\s*$/m.test(md) && /^\s*\|[\s|:-]+\|\s*$/m.test(md);
}

/** First markdown table in `markdown` → real .xlsx via the backend (openpyxl). */
export async function downloadTableXlsx(
  markdown: string,
  filenameBase: string,
): Promise<void> {
  const res = await fetch(`${DIRECT_API}/export/table.xlsx`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown, filename: filenameBase }),
  });
  if (!res.ok) throw new Error(`${res.status}`);
  triggerDownload(await res.blob(), `${filenameBase}.xlsx`);
}
