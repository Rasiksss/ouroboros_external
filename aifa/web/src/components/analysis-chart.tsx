"use client";

import type { AnalysisChart as AnalysisChartSpec } from "@/lib/api";

/** График карточки анализа — рисуется ПО СПЕЦИФИКАЦИИ с бэкенда (#157).
 *
 * Почему SVG собирается здесь элементами React, а не приходит готовым:
 *  • разметка из ответа модели — это внедрение чужого HTML в страницу;
 *    `dangerouslySetInnerHTML` здесь запрещён, и обойти его нечем — весь SVG
 *    ниже собран из чисел, ни одной строки разметки со стороны;
 *  • числа обязаны быть теми же, что в таблице: спецификация приходит с
 *    заземления прогона, а компонент их только РАСКЛАДЫВАЕТ по координатам.
 *    Никаких пересчётов, округлений «для красоты» и домножений здесь нет.
 *
 * МАСШТАБ ОСИ — ИЗ ДАННЫХ. Подпись единицы берётся из `spec.unit` и печатается
 * как есть; делителей и «удобных» коэффициентов в коде нет намеренно. У одного
 * отчёта млн ₽, у другого тыс., у третьего доллары — зашитый здесь множитель
 * превратил бы график в ложь, не показав ни одной неверной цифры.
 */

const W = 640;
const H = 300;
const PAD_TOP = 26; // место под подпись значения над самым высоким столбиком
const PAD_BOTTOM = 62; // место под подписи столбиков (до трёх строк)
const PLOT_H = H - PAD_TOP - PAD_BOTTOM;
const BAR_RATIO = 0.56; // доля ширины категории, занятая столбиком

/** Число так, как его читает человек, и с той точностью, которая у величины
 * есть: у миллионов дробная часть — шум, у коэффициента она и есть значение. */
function fmt(v: number): string {
  const abs = Math.abs(v);
  const digits = abs >= 1000 ? 0 : abs >= 1 ? 1 : 2;
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(v);
}

/** Подпись столбика в несколько строк: имена показателей реестра длинные
 * («Чистый операционный денежный поток»), и в ширину категории они не влезают
 * ни при каком кегле.
 *
 * ГРАБЛЯ (найдена при отрисовке). Первая версия при упоре в лимит строк просто
 * ПРЕКРАЩАЛА набор, и «Чистый операционный денежный поток» подписывался как
 * «Чистый операционный денежный»: столбик молча менял смысл — это уже не
 * операционный поток, а неизвестно что. Поэтому лимит строк мягкий: кончились
 * строки — доклеиваем остаток к последней. Подпись станет длиннее задуманного,
 * но останется ВЕРНОЙ, а это здесь важнее вёрстки. */
function wrap(label: string, perLine = 15, maxLines = 3): string[] {
  const lines: string[] = [];
  for (const word of label.split(/\s+/).filter(Boolean)) {
    const last = lines.length ? lines[lines.length - 1] : undefined;
    if (last !== undefined && (`${last} ${word}`.length <= perLine || lines.length >= maxLines)) {
      lines[lines.length - 1] = `${last} ${word}`;
    } else {
      lines.push(word);
    }
  }
  return lines;
}

type Bar = {
  label: string;
  value: number;
  from: number;
  to: number;
  anchor: boolean; // столбик от нуля (начало и итоги), а не приращение
};

/** Разложить точки водопада в отрезки [from, to].
 *
 * `start`/`total` — столбики ОТ НУЛЯ: их значение самодостаточно (итог пришёл
 * из таблицы заземления, а не сложен здесь). `delta` — приращение от текущего
 * накопленного уровня. Накопление после каждого итога СБРАСЫВАЕТСЯ на его
 * значение: если бы итог и накопленная сумма разошлись, продолжать надо от
 * итога — он посчитан кодом по формуле реестра, а не нами по дороге. */
export { wrap };

