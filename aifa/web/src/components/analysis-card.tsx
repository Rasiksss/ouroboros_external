"use client";

import { useRef, useState } from "react";
import { Check, Copy, Download, Globe } from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import { kpiToTSV } from "@/lib/download";
import {
  PAGE_HREF_PREFIX,
  linkifyPageRefs,
  pageFromHref,
  splitPageRefs,
} from "@/lib/page-refs";
import { AssistantMarkdown } from "@/components/assistant-markdown";
import { useWorkspaceOptional } from "@/components/workspace/workspace-context";
import { AnalysisChart } from "@/components/analysis-chart";
import type { AnalysisCard as AnalysisCardData, AnalysisCardKpi } from "@/lib/api";

// Status-pill styling per flag. Colours use tailwind's /15 alpha so they read
// well in both light and dark themes; "na" falls back to the neutral token.
// «crit» намеренно НЕ такой же красный, как «risk»: если верхние две ступени
// выглядят одинаково, четвёртый уровень бессмыслен — он и заводился ради того,
// чтобы «предел уже нарушен» было видно с одного взгляда.
const FLAG_STYLES: Record<AnalysisCardKpi["flag"], { pill: string; label: string }> = {
  good: { pill: "bg-emerald-500/25 text-emerald-700 dark:text-emerald-400", label: "позитивный" },
  ok: { pill: "bg-emerald-500/15 text-emerald-600", label: "норма" },
  warn: { pill: "bg-amber-500/15 text-amber-600", label: "внимание" },
  risk: { pill: "bg-rose-500/15 text-rose-600", label: "риск" },
  crit: { pill: "bg-rose-600 text-white", label: "критический" },
  na: { pill: "bg-muted text-muted-foreground", label: "нет данных" },
};

// Обложка отчёта: плашка уровня риска. Уровень приезжает с бэкенда посчитанным
// ПРАВИЛОМ (metric_sanity.risk_profile) — фронт только красит; «unknown» — это
// честное «не определён», а не тихий зелёный.
const RISK_STYLES: Record<string, string> = {
  critical: "border-rose-600/50 bg-rose-600/15 text-rose-700 dark:text-rose-400",
  elevated: "border-orange-500/50 bg-orange-500/10 text-orange-700 dark:text-orange-400",
  watch: "border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-500",
  clear: "border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  unknown: "border-border bg-muted text-muted-foreground",
};

function companyTypeLabel(t: string): string {
  const v = t.toLowerCase();
  if (v.includes("bank") || v.includes("банк")) return "банк";
  if (v.includes("non") || v.includes("нефин") || v.includes("corp")) return "нефин.";
  return t;
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-base font-bold text-foreground">{children}</h3>;
}

/** Проза карточки (вердикт, выводы, пункты) со ссылками «PDF стр. N».
 *
 * `linked=false` — рисуем голый текст: так карточка ведёт себя вне рабочего
 * экрана и у старых карточек без doc_id, где прыгать некуда. Компонент вынесен
 * НАРУЖУ карточки намеренно: объявленный внутри, он получал бы новую
 * идентичность на каждый рендер и пересоздавал бы всё поддерево.
 */
function Prose({ text, linked }: { text: string; linked: boolean }) {
  if (!linked) return <>{text}</>;
  return (
    <>
      {splitPageRefs(text).map((part, i) =>
        part.page ? (
          <a
            key={i}
            href={`${PAGE_HREF_PREFIX}${part.page}`}
            className="text-primary underline-offset-2 hover:underline"
            title="Открыть эту страницу исходного PDF"
          >
            {part.text}
          </a>
        ) : (
          <span key={i}>{part.text}</span>
        ),
      )}
    </>
  );
}

/**
 * #159. Строка «Контекста извне» без источника, ссылки или даты НЕ показывается.
 *
 * Бэкенд её и не пришлёт: сборщик (`analysis/external.py:_accept`) такую строку
 * не создаёт. Здесь та же проверка стоит вторым рубежом — на случай карточки,
 * приехавшей из старого сохранённого диалога или собранной в обход сборщика.
 * Дублирование намеренное: цена ошибки несимметрична. Пропавшая строка — это
 * минус одна новость, а строка без адреса встаёт рядом с выверенными числами
 * отчёта и получает их авторитет даром, хотя проверить её читателю нечем.
 */
