"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Download, Search } from "lucide-react";
import { toast } from "sonner";
import { downloadMetricsXlsx, type MetricValue } from "@/lib/api";
import { metricIdentityKey, type MetricRow } from "@/lib/projects";
import { useWorkspace } from "../workspace-context";
import { cn } from "@/lib/utils";

/**
 * Таблица извлечённого: поиск по строкам, фильтр по «оси» строки (форма
 * отчёта / срез сегмента / вид связи в холдинге) и колонка «Источник».
 * Клик по строке уводит панель документа на страницу-источник через
 * gotoSource — сама таблица про панель документа ничего не знает.
 */

export type ExtractedVariant = "metrics" | "segments" | "group";

/** Колонки задаются сеткой: шапка и строки должны делить одну раскладку. */
// Первая колонка (название показателя) НЕ ужимается ниже 200px: при узкой
// рабочей области она раньше схлопывалась в многоточие, и таблица становилась
// нечитаемой. Теперь вместо сжатия таблица уезжает вбок — прокручивается
// двумя пальцами, название всегда можно долистать целиком.
const GRID: Record<ExtractedVariant, string> = {
  metrics: "minmax(200px,1fr) 88px 88px 66px 108px",
  segments: "minmax(200px,1fr) minmax(160px,1fr) 88px 88px 108px",
  group: "minmax(200px,1fr) 66px minmax(160px,1fr) 104px 108px",
};

/** Минимальная ширина таблицы = сумма колонок + зазоры + поля. Ниже неё
 *  включается горизонтальная прокрутка вместо сжатия. */
const MIN_W: Record<ExtractedVariant, string> = {
  metrics: "620px",
  segments: "680px",
  group: "700px",
};

const DIMENSION_RU: Record<string, string> = {
  operating_segment: "операционный сегмент",
  "operating segment": "операционный сегмент",
  geography: "география",
  geographic: "география",
  geographical: "география",
  region: "география",
  product: "продукт",
  other: "прочее",
};

export function ExtractedTable({
  rows,
  variant,
  unit,
  periodCurrent,
  periodPrev,
  company,
  query: queryOuter,
}: {
  rows: MetricRow[];
  variant: ExtractedVariant;
  /** Единица измерения прогона (млн ₽ и т.п.) — подставляется, если у строки своей нет. */
  unit?: string;
  periodCurrent?: string;
  periodPrev?: string;
  /** Имя компании прогона — для подписи книги Excel. У строк его нет: оно
   *  живёт в паспорте прогона, а не в каждой строке. */
  company?: string;
  /** Поиск ИЗВНЕ (правка Nate 17.08). Поле переехало в шапку зоны — на место,
   *  которое освободила выпадашка документа. Своё поле остаётся для случаев,
   *  когда таблицу показывают без шапки зоны. */
  query?: string;
}) {
  const { gotoSource } = useWorkspace();
  const [queryOwn, setQuery] = useState("");
  const query = queryOuter ?? queryOwn;
  const [facet, setFacet] = useState("");

  // Значения фильтра берём из самих строк — придуманных вариантов в списке нет.
  const facets = useMemo(() => {
    const seen = new Set<string>();
    for (const r of rows) {
      const f = facetOf(r, variant);
      if (f) seen.add(f);
    }
    return Array.from(seen).sort((a, b) => a.localeCompare(b, "ru"));
  }, [rows, variant]);

  // Сменился документ или вкладка — прежнее значение фильтра может отсутствовать
  // в новом наборе; тогда считаем, что фильтр снят (сбрасывать состоянием не надо).
  const activeFacet = facet && facets.includes(facet) ? facet : "";

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return rows.filter((r) => {
      if (activeFacet && facetOf(r, variant) !== activeFacet) return false;
      if (!q) return true;
      return searchHaystack(r).includes(q);
    });
  }, [rows, variant, activeFacet, query]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className={cn("flex shrink-0 items-center gap-3 px-4", queryOuter === undefined ? "py-2" : "py-0")}>
        {queryOuter === undefined && (
          <div className="flex min-w-0 flex-1 items-center gap-2 border-b border-[var(--color-divider)] py-1 focus-within:border-[var(--color-accent)]">
            <Search className="size-3.5 shrink-0 text-[var(--color-neutral-500)]" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Поиск по строкам"
              className="min-w-0 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-[var(--color-neutral-500)]"
            />
          </div>
        )}
        {/* Фильтр показываем, только когда есть из чего выбирать. */}
        {facets.length > 1 && (
          <PickList
            className="w-[172px] shrink-0"
            value={activeFacet}
            onPick={setFacet}
            options={[
              { id: "", label: facetAllLabel(variant) },
              ...facets.map((f) => ({ id: f, label: facetLabel(f, variant) })),
            ]}
          />
        )}
      </div>

      {/* Шапка и строки живут в ОДНОМ контейнере прокрутки: иначе при сдвиге
          вбок заголовки уезжали бы отдельно от данных. */}
      <div className="min-h-0 flex-1 overflow-auto">
        <ExtractedGrid
          rows={shown}
          totalRows={rows.length}
          variant={variant}
          unit={unit}
          periodCurrent={periodCurrent}
          periodPrev={periodPrev}
          onOpenSource={gotoSource}
        />
      </div>

      {/* ВЫГРУЗКА ПОД ТАБЛИЦЕЙ (правка Nate 18.08). Excel и раньше приезжал —
          артефактом хода, отдельной вкладкой. Но человек, который смотрит на
          таблицу, хочет забрать ИМЕННО ЭТУ таблицу, и искать её в артефактах
          не должен. Уходит то, что на экране: фильтр и поиск учтены. */}
      {shown.length > 0 && (
        <div className="flex shrink-0 justify-end border-t border-[var(--color-divider)] px-4 py-1.5">
          <XlsxButton
            rows={shown}
            company={company || ""}
            unit={unit}
            periodCurrent={periodCurrent}
            periodPrev={periodPrev}
          />
        </div>
      )}
    </div>
  );
}

