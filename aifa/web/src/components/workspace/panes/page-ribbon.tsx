"use client";

import { useEffect, useRef, useState } from "react";
import { cn } from "@/lib/utils";

/**
 * Непрерывная лента страниц исходного PDF.
 *
 * Рендер — тот же, что уже работает в pdf-viewer.tsx: встроенный просмотрщик
 * браузера в <iframe> с якорями Open Parameters (#page/#view). Второй способ
 * (canvas через pdf.js) не заводим — это новая зависимость, а браузерный
 * просмотрщик уже проверен продом.
 *
 * Отсюда три неочевидных решения:
 *
 * 1. Страница ленты = отдельный <iframe> c #page=N&view=Fit. Каждый такой
 *    iframe — самостоятельный экземпляр просмотрщика (байты PDF берутся из
 *    HTTP-кэша, но документ парсится заново), поэтому держим смонтированными
 *    только соседние страницы, а остальные — пустые паспарту известной высоты.
 * 2. Монтируем не мгновенно, а когда прокрутка утихла: без этого быстрый скролл
 *    успевает создать и убить десятки экземпляров просмотрщика.
 * 3. Поверх каждого iframe лежит прозрачная накладка. Без неё колесо над
 *    страницей крутит внутренний просмотрщик, а не ленту. Плата за это —
 *    внутри страницы нельзя выделить текст; выделение остаётся в отдельном
 *    окне документа (ссылка «открыть» в панели).
 */

/** Высота страницы из дизайн-эталона; одинаковая у всех — на ней держится виртуализация. */
const PAGE_HEIGHT = 520;
/** Сколько страниц вокруг текущей держим реально отрисованными. */
const NEAR = 1;
/** Пауза после прокрутки, после которой монтируем просмотрщики. */
const SETTLE_MS = 180;

export type RibbonTarget = { page: number; label?: string } | null;

export function PageRibbon({
  fileUrl,
  totalPages,
  target,
}: {
  fileUrl: string;
  totalPages: number;
  /** Страница-источник метрики; новый объект на каждый клик по таблице. */
  target: RibbonTarget;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pageRefs = useRef<Array<HTMLDivElement | null>>([]);
  const ratios = useRef(new Map<number, number>());
  const [active, setActive] = useState(1);
  const [renderAt, setRenderAt] = useState(1);

  const pages = Array.from({ length: totalPages }, (_, i) => i + 1);
  const sourcePage = target && target.page >= 1 && target.page <= totalPages ? target.page : 0;

  // Активная страница — та, что занимает больше всего видимой части ленты.
  useEffect(() => {
    const root = scrollRef.current;
    if (!root) return;
    const seen = ratios.current;
    seen.clear();
    const io = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          const n = Number((e.target as HTMLElement).dataset.page);
          seen.set(n, e.isIntersecting ? e.intersectionRatio : 0);
        }
        let bestRatio = 0;
        let bestPage = 0;
        for (const [n, r] of seen) {
          if (r > bestRatio) {
            bestRatio = r;
            bestPage = n;
          }
        }
        if (bestPage) setActive(bestPage);
      },
      { root, threshold: [0, 0.1, 0.25, 0.5, 0.75, 1] },
    );
    // Если страниц стало меньше, в массиве остаются оторванные узлы прошлого
    // документа — их наблюдать нельзя, они никогда не пересекутся с лентой.
    pageRefs.current.length = totalPages;
    for (const el of pageRefs.current) if (el) io.observe(el);
    return () => io.disconnect();
  }, [totalPages]);

  // Просмотрщики поднимаем только когда прокрутка остановилась.
  useEffect(() => {
    const t = setTimeout(() => setRenderAt(active), SETTLE_MS);
    return () => clearTimeout(t);
  }, [active]);

  // Переход к странице-источнику. Прыжок мгновенный: при плавном пролёте через
  // сотню страниц ловушка «утихшей прокрутки» сработала бы на каждой из них.
  useEffect(() => {
    if (!target || !sourcePage) return;
    const el = pageRefs.current[sourcePage - 1];
    const root = scrollRef.current;
    if (!el || !root) return;
    root.scrollTo({ top: el.offsetTop - 12 });
    setActive(sourcePage);
  }, [target, sourcePage]);

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        className="relative flex h-full flex-col gap-3 overflow-y-auto bg-[var(--color-neutral-200)] p-3"
      >
        {pages.map((n) => {
          const isActive = n === active;
          const isSource = n === sourcePage;
          const isLive = Math.abs(n - renderAt) <= NEAR;
          return (
            <div
              key={n}
              data-page={n}
              ref={(el) => {
                pageRefs.current[n - 1] = el;
              }}
              // Паспарту: 8px подложки --color-surface, поверх неё хайрлайн.
              className={cn(
                "shrink-0 bg-[var(--color-surface)] p-2 ring-1",
                isActive
                  ? "ring-[var(--color-accent)]"
                  : isSource
                    ? "ring-[var(--color-accent-300)]"
                    : "ring-[var(--color-divider)]",
              )}
              style={{ height: PAGE_HEIGHT }}
            >
              <div className="flex h-full flex-col bg-[var(--color-neutral-100)]">
                <div className="flex shrink-0 items-baseline gap-2 px-2 py-1">
                  <span className="text-[9.5px] uppercase tracking-[0.16em] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                    стр. {n}
                  </span>
                  {isSource && (
                    <span className="ml-auto text-[9.5px] uppercase tracking-[0.12em] text-[var(--color-accent-700)]">
                      источник метрики
                    </span>
                  )}
                </div>
                <div className="relative min-h-0 flex-1">
                  {isLive ? (
                    <>
                      <iframe
                        src={`${fileUrl}#page=${n}&view=Fit&toolbar=0&navpanes=0&scrollbar=0&statusbar=0&messages=0`}
                        title={`Страница ${n}`}
                        className="size-full border-0 bg-[var(--color-neutral-100)]"
                      />
                      {/* Накладка: колесо крутит ленту, клик выбирает страницу. */}
                      <button
                        type="button"
                        aria-label={`Страница ${n}`}
                        title="Колесо — листать ленту, клик — выбрать страницу"
                        onClick={() => setActive(n)}
                        className="absolute inset-0 cursor-default bg-transparent"
                      />
                    </>
                  ) : (
                    <div className="grid size-full place-items-center">
                      <span className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-400)] [font-variant-numeric:tabular-nums]">
                        стр. {n}
                      </span>
                    </div>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {/* Счётчик следует за прокруткой — висит над лентой, а не внутри неё. */}
      <span className="pointer-events-none absolute right-4 bottom-3 border border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1 text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
        стр. {active} / {totalPages}
      </span>
    </div>
  );
}
