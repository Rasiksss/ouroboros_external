import type { FormattedTable } from "./api";
import { formatDisplayNumber } from "./display-number";

/**
 * Раскладка роли: чтение ячейки, формат числа, поиск, разрез ДИЭ, гейт смены.
 *
 * 🔴 ПОЧЕМУ ОТДЕЛЬНЫМ МОДУЛЕМ. Одну и ту же таблицу рисовали ДВА места по
 * РАЗНЫМ правилам, и второе было неверным. На «/» ячейка ищется сначала на
 * верхнем уровне строки, потом в `values` (так её кладёт бэкенд: `cib` и
 * `baifp` пишут `label/period/value/unit` полями строки, а `values` оставляют
 * пустым). В рабочей области стояло `ci === 0 ? r.label : r.values[key]` — и у
 * CIB с БАИФП все колонки кроме первой были ПУСТЫМИ, у `risks` пустой была и
 * первая (подпись строки лежит в `label_en`). Правильно рисовался только
 * `dgr`, у которого значения и правда в `values`.
 *
 * Поэтому правило разрешения ячейки, формат числа и обрезка пустых колонок
 * живут здесь ОДНИМ экземпляром. Второй реализации быть не должно: разойдутся
 * они опять молча — таблица не падает, она просто врёт пустотой.
 */

export type FormattedColumn = FormattedTable["columns"][number];
export type FormattedRow = FormattedTable["rows"][number];

/**
 * Значение ячейки: сначала поле строки, потом `values`.
 *
 * Порядок именно такой (как в `formattedCellValue` на «/»): бэкенд кладёт
 * значения по-разному в зависимости от формата роли, и `values` — не общий
 * контракт, а один из двух способов.
 */
export function formattedCellValue(row: FormattedRow, column: FormattedColumn): unknown {
  const key = column.key;
  if (Object.prototype.hasOwnProperty.call(row, key)) return row[key];
  return row.values?.[key];
}

function isTrailingSpacerColumn(column: FormattedColumn): boolean {
  const key = String(column.key ?? "");
  const label = String(column.label ?? "").trim();
  return !label && (key.startsWith("blank") || key.startsWith("spacer:"));
}

/** Хвостовые распорки (`blank1`, `spacer:…`) — это отступы для Excel, а не
 *  колонки. На экране они рисовались настоящими пустыми столбцами. */
export function trimTrailingSpacerColumns(columns: FormattedColumn[]): FormattedColumn[] {
  let end = columns.length;
  while (end > 0 && isTrailingSpacerColumn(columns[end - 1])) end -= 1;
  return end === columns.length ? columns : columns.slice(0, end);
}

/** Колонки, которые действительно надо нарисовать. */
export function visibleFormattedColumns(table: FormattedTable): FormattedColumn[] {
  return trimTrailingSpacerColumns(
    (table.columns ?? []).filter(
      (column) =>
        !column.hidden
        // У `risks` подпись строки лежит в `label_en`, а `label_ru` и `blank1`
        // — служебные колонки excel-выгрузки (порядок тот же, что на «/»).
        && !(table.format === "risks" && column.key === "label_ru")
        && !(table.format === "risks" && column.key === "blank1"),
    ),
  );
}

/**
 * Текст ячейки. Проценты бэкенд отдаёт долей (0.12), и без этого перевода в
 * таблице стояло «0.12» там, где человек ждёт «12%».
 */
export function fmtFormattedCell(
  value: unknown,
  column: FormattedColumn,
  row: FormattedRow,
): string {
  if (value === null || value === undefined || value === "") return "";
  if (typeof value === "number") {
    const isPercent = column.kind === "percent" || row.is_percent || row.value_kind === "percent";
    if (isPercent) return `${formatDisplayNumber(value * 100, 2)}%`;
    return formatDisplayNumber(value, 2);
  }
  return String(value);
}

/** Колонка числовая — значит выравнивание вправо и табличные цифры. */
export function isFormattedNumeric(column: FormattedColumn): boolean {
  return column.kind === "value" || column.kind === "percent" || column.kind === "number";
}

function formattedSearchValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return "";
  return String(value).toLowerCase();
}

function formattedRowMatchesQuery(
  row: FormattedRow,
  columns: FormattedColumn[],
  query: string,
): boolean {
  const direct: unknown[] = [
    row.enum,
    row.label,
    row.label_en,
    row.label_ru,
    row.name,
    row.name_en,
    row.name_ru,
    row.period,
    row.unit,
    row.source,
    row.source_note,
  ];
  if (direct.some((value) => formattedSearchValue(value).includes(query))) return true;
  return columns.some((column) =>
    formattedSearchValue(formattedCellValue(row, column)).includes(query),
  );
}

/**
 * Поиск по роль-таблице. Заголовок раздела остаётся, если под ним что-то
 * нашлось, — иначе строки повисают без раздела и читаются не тем показателем.
 */