function XlsxButton({
  rows,
  company,
  unit,
  periodCurrent,
  periodPrev,
}: {
  rows: MetricRow[];
  company: string;
  unit?: string;
  periodCurrent?: string;
  periodPrev?: string;
}) {
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      await downloadMetricsXlsx({
        company,
        period_current: periodCurrent || "",
        period_prev: periodPrev || "",
        unit: unit || "",
        filename: [company, periodCurrent].filter(Boolean).join(" ") || "показатели",
        rows: rows.map((r) => ({
          enum: r.enum,
          name_ru: r.name_ru,
          segment: r.segment || "",
          dimension: r.dimension || "",
          value_current: r.value_current,
          value_prev: r.value_prev,
          period_values: r.period_values ?? [],
          unit: r.unit || "",
          page: r.page ?? null,
          source_form: r.source_form || "",
          is_calculated: r.is_calculated,
          formula: r.formula || "",
        })),
      });
    } catch (e) {
      // Отказ выгрузки обязан быть слышен: молчащая кнопка читается как
      // «ничего не произошло», и человек жмёт её снова и снова.
      toast.error(`Не удалось собрать Excel: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <button
      type="button"
      onClick={() => void save()}
      disabled={busy}
      title={`Скачать таблицу в Excel — ${rows.length} строк`}
      className="flex items-center gap-1.5 text-[11.5px] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)] disabled:opacity-50"
    >
      <Download className="size-3.5" />
      {busy ? "собираю…" : "Excel"}
    </button>
  );
}

/**
 * Сетка показателей БЕЗ обвязки панели: шапка и строки, и ничего больше.
 *
 * ЗАЧЕМ ОТДЕЛЬНО (правка Nate 17.08). Одни и те же числа показывались в двух
 * видах: в рабочей области — этой сеткой, в чате — markdown-таблицей от модели.
 * Разные колонки, разное выравнивание, разное форматирование чисел; вопрос
 * «у нас одинакового формата таблица там и там?» имел ответ «нет».
 *
 * Целиком `ExtractedTable` в чат не годится: у него свой поиск, фильтр,
 * растяжка на высоту панели и строгий `useWorkspace()`, который вне провайдера
 * бросает исключение. Поэтому общая часть — сетка, а поиск, фильтры и
 * прокрутка остаются снаружи, у того, кто её показывает.
 *
 * `onOpenSource` приходит СНАРУЖИ, а не из контекста: в чате переход к
 * странице-источнику может быть недоступен, и сетка обязана рисоваться
 * без него, а не падать.
 */
export function ExtractedGrid({
  rows,
  totalRows,
  variant,
  unit,
  periodCurrent,
  periodPrev,
  onOpenSource,
}: {
  rows: MetricRow[];
  /** Сколько строк было ДО фильтра — только для честного текста пустоты. */
  totalRows?: number;
  variant: ExtractedVariant;
  unit?: string;
  periodCurrent?: string;
  periodPrev?: string;
  onOpenSource?: (t: { docId: string; page: number; label: string }) => void;
}) {
  // 🔴 ШАПКА НЕ СМЕЕТ НАЗЫВАТЬ ОДИН ПЕРИОД, КОГДА ИХ ДВА (#205, правка 19.08).
  // У промежуточного отчёта баланс сравнивается с концом прошлого года, а ОПУ —
  // с тем же кварталом год назад. Конкретная подпись верна для одной половины
  // строк и лжёт про другую, причём правдоподобно: пользователь читает цифры
  // баланса как «первый квартал прошлого года». Строки расходятся — колонка
  // называется общо, а точный период каждой строки виден подсказкой в ячейке.
  const prevHead = useMemo(() => comparativeHeader(rows, periodPrev), [rows, periodPrev]);
  const heads = headers(variant, periodCurrent, prevHead);
  const cols = GRID[variant];
  const shown = rows;
  const total = totalRows ?? rows.length;
  // Словарь для подсказки у «расчёта»: коды реестра → русские имена. Строится
  // из ЭТИХ же строк, поэтому фильтр в панели его обедняет — это осознанно:
  // держать полный реестр на клиенте значило бы завести второе место правды.
  const names = useMemo(() => metricNames(rows), [rows]);

  return (
    <div style={{ minWidth: MIN_W[variant] }}>
      {/* Шапка липкая — при вертикальной прокрутке остаётся на месте. */}
      <div
        className="sticky top-0 z-10 grid shrink-0 gap-3 border-b border-[var(--color-divider)] bg-[var(--color-surface)] px-4 pb-1 pt-1 text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-600)]"
        style={{ gridTemplateColumns: cols }}
      >
        {heads.map((h, i) => (
          <span key={h.key} className={cn("truncate", alignOf(variant, i))}>
            {h.label}
          </span>
        ))}
      </div>

      <div>
        {shown.length === 0 ? (
          <p className="px-4 py-6 text-[12.5px] text-[var(--color-neutral-500)]">
            {total === 0
              ? "Строк нет."
              : `Под фильтр не попало ни одной строки из ${total}.`}
          </p>
        ) : (
          shown.map((r) => {
            const page = pageOf(r);
            const cells = cellsOf(r, variant, unit, names);
            return (
              <div
                key={metricIdentityKey(r)}
                role={page ? "button" : undefined}
                tabIndex={page ? 0 : undefined}
                title={page ? "Открыть страницу-источник" : undefined}
                onClick={
                  page
                    ? () => onOpenSource?.({ docId: r.doc_id, page, label: rowTitle(r, variant) })
                    : undefined
                }
                onKeyDown={
                  page
                    ? (e) => {
                        if (e.key !== "Enter" && e.key !== " ") return;
                        e.preventDefault();
                        onOpenSource?.({ docId: r.doc_id, page, label: rowTitle(r, variant) });
                      }
                    : undefined
                }
                className={cn(
                  "grid gap-3 border-b border-[var(--color-divider)] px-4 py-1.5 text-[12.5px] leading-snug",
                  page && onOpenSource && "cursor-pointer hover:bg-[var(--color-accent-100)]",
                )}
                style={{ gridTemplateColumns: cols }}
              >
                {cells.map((c, i) => (
                  <span
                    key={i}
                    title={c.title || c.text}
                    className={cn(
                      "min-w-0 truncate",
                      alignOf(variant, i),
                      c.numeric && "[font-variant-numeric:tabular-nums]",
                      c.muted && "text-[var(--color-neutral-500)]",
                    )}
                  >
                    {c.text}
                  </span>
                ))}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

/**
 * Небольшой выпадающий список на токенах. Родной <select> не годится: его
 * раскрытый список рисует ОС — на тёмной теме он приезжает светлым.
 */
export function PickList({
  value,
  options,
  onPick,
  placeholder,
  className,
  search,
}: {
  value: string;
  options: { id: string; label: string; hint?: string }[];
  onPick: (id: string) => void;
  placeholder?: string;
  className?: string;
  /** Поле фильтра. По умолчанию появляется от пяти вариантов — но тексты в нём
   *  написаны про ДОКУМЕНТЫ, а список видов документа искать не нужно вовсе. */
  search?: boolean;
}) {
  const [open, setOpen] = useState(false);
  // ПОИСК ПО СПИСКУ (#250, правка Nate 17.08). В шапке рабочей области был
  // только выпадающий список с именем компании: в чате с одним документом это
  // достаточно, а с десятком — прокрутка глазами. Поле фильтра появляется,
  // когда выбирать действительно есть из чего.
  const [query, setQuery] = useState("");
  const boxRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const current = options.find((o) => o.id === value);
  const SEARCH_FROM = 5;
  const searchable = (search ?? true) && options.length >= SEARCH_FROM;
  const q = query.trim().toLowerCase();
  // Ищем и по имени, и по подписи: период и число страниц — то, чем документы
  // одной компании друг от друга и отличаются.
  const shown = q
    ? options.filter((o) => `${o.label} ${o.hint ?? ""}`.toLowerCase().includes(q))
    : options;

  return (
    <div ref={boxRef} className={cn("relative", className)}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={options.length === 0}
        onClick={() => {
          const next = !open;
          setOpen(next);
          setQuery("");
          // Открыли — курсор сразу в поиске: список для того и открывают,
          // чтобы найти, а не чтобы прокрутить.
          if (next && searchable) requestAnimationFrame(() => searchRef.current?.focus());
        }}
        className="flex w-full items-center gap-2 border-b border-[var(--color-divider)] py-1 text-left transition-colors hover:border-[var(--color-accent)] disabled:opacity-60"
      >
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-[var(--color-text)]">
          {current?.label ?? placeholder ?? "—"}
        </span>
        <ChevronDown
          className={cn(
            "size-3.5 shrink-0 text-[var(--color-neutral-500)] transition-transform",
            open && "rotate-180",
          )}
        />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute inset-x-0 top-[calc(100%+2px)] z-30 max-h-[280px] overflow-y-auto rounded-[var(--radius-token-md)] border border-[var(--color-divider)] bg-[var(--color-surface)] shadow-lg"
        >
          {searchable && (
            <div className="sticky top-0 flex items-center gap-2 border-b border-[var(--color-divider)] bg-[var(--color-surface)] px-3 py-1.5">
              <Search className="size-3.5 shrink-0 text-[var(--color-neutral-500)]" />
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Поиск по документам"
                className="min-w-0 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-[var(--color-neutral-500)]"
              />
            </div>
          )}
          {shown.length === 0 && (
            <p className="px-3 py-2 text-[12px] text-[var(--color-neutral-500)]">
              Ничего не найдено — под «{query}» нет ни одного документа.
            </p>
          )}
          {shown.map((o) => (
            <button
              key={o.id}
              type="button"
              role="option"
              aria-selected={o.id === value}
              onClick={() => {
                onPick(o.id);
                setOpen(false);
              }}
              className={cn(
                "block w-full px-3 py-1.5 text-left transition-colors hover:bg-[var(--color-accent-100)]",
                o.id === value && "bg-[var(--color-accent-100)]",
              )}
            >
              <span className="block truncate text-[12.5px] text-[var(--color-text)]">
                {o.label}
              </span>
              {o.hint && (
                <span className="block truncate text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
                  {o.hint}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ───────────────────────────── строки и колонки ─────────────────────────────

/**
 * Как назвать колонку сравнительного периода (#205).
 *
 * У промежуточного отчёта две оси: баланс сравнивается с концом прошлого года,
 * ОПУ — с тем же кварталом год назад. Одна подпись на всю таблицу верна для
 * одной половины строк и лжёт про другую, причём правдоподобно.
 *
 * 🔴 ОДНОЙ ПОДПИСИ МАЛО, ЧТОБЫ НАЗВАТЬ КОЛОНКУ, и это не педантизм. Подпись
 * появляется только у строк, которым сравнительное значение подобрали МЫ; там,
 * где его нашёл сам движок, подписи нет — её период равен периоду прогона.
 * Значит «ровно одна подпись» бывает и при ДВУХ осях: у баланса 31.12.2025,
 * у ОПУ молча Q1 2025. Назвать колонку балансовой датой значит соврать про
 * строки ОПУ — ровно то, против чего проверка и писалась, только наизнанку.
 * Поймано на живом прогоне 19.08 (подписей там не было вовсе), а не рассуждением.
 *
 * Пустая строка — колонка называется общим словом, а точный период каждой
 * строки виден подсказкой в ячейке.
 */
export function comparativeHeader(rows: MetricRow[], periodPrev?: string): string | undefined {
  const labels = new Set<string>();
  let unlabelled = false; // есть сравнительное значение, но подписи к нему нет
  for (const r of rows) {
    if (isEmpty(r.value_prev)) continue;
    const l = (r.period_prev_label || "").trim();
    if (l) labels.add(l);
    else unlabelled = true;
  }
  if (labels.size === 0) return periodPrev;
  if (labels.size > 1) return "";
  const only = labels.values().next().value as string;
  if (unlabelled && only !== (periodPrev || "").trim()) return "";
  return only;
}


type Cell = { text: string; numeric?: boolean; muted?: boolean; title?: string };

function headers(
  variant: ExtractedVariant,
  cur?: string,
  prev?: string,
): { key: string; label: string }[] {
  const c = (cur || "").trim() || "Текущий";
  const p = (prev || "").trim() || "Прошлый";
  if (variant === "segments") {
    return [
      { key: "seg", label: "Сегмент" },
      { key: "name", label: "Показатель" },
      { key: "cur", label: c },
      { key: "prev", label: p },
      { key: "src", label: "Источник" },
    ];
  }
  if (variant === "group") {
    return [
      { key: "name", label: "Общество" },
      { key: "share", label: "Доля" },
      { key: "rel", label: "Отношение" },
      { key: "reg", label: "Регистрация" },
      { key: "src", label: "Источник" },
    ];
  }
  return [
    { key: "name", label: "Метрика" },
    { key: "cur", label: c },
    { key: "prev", label: p },
    { key: "delta", label: "Δ" },
    { key: "src", label: "Источник" },
  ];
}

/** Правое выравнивание — у числовых колонок и «Источника». */
function alignOf(variant: ExtractedVariant, i: number): string {
  if (variant === "metrics") return i === 0 ? "" : "text-right";
  if (variant === "segments") return i >= 2 ? "text-right" : "";
  return i === 1 || i === 4 ? "text-right" : "";
}

function cellsOf(
  r: MetricRow,
  variant: ExtractedVariant,
  unit?: string,
  names?: Map<string, string>,
): Cell[] {
  const u = r.unit || unit || "";
  const src = sourceCell(r, names);

  if (variant === "segments") {
    return [
      { text: r.segment || "—", title: joinLines(r.segment, dimensionTitle(r)) },
      { text: r.name_ru || r.enum, title: r.enum },
      { text: fmtValue(r.value_current, u), numeric: true, muted: isEmpty(r.value_current) },
      { text: fmtValue(r.value_prev, u), numeric: true, muted: isEmpty(r.value_prev) },
      src,
    ];
  }

  if (variant === "group") {
    return [
      {
        text: r.segment || r.name_ru || "—",
        title: joinLines(r.segment || r.name_ru, r.group_activity),
      },
      {
        text: fmtValue(r.value_current, r.unit),
        numeric: true,
        muted: isEmpty(r.value_current),
      },
      { text: r.group_relation || "—", muted: !r.group_relation },
      { text: r.group_registration || "—", muted: !r.group_registration },
      src,
    ];
  }

  const d = deltaPct(r.value_current, r.value_prev);
  // ПОДПИСЬ СРАВНИТЕЛЬНОГО ПЕРИОДА — В ЯЧЕЙКЕ, а не только в шапке (#205).
  // У баланса сравнение с концом прошлого года (31.12.2025), у ОПУ — с тем же
  // кварталом год назад (Q1 2025). Одна подпись колонки на всю таблицу врёт
  // про две трети строк; здесь пользователь видит, с чем сравнили именно эту.
  const prevAt = (r.period_prev_label || "").trim();
  return [
    { text: r.name_ru || r.enum, title: r.enum },
    { text: fmtValue(r.value_current, u), numeric: true, muted: isEmpty(r.value_current) },
    {
      text: fmtValue(r.value_prev, u),
      numeric: true,
      muted: isEmpty(r.value_prev),
      title: prevAt ? `Сравнение с ${prevAt}` : undefined,
    },
    { text: fmtDelta(d), numeric: true, muted: d === null, title: prevAt ? `к ${prevAt}` : undefined },
    src,
  ];
}

/** «Источник» — страница отчёта; у расчётных строк источника нет, есть формула. */
function sourceCell(r: MetricRow, names?: Map<string, string>): Cell {
  const page = pageOf(r);
  if (page) {
    const form = (r.source_form || "").trim();
    return { text: form ? `стр. ${page} (${form})` : `стр. ${page}`, numeric: true };
  }
  if (r.is_calculated) {
    const hint = calculationHint(r, names);
    return { text: "расчёт", muted: true, title: hint || undefined };
  }
  return { text: "—", muted: true };
}

/**
 * Формула человеческим языком (правка Nate 18.08).
 *
 * В подсказке у «расчёта» стояло `NET_PROFIT / TOTAL_EQUITY` — имена реестра.
 * Аналитику они ничего не говорят: он спрашивает «как это посчитано», а
 * получает внутренние коды.
 *
 * Словарь берём из СОСЕДНИХ строк того же прогона: у каждой есть `enum` и
 * `name_ru`. Второго места, где такое соответствие пришлось бы поддерживать,
 * не заводим — оно и так приезжает вместе с числами. Показателя, который в
 * формуле есть, а в прогоне его нет, перевести нечем — оставляем код: это
 * честнее выдумки.
 */
export function formulaRu(formula: string, names?: Map<string, string>): string {
  if (!names?.size) return formula;
  return formula.replace(/[A-Z][A-Z0-9_]{2,}/g, (token) => names.get(token) || token);
}

/** Что сделали со входом перед подстановкой — так же, как это стоит в формуле. */
function inputLabel(enumCode: string, transform: string | undefined, names?: Map<string, string>): string {
  const name = formulaRu(enumCode, names);
  if (transform === "avg") return `AVG(${name})`;
  if (transform === "abs") return `abs(${name})`;
  if (transform === "sign") return `-${name}`;
  return name;
}

/**
 * Подсказка расчётной строки: формула, ЕЁ ЧИСЛА и — если пусто — причина (Б5).
 *
 * ЦЕНА. В подсказке стояла одна формула. Проверить по ней число нельзя: у
 * EBITDA есть фолбэк, который считает по другому выражению, а отсутствующий
 * необязательный вход вычислитель достраивает нулём — формула при этом
 * выглядит полной. Числа приезжают в `inputs` от самого вычислителя.
 *
 * Имена входов переводит `formulaRu` — тот же словарь по соседним строкам
 * прогона, второго места соответствия «код → имя» не заводим. Регуляркой, а не
 * поиском в словаре: у усреднения во входе стоит целое выражение
 * (`RIGHT_OF_USE_ASSETS + PPE`), и точное совпадение по ключу его не переведёт.
 *
 * ПЕРИОД НАЗВАН ВСЛУХ (20.08). Входы сняты за ОДНУ колонку, а подсказка одна на
 * строку: без периода она сходилась только с текущей, и читатель соседней
 * колонки видел числа, которые её не дают.
 *
 * ПРАВИЛО ЗДЕСЬ ОДНО НА ДВА ОТРИСОВЩИКА. То же самое печатает клетка
 * «Источник» ролевой книги (`roles/formatter.py::_calculated_source`), и
 * случаи закреплены общей разметкой `tests/fixtures/calculation_signature.json`
 * — её читают оба сторожа, vitest и pytest.
 */
export function calculationHint(r: MetricRow, names?: Map<string, string>): string {
  const parts: string[] = [];
  const formula = (r.formula || "").trim();
  if (formula) parts.push(formulaRu(formula, names));
  const rows = r.inputs ?? [];
  const inputs = rows.map((i) => {
    const zero = i.substituted_zero ? " — подставлен ноль" : "";
    return `${inputLabel(i.enum, i.transform, names)} = ${fmtValue(i.value, "")}${zero}`;
  });
  if (inputs.length) {
    const periods = new Set(rows.map((i) => (i.period || "").trim()));
    const period = periods.size === 1 ? [...periods][0] : "";
    parts.push((period ? `входы за ${period}: ` : "входы: ") + inputs.join("; "));
  }
  const missing = (r.calc_missing ?? []).map((e) => formulaRu(e, names));
  if (missing.length) parts.push(`не хватило: ${missing.join(", ")}`);
  return parts.join(" · ");
}

/** Соответствие «код → русское имя» по строкам прогона. */
export function metricNames(rows: MetricRow[]): Map<string, string> {
  const m = new Map<string, string>();
  for (const r of rows) {
    const name = (r.name_ru || "").trim();
    if (r.enum && name) m.set(r.enum, name);
  }
  return m;
}

/** Страница-источник: у строки либо своя, либо первая непустая из периодов. */
function pageOf(r: MetricRow): number | null {
  const direct = Number(r.page ?? 0);
  if (Number.isFinite(direct) && direct > 0) return direct;
  for (const pv of r.period_values ?? []) {
    const p = Number(pv.page ?? 0);
    if (Number.isFinite(p) && p > 0) return p;
  }
  return null;
}

function rowTitle(r: MetricRow, variant: ExtractedVariant): string {
  if (variant === "group") return r.segment || r.name_ru || r.enum;
  if (variant === "segments") return `${r.segment || "—"} · ${r.name_ru || r.enum}`;
  return r.name_ru || r.enum;
}

function searchHaystack(r: MetricRow): string {
  return [
    r.name_ru,
    r.enum,
    r.segment,
    r.dimension,
    r.source_form,
    r.group_relation,
    r.group_registration,
    r.group_activity,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

/** Подсказка строки: имя плюс уточнение, если оно есть. */
function joinLines(...parts: (string | undefined)[]): string | undefined {
  const t = parts.filter(Boolean).join("\n");
  return t || undefined;
}

function dimensionTitle(r: MetricRow): string | undefined {
  const d = (r.dimension || "").trim();
  if (!d) return undefined;
  return `Срез: ${DIMENSION_RU[d.toLowerCase()] ?? d}`;
}

// ───────────────────────────────── фильтр ─────────────────────────────────

function facetOf(r: MetricRow, variant: ExtractedVariant): string {
  if (variant === "segments") return (r.dimension || "").trim();
  if (variant === "group") return (r.group_relation || "").trim();
  // 🔴 ПОКАЗАТЕЛИ БЕЗ ФИЛЬТРА ПО ФОРМЕ ОТЧЁТНОСТИ (решение владельца 15.08).
  // Здесь стоял выпадающий список «Все формы / Баланс / ОиК / Примечания» по
  // полю `source_form`. Извлечение он не менял — резал уже готовую таблицу, —
  // но выглядел так, будто показатели извлекаются ПО РАЗДЕЛАМ отчётности.
  // В FinEngine таблица одна, реестром ячейки, без нарезки по формам; владелец
  // потребовал того же и здесь. Само поле `source_form` остаётся: оно едет в
  // Excel и в подсказку строки — убран только фильтр.
  return "";
}

function facetLabel(value: string, variant: ExtractedVariant): string {
  if (variant === "segments") return DIMENSION_RU[value.toLowerCase()] ?? value;
  return value;
}

function facetAllLabel(variant: ExtractedVariant): string {
  if (variant === "segments") return "Все срезы";
  if (variant === "group") return "Все связи";
  return "Все формы";
}

// ──────────────────────────────── значения ────────────────────────────────

function isEmpty(v: MetricValue | undefined): boolean {
  return v === null || v === undefined || (typeof v === "string" && !v.trim());
}

function fmtNumber(v: number): string {
  return Number.isInteger(v)
    ? v.toLocaleString("ru-RU")
    : v.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
}

function fmtValue(v: MetricValue | undefined, unit: string): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v.trim() || "—";
  return unit.trim() === "%" ? `${fmtNumber(v)}%` : fmtNumber(v);
}

/** Текстовые значения из отчёта («2 999,6») тоже участвуют в расчёте дельты. */
function asNumber(v: MetricValue | undefined): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string") return null;
  // \s в JS покрывает и неразрывный пробел — им разделены разряды в отчётах.
  const cleaned = v.trim().replace(/\s+/g, "").replace(/%$/, "").replace(",", ".");
  if (!cleaned || !/^-?\d+(?:\.\d+)?$/.test(cleaned)) return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

function deltaPct(cur: MetricValue | undefined, prev: MetricValue | undefined): number | null {
  const a = asNumber(cur);
  const b = asNumber(prev);
  if (a === null || b === null || b === 0) return null;
  return ((a - b) / Math.abs(b)) * 100;
}

function fmtDelta(d: number | null): string {
  if (d === null) return "—";
  const sign = d > 0 ? "+" : "";
  return Math.abs(d) >= 100 ? `${sign}${d.toFixed(0)}%` : `${sign}${d.toFixed(1)}%`;
}