function isCitable(x: { text?: string; source?: string; url?: string; date?: string }): boolean {
  return Boolean(x.text?.trim() && x.source?.trim() && x.url?.trim() && x.date?.trim());
}

export function AnalysisCard({ card }: { card: AnalysisCardData }) {
  const cardRef = useRef<HTMLDivElement>(null);

  // Прыжок к странице-источнику (#157). Приёмник УЖЕ работает у таблицы
  // извлечения — переиспользуем его, второй реализации у перехода в документ
  // быть не должно. Вне рабочего экрана (старый чат «/») контекста нет, и
  // ссылок мы не рисуем: ссылка, которая никуда не ведёт, хуже простого
  // текста. Нет doc_id (карточка из старого прогона) — тоже не рисуем.
  const ws = useWorkspaceOptional();
  const jumpDocId = card.doc_id || "";
  const canJump = !!ws && !!jumpDocId;

  /** Клик по ссылке страницы — один делегат на всю карточку.
   *  Иначе пришлось бы протаскивать обработчик внутрь рендерера markdown
   *  (разделы приходят разметкой) и отдельно в каждую строку прозы. */
  function onCardClick(e: React.MouseEvent<HTMLDivElement>) {
    if (!canJump) return;
    const anchor = (e.target as HTMLElement).closest("a");
    const page = pageFromHref(anchor?.getAttribute("href"));
    if (!page) return;
    e.preventDefault();
    ws!.gotoSource({ docId: jumpDocId, page, label: "источник числа в отчёте" });
  }

  const meta = [
    card.period,
    card.company_type ? companyTypeLabel(card.company_type) : "",
    card.standard,
    card.language,
  ].filter(Boolean);

  const kpi = card.kpi ?? [];
  const report = (card.report_md ?? "").trim();
  const sections = card.sections ?? [];
  const strengths = card.strengths ?? [];
  const watch = card.watch ?? [];
  const conclusions = card.conclusions ?? [];
  const external = (card.external_context ?? []).filter(isCitable);
  const risk = card.risk_profile;
  const charts = card.charts ?? [];
  const [copied, setCopied] = useState(false);

  // Кнопка — ИКОНКА без подписи (правка Nate 02.08). Слово «Excel» обещало
  // лишнее: TSV из буфера одинаково ложится и в Excel, и в Таблицы, и в
  // Numbers, а «копировать» — единственное, что кнопка делает. Подтверждение
  // галочкой на месте иконки, а не всплывающим сообщением: оно короче пути
  // взгляда и не перекрывает саму таблицу.
  async function copyTable() {
    try {
      await navigator.clipboard.writeText(kpiToTSV(kpi));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (e) {
      toast.error("Не удалось скопировать: " + (e as Error).message);
    }
  }

  // window.print() prints the whole document, so we tag THIS card as the print
  // target and let @media print (globals.css) hide everything else. Cleared on
  // afterprint so the DOM returns to normal.
  function printPdf() {
    const el = cardRef.current;
    if (!el) {
      window.print();
      return;
    }
    el.classList.add("analysis-print-active");
    const cleanup = () => {
      el.classList.remove("analysis-print-active");
      window.removeEventListener("afterprint", cleanup);
    };
    window.addEventListener("afterprint", cleanup);
    window.print();
  }

  return (
    <div
      ref={cardRef}
      onClick={onCardClick}
      className="bg-card border rounded-lg p-4 sm:p-5 space-y-4 not-prose"
    >
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          {card.company && (
            <div className="text-lg font-bold text-foreground truncate">
              {card.company}
            </div>
          )}
          {meta.length > 0 && (
            <div className="mt-0.5 text-xs text-muted-foreground">
              {meta.join(" · ")}
            </div>
          )}
        </div>
        <button
          type="button"
          onClick={printPdf}
          className="print:hidden shrink-0 inline-flex items-center gap-1.5 rounded-md border px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground hover:bg-muted"
          title="Скачать карточку в PDF (печать → сохранить как PDF)"
        >
          <Download className="size-3.5" />
          <span>Скачать PDF</span>
        </button>
      </div>

      {/* Risk profile — обложка. Стоит ПЕРЕД вердиктом: отчёт должен
          открываться уровнем риска, а не абзацем прозы. */}
      {risk?.label && (
        <div className={cn("rounded-md border px-3 py-2.5", RISK_STYLES[risk.level ?? "unknown"] ?? RISK_STYLES.unknown)}>
          <div className="text-sm font-bold">
            Итоговый риск-профиль: {risk.label.toUpperCase()}
          </div>
          {(risk.why ?? []).length > 0 && (
            <ul className="mt-1.5 list-disc pl-4 space-y-0.5 text-xs leading-relaxed">
              {(risk.why ?? []).map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Verdict */}
      {card.verdict && (
        <div className="rounded-md bg-muted px-3 py-3">
          <div className="text-base font-bold text-foreground">Вердикт</div>
          <p className="mt-1.5 text-sm leading-relaxed text-foreground">
            <Prose text={card.verdict} linked={canJump} />
          </p>
        </div>
      )}

      {/* Key figures */}
      {kpi.length > 0 && (
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3">
            <SectionTitle>Ключевые показатели</SectionTitle>
            <button
              type="button"
              onClick={copyTable}
              aria-label="Копировать таблицу"
              className="print:hidden shrink-0 rounded-md border p-1.5 text-muted-foreground hover:text-foreground hover:bg-muted"
              title={copied ? "Скопировано" : "Копировать таблицу"}
            >
              {copied
                ? <Check className="size-3.5 text-emerald-600 dark:text-emerald-500" />
                : <Copy className="size-3.5" />}
            </button>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="text-left text-xs text-muted-foreground border-b">
                  <th className="py-1.5 pr-3 font-medium">Показатель</th>
                  <th className="py-1.5 px-3 font-medium text-right">Значение</th>
                  <th className="py-1.5 px-3 font-medium text-right">Δ</th>
                  <th className="py-1.5 pl-3 font-medium">Статус</th>
                </tr>
              </thead>
              <tbody>
                {kpi.map((k, i) => {
                  const style = FLAG_STYLES[k.flag] ?? FLAG_STYLES.na;
                  return (
                    <tr
                      key={`${k.name}-${i}`}
                      className="border-b last:border-0 align-top"
                      title={k.why || undefined}
                    >
                      <td className="py-1.5 pr-3 text-foreground">{k.name}</td>
                      <td className="py-1.5 px-3 text-right tabular-nums whitespace-nowrap text-foreground">
                        {k.value}
                      </td>
                      <td className="py-1.5 px-3 text-right tabular-nums whitespace-nowrap text-muted-foreground">
                        {k.delta}
                      </td>
                      <td className="py-1.5 pl-3">
                        <span
                          className={cn(
                            "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
                            style.pill,
                          )}
                        >
                          {style.label}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <p className="text-[11px] text-muted-foreground">
            Статус — по значимости; обоснование в разделах.
          </p>
        </div>
      )}

      {/* Графики (#157). Сразу под таблицей показателей — намеренно: столбик и
          строка таблицы это ОДНО И ТО ЖЕ число, и увидеть их надо рядом.
          Спецификация приходит с бэкенда данными; не построился (нет входов,
          разошлись единицы) — блока просто нет, причина в логе прогона. */}
      {charts.length > 0 && (
        <div className="space-y-4">
          {charts.map((c, i) => (
            <AnalysisChart key={`${c.title}-${i}`} spec={c} />
          ))}
        </div>
      )}

      {/* Отчёт целиком — как написал аналитик.
          Раньше здесь рисовался список разделов, который бэкенд получал
          нарезкой markdown по заголовкам и сопоставлением их с ролями
          методики. Рисовалось при этом ровно то же: заголовок + markdown, в
          том же порядке. Разбор не давал ничего и терял блоки с несовпавшим
          заголовком — снят 11.08 (parse.parse_report). */}
      {report && <AssistantMarkdown text={canJump ? linkifyPageRefs(report) : report} />}

      {/* Карточки из сохранённых диалогов, собранные до 11.08: у них нет
          report_md, зато есть нарезанные разделы. Рисуем по-старому — иначе
          вся прошлая переписка осталась бы без текста. */}
      {!report && sections.length > 0 && (
        <div className="space-y-3">
          {sections.map((s, i) => (
            <div key={`${s.title}-${i}`} className="space-y-1">
              {s.title && (
                <h3 className="text-sm font-semibold text-foreground">{s.title}</h3>
              )}
              {s.body && (
                <AssistantMarkdown text={canJump ? linkifyPageRefs(s.body) : s.body} />
              )}
            </div>
          ))}
        </div>
      )}

      {/* Strengths / watch */}
      {(strengths.length > 0 || watch.length > 0) && (
        <div
          className="grid gap-3"
          style={{ gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))" }}
        >
          {strengths.length > 0 && (
            <div className="rounded-md bg-emerald-500/10 p-3">
              <SectionTitle>Сильные стороны</SectionTitle>
              <ul className="mt-1.5 list-disc pl-4 space-y-1 text-sm text-foreground">
                {strengths.map((s, i) => (
                  <li key={i}>
                    <Prose text={s} linked={canJump} />
                  </li>
                ))}
              </ul>
            </div>
          )}
          {watch.length > 0 && (
            <div className="rounded-md bg-amber-500/10 p-3">
              <SectionTitle>Зоны внимания</SectionTitle>
              <ul className="mt-1.5 list-disc pl-4 space-y-1 text-sm text-foreground">
                {watch.map((w, i) => (
                  <li key={i}>
                    <Prose text={w} linked={canJump} />
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Conclusions */}
      {conclusions.length > 0 && (
        <div className="space-y-1.5">
          <SectionTitle>Выводы</SectionTitle>
          <ol className="list-decimal pl-4 space-y-1 text-sm text-foreground">
            {conclusions.map((c, i) => (
              <li key={i}>
                <Prose text={c} linked={canJump} />
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Контекст извне (#159).
          Оформление намеренно НЕ такое, как у остальных блоков: пунктирная
          рамка и приглушённый фон — это граница между выверенным и найденным.
          Читатель обязан видеть, где кончаются числа отчёта. Палитру флагов
          (emerald/amber/rose) здесь не берём: она означает ОЦЕНКУ показателя,
          а не происхождение сведений, и жёлтая рамка прочиталась бы как
          «тревожно», хотя речь про «из другого источника».
          Идёт ПОСЛЕДНИМ — тем же порядком, что просили у модели в промпте. */}
      {external.length > 0 && (
        <section className="rounded-md border border-dashed bg-muted/40 p-3">
          <div className="flex items-center gap-1.5">
            <Globe className="size-3.5 shrink-0 text-muted-foreground" />
            <SectionTitle>Контекст извне</SectionTitle>
          </div>
          <p className="mt-1 text-[11px] text-muted-foreground">
            Не из отчётности — публикации внешних источников. По отчёту не выверены:
            проверяйте по ссылке.
          </p>
          <ul className="mt-2 space-y-2">
            {external.map((x, i) => (
              <li key={x.url || i} className="text-sm text-foreground">
                <span>{x.text}</span>
                {/* Источник и дата — в КАЖДОЙ строке, а не сноской в конце
                    блока: строку копируют и пересылают поодиночке, и без
                    адреса она тут же становится «фактом ниоткуда». */}
                <span className="mt-0.5 block text-[11px] text-muted-foreground">
                  <a
                    href={x.url}
                    target="_blank"
                    rel="noreferrer noopener"
                    className="underline underline-offset-2 hover:text-foreground"
                  >
                    {x.source}
                  </a>
                  {" · "}
                  {x.date}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Footnote */}
      {card.footnote && (
        <p className="border-t pt-3 text-[11px] text-muted-foreground">{card.footnote}</p>
      )}
    </div>
  );
}