export function layout(points: AnalysisChartSpec["points"]): Bar[] {
  let cum = 0;
  return points.map((p) => {
    const anchor = p.kind !== "delta";
    const from = anchor ? 0 : cum;
    const to = anchor ? p.value : cum + p.value;
    cum = to;
    return { label: p.label, value: p.value, from, to, anchor };
  });
}

export function AnalysisChart({ spec }: { spec: AnalysisChartSpec }) {
  const points = spec.points ?? [];
  if (points.length === 0) return null;

  const bars = layout(points);
  const lo = Math.min(0, ...bars.map((b) => Math.min(b.from, b.to)));
  const hi = Math.max(0, ...bars.map((b) => Math.max(b.from, b.to)));
  const span = hi - lo || 1;
  const y = (v: number) => PAD_TOP + ((hi - v) / span) * PLOT_H;

  const step = W / bars.length;
  const barW = step * BAR_RATIO;
  const zeroY = y(0);

  return (
    <figure className="space-y-1.5">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className="text-sm font-semibold text-foreground">{spec.title}</span>
        {/* Единица — рядом с заголовком, а не в подписях столбиков: она одна на
            весь график, и повторять её шесть раз значит спрятать её. */}
        {spec.unit && (
          <span className="text-[11px] text-muted-foreground">{spec.unit}</span>
        )}
      </figcaption>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full h-auto overflow-visible"
        role="img"
        aria-label={`${spec.title}: ${bars
          .map((b) => `${b.label} ${fmt(b.value)} ${spec.unit}`)
          .join("; ")}`}
      >
        {/* Нулевая линия — единственный ориентир масштаба на графике; без неё
            «столбик ушёл вниз» неотличимо от «столбик просто короче». */}
        <line
          x1={0}
          x2={W}
          y1={zeroY}
          y2={zeroY}
          className="text-border"
          stroke="currentColor"
          strokeWidth={1}
        />
        {bars.map((b, i) => {
          const cx = i * step + step / 2;
          const top = Math.min(y(b.from), y(b.to));
          const height = Math.max(Math.abs(y(b.to) - y(b.from)), 1);
          const negative = b.anchor ? b.value < 0 : b.value < 0;
          const tone = b.anchor
            ? negative
              ? "text-rose-600 dark:text-rose-500"
              : "text-sky-600 dark:text-sky-500"
            : negative
              ? "text-rose-500/70"
              : "text-emerald-500/70";
          const prev = bars[i - 1];
          return (
            <g key={`${b.label}-${i}`} className={tone}>
              {/* Соединитель с предыдущим столбиком: без него водопад читается
                  как обычная гистограмма и теряет весь смысл. */}
              {prev && (
                <line
                  x1={(i - 1) * step + step / 2 + barW / 2}
                  x2={cx - barW / 2}
                  y1={y(prev.to)}
                  y2={y(prev.to)}
                  className="text-muted-foreground/40"
                  stroke="currentColor"
                  strokeWidth={1}
                  strokeDasharray="3 3"
                />
              )}
              <rect
                x={cx - barW / 2}
                y={top}
                width={barW}
                height={height}
                fill="currentColor"
                rx={2}
              />
              <text
                x={cx}
                y={top - 6}
                textAnchor="middle"
                fill="currentColor"
                className="text-[13px] font-semibold"
              >
                {fmt(b.value)}
              </text>
              <text
                x={cx}
                y={H - PAD_BOTTOM + 16}
                textAnchor="middle"
                className="text-[11px] text-muted-foreground"
                fill="currentColor"
              >
                {wrap(b.label).map((line, li) => (
                  <tspan key={li} x={cx} dy={li === 0 ? 0 : 13}>
                    {line}
                  </tspan>
                ))}
              </text>
            </g>
          );
        })}
      </svg>
      {spec.note && <p className="text-xs text-foreground">{spec.note}</p>}
      {/* Источник числа — под каждым графиком: «откуда это» не должно требовать
          отдельного вопроса в чат. */}
      {spec.source && (
        <p className="text-[11px] text-muted-foreground">Источник: {spec.source}</p>
      )}
    </figure>
  );
}