export function filterFormattedTable(table: FormattedTable, query: string): FormattedTable {
  const q = query.toLowerCase().trim();
  if (!q) return table;

  const columns = visibleFormattedColumns(table);
  const filteredRows: FormattedTable["rows"] = [];
  let pendingSection: FormattedTable["rows"] = [];

  for (const row of table.rows ?? []) {
    if (row.kind === "section") {
      pendingSection = [row];
      continue;
    }
    if (row.kind === "spacer") continue;
    if (!formattedRowMatchesQuery(row, columns, q)) continue;
    if (pendingSection.length > 0) {
      filteredRows.push(...pendingSection);
      pendingSection = [];
    }
    filteredRows.push(row);
  }

  return { ...table, rows: filteredRows };
}

/** Строки-данные (без заголовков разделов и распорок). */
export function formattedDataRowCount(table: FormattedTable): number {
  return (table.rows ?? []).filter((row) => row.kind !== "section" && row.kind !== "spacer").length;
}

export function isDieFormattedTable(table: FormattedTable | null | undefined): boolean {
  return table?.format === "die_nf" || table?.format === "die_bank";
}

export function firstDieSegmentRowIndex(table: FormattedTable): number {
  return (table.rows ?? []).findIndex((row) => row.die_kind === "segment_section");
}

/**
 * ДИЭ приезжает ОДНОЙ таблицей: сверху значения, ниже разбивка по сегментам.
 * Вкладки «Показатели» и «Сегменты» обязаны показывать свою половину, иначе
 * на обеих стоит одно и то же полотно.
 */
export function dieValuesTable(table: FormattedTable): FormattedTable {
  if (!isDieFormattedTable(table)) return table;
  const segmentStart = firstDieSegmentRowIndex(table);
  if (segmentStart < 0) return table;
  const rows = (table.rows ?? []).slice(0, segmentStart);
  while (rows.length > 0 && rows[rows.length - 1]?.kind === "spacer") rows.pop();
  return { ...table, rows };
}

export function dieSegmentsTable(table: FormattedTable): FormattedTable | null {
  if (!isDieFormattedTable(table)) return null;
  const segmentStart = firstDieSegmentRowIndex(table);
  if (segmentStart < 0) return null;
  return { ...table, rows: (table.rows ?? []).slice(segmentStart) };
}

/** Роли, для которых раскладки нет по определению: это и есть обычная таблица. */
export function isGenericTableRole(role: string): boolean {
  return role === "other" || role === "all";
}

/**
 * 🔴 ПЕРЕСТРАИВАТЬ МОЖНО ТОЛЬКО С ОБЩЕЙ РОЛИ (#142, уточнение владельца 15.08).
 *
 * Смена роли — это ПЕРЕСОРТИРОВКА уже прочитанного, а не новое чтение отчёта.
 * Прогон в узкой роли содержит только её показатели: построить из него «CIB»
 * технически можно, но половина клеток будет пустой, и отличить «в отчёте
 * нет» от «мы этого не извлекали» станет нечем.
 */
export function canReshapeTableRole(extractionRole: string): boolean {
  return isGenericTableRole(extractionRole);
}

/** Причина, по которой роль не переключается. Текст один на оба экрана —
 *  раньше он жил только в карточке на «/», и рабочая область молчала. */
export function reshapeBlockedReason(extractionRoleLabel: string): string {
  return (
    `Извлечение шло по роли «${extractionRoleLabel}» — в таблице только её показатели. `
    + "Чтобы переключать роли, нужен прогон в общей роли: она читает полный набор ячейки."
  );
}

/**
 * Ручка ответила 200 и `formatted: null` — это НЕ ошибка сети и НЕ пустой
 * отчёт. Для трёх пар роль×отчётность (Риски×банк, БАИФП×нефин., ДГР×нефин.)
 * раскладки в бэкенде нет вовсе, и он честно отдаёт `role_format=unsupported`.
 * Без этой надписи пользователь видел общую таблицу под подписью «ДГР» и
 * решал, что роль не работает.
 */
export function roleLayoutMissingNote(
  roleLabel: string,
  roleFormat: string,
  hadTable = false,
): string {
  if (roleFormat === "unsupported") {
    return `Раскладки роли «${roleLabel}» для этой отчётности нет — показываем общую таблицу.`;
  }
  if (hadTable) {
    return `Раскладка роли «${roleLabel}» по этому прогону пустая — показываем общую таблицу.`;
  }
  return `Раскладку роли «${roleLabel}» бэкенд не собрал — показываем общую таблицу.`;
}

/**
 * Сузить строки до набора роли. Пустой список = «фильтра нет», а НЕ «показывать
 * нечего»: у роли без раскладки бэкенд шлёт именно пустой массив, и без этой
 * проверки таблица и счётчики обнулились бы (та же мина, что закрыли на «/»).
 */
export function filterRowsByOutputEnums<T extends { enum?: string }>(
  rows: T[],
  role: string,
  outputEnums: string[] | null | undefined,
): T[] {
  if (isGenericTableRole(role) || !outputEnums?.length) return rows;
  const allowed = new Set(
    outputEnums.map((id) => String(id).trim().toUpperCase()).filter(Boolean),
  );
  return rows.filter((row) => allowed.has(String(row.enum ?? "").trim().toUpperCase()));
}
