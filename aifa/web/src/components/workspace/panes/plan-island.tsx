"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import type { PlanStep } from "@/lib/projects";

/**
 * Островок прогресса — шапка рабочей области. ДВЕ страницы, между которыми
 * листают горизонтальным жестом (двумя пальцами по тачпаду) прямо внутри
 * прямоугольника:
 *   1) «План» — чек-лист хода из событий `plan` / `plan_step`;
 *   2) «Агенты» — параллельные воркеры, когда задача разошлась по нескольким
 *      прогонам (каждый шлёт свой поток с прогрессом).
 *
 * Вторая страница НЕ рисуется, если параллельных прогонов не было: показывать
 * пустую схему воркеров там, где работал один агент, — враньё.
 *
 * Сворачивание контролируется снаружи (`open` / `onOpenChange`): событие `plan`
 * должно уметь развернуть остров, а это знает владелец стрима, не панель.
 * Долю прогресса компонент считает сам из `plan` — второй источник правды
 * позволил бы нарисовать полосу, не совпадающую со списком под ней.
 */

/** Один параллельный прогон (воркер/комната) текущего хода. */
export type WorkerRun = {
  id: string;
  label: string;
  status: "running" | "done" | "error" | "aborted";
  /** Сколько показателей уже пришло и сколько ожидается (0 — размер неизвестен). */
  found: number;
  total: number;
};

export type PlanLane = { label: string; steps: PlanStep[] };

export type PlanIslandProps = {
  /** Шаги хода. null | [] — плана нет: честная надпись, а не пустой остров. */
  plan: PlanStep[] | null;
  /** Линии работ хода (анализ, извлечение, …) — каждая со своим под-планом.
   * Задана хотя бы одна — страница «План» рисует их группами; `plan` тогда
   * не используется. Так план отражает ВСЮ задачу, а не последний кусок. */
  lanes?: PlanLane[];
  /** Параллельные прогоны хода. Меньше двух — страница агентов не нужна. */
  workers?: WorkerRun[];
  open: boolean;
  onOpenChange: (next: boolean) => void;
  /** Клик по шагу. Не задан — строки некликабельны (в PlanStep пока нет
   *  ни id, ни ссылки на параметр, которым шаг управляет). */
  onStepClick?: (index: number, step: PlanStep) => void;
  className?: string;
};

const EMPTY: PlanStep[] = [];

const STATUS_LABEL: Record<PlanStep["status"], string> = {
  pending: "в очереди",
  running: "выполняется",
  done: "готово",
  error: "ошибка",
  empty: "нет результата",
};

const DOT_BG: Record<PlanStep["status"], string> = {
  pending: "var(--color-neutral-300)",
  running: "var(--color-accent)",
  done: "var(--color-accent-700)",
  empty: "var(--color-neutral-400)",
  // Единственный цвет вне палитры темы: ошибку нечем показать в нейтралях,
  // а --destructive объявлен для всех трёх тем (globals.css).
  error: "var(--destructive)",
};

const TITLE_FG: Record<PlanStep["status"], string> = {
  pending: "var(--color-neutral-500)",
  running: "var(--color-text)",
  done: "var(--color-neutral-700)",
  empty: "var(--color-neutral-500)",
  error: "var(--destructive)",
};

export function PlanIsland({
  plan,
  lanes,
  workers,
  open,
  onOpenChange,
  onStepClick,
  className,
}: PlanIslandProps) {
  const laneList = (lanes ?? []).filter((l) => l.steps.length);
  const steps = laneList.length ? laneList.flatMap((l) => l.steps) : (plan ?? EMPTY);
  // Страница агентов появляется, только когда воркеров РЕАЛЬНО несколько.
  const runs = workers ?? [];
  const hasWorkers = runs.length > 0;
  // Выбор страницы пользователем НЕ сбрасывается. Раньше стоял эффект
  // «нет воркеров → setPage(0)», а список воркеров пересобирается на каждое
  // событие хода и на миг пустеет — остров сам утаскивал пользователя со
  // страницы «Агенты» обратно на план, раз за разом. Теперь выбранная
  // страница живёт, а когда листать нечего — просто ПОКАЗЫВАЕТСЯ план,
  // без затирания выбора: воркеры вернулись — вернулась и страница.
  const [chosenPage, setPage] = useState(0);
  const page = hasWorkers ? chosenPage : 0;

  // Горизонтальный жест внутри острова листает страницы. Слушатель нативный и
  // НЕ пассивный: React вешает колесо пассивно, и preventDefault (без него
  // страница уезжает вбок вместе с жестом) там не работает. Копим смещение и
  // берём паузу после переключения — иначе один взмах пролистал бы всё.
  const boxRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = boxRef.current;
    if (!el || !hasWorkers) return;
    let acc = 0;
    let lock = false;
    const onWheel = (e: WheelEvent) => {
      if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return; // вертикаль — не наше
      e.preventDefault();
      if (lock) return;
      acc += e.deltaX;
      if (Math.abs(acc) < 40) return;
      setPage((p) => Math.max(0, Math.min(1, p + (acc > 0 ? 1 : -1))));
      acc = 0;
      lock = true;
      setTimeout(() => {
        lock = false;
      }, 320);
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [hasWorkers]);
  const total = steps.length;
  const hasPlan = total > 0;

  const done = steps.filter((s) => s.status === "done").length;
  const runningIndex = steps.findIndex((s) => s.status === "running");
  const errorIndex = steps.findIndex((s) => s.status === "error");

  // Честная полоса — доля ЗАВЕРШЁННЫХ шагов. Идущий шаг за половину не
  // засчитываем: полоса не должна показывать больше, чем видно в списке.
  const pct = hasPlan ? (done / total) * 100 : 0;

  // Текущий шаг для свёрнутого вида: идущий → сбойный → первый неначатый.
  const pendingIndex = steps.findIndex((s) => s.status === "pending");
  const currentIndex = !hasPlan
    ? -1
    : runningIndex >= 0
      ? runningIndex
      : errorIndex >= 0
        ? errorIndex
        : pendingIndex >= 0
          ? pendingIndex
          : total - 1;

  let state: string;
  if (!hasPlan) state = "нет активного плана";
  else if (errorIndex >= 0) state = `ошибка на шаге ${errorIndex + 1}`;
  else if (runningIndex >= 0) state = `шаг ${runningIndex + 1} из ${total}`;
  else if (done === total) state = "готово";
  // Ход оборван или ещё не начат — не врём про «выполняется».
  else state = `${done} из ${total}`;

  const current = currentIndex >= 0 ? steps[currentIndex] : null;

  const busy = runs.filter((r) => r.status === "running").length;

  return (
    <div ref={boxRef} className={cn("border-b border-[var(--color-divider)]", className)}>
      {/* Пульс живёт локально: в globals.css кейфреймов нет, а трогать его
       * задача не разрешает. Правило объявлено внутри media-запроса — при
       * prefers-reduced-motion кейфреймов просто не существует и точка
       * остаётся статичной. */}
      <style>{`
@media (prefers-reduced-motion: no-preference) {
  @keyframes aifa-plan-pulse { 0%, 100% { opacity: 1 } 50% { opacity: .28 } }
  .aifa-plan-dot-running { animation: aifa-plan-pulse 1.6s ease-in-out infinite }
}
      `}</style>

      <button
        type="button"
        disabled={!hasPlan}
        aria-expanded={hasPlan ? open : undefined}
        onClick={() => onOpenChange(!open)}
        title={hasPlan ? (open ? "Свернуть план" : "Развернуть план") : undefined}
        className="flex w-full items-center gap-2 px-4 pt-2.5 pb-1.5 text-left"
      >
        <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-500)]">
          {page === 0 ? "План агента" : "Агенты"}
        </span>
        <span className="min-w-0 truncate text-[10.5px] italic text-[var(--color-neutral-500)]">
          {page === 0
            ? state
            : busy
              ? `${busy} из ${runs.length} работают`
              : `${runs.length} прогонов · завершены`}
        </span>
        {/* Точки страниц — и подсказка, что остров листается вбок. */}
        {hasWorkers && (
          <span className="ml-auto flex shrink-0 items-center gap-1">
            {[0, 1].map((i) => (
              <span
                key={i}
                aria-hidden
                className={cn(
                  "size-[5px] rounded-full transition-colors duration-200 motion-reduce:transition-none",
                  i === page
                    ? "bg-[var(--color-accent)]"
                    : "bg-[var(--color-neutral-300)]",
                )}
              />
            ))}
          </span>
        )}
        {hasPlan && (
          <ChevronDown
            className={cn(
              "size-[13px] shrink-0 text-[var(--color-neutral-600)] transition-transform duration-200 motion-reduce:transition-none",
              !hasWorkers && "ml-auto",
              !open && "-rotate-90",
            )}
          />
        )}
      </button>

      {/* Лента страниц: обе всегда в разметке, видимую выбирает сдвиг — так
          переход виден движением, а не подменой содержимого. */}
      {open && hasWorkers && (
        <div className="overflow-hidden">
          <div
            className="flex w-[200%] transition-transform duration-300 ease-out motion-reduce:transition-none"
            style={{ transform: `translateX(-${page * 50}%)` }}
          >
            <div className="w-1/2 shrink-0">
              {hasPlan ? <LaneLists lanes={laneList} steps={steps} onStepClick={onStepClick} /> : null}
            </div>
            <div className="w-1/2 shrink-0">
              <WorkerBoard runs={runs} />
            </div>
          </div>
        </div>
      )}

      {hasPlan && open && !hasWorkers && (
        <LaneLists lanes={laneList} steps={steps} onStepClick={onStepClick} />
      )}

      {hasPlan && !open && current && (
        <div className="flex items-center gap-2 px-4 pb-2 text-[12px]">
          <span
            aria-hidden
            className={cn(
              "size-[6px] shrink-0 rounded-full",
              current.status === "running" && "aifa-plan-dot-running",
            )}
            style={{ background: DOT_BG[current.status] }}
          />
          <span className="min-w-0 truncate" style={{ color: TITLE_FG[current.status] }}>
            {current.title}
          </span>
        </div>
      )}

      {hasPlan && (
        <div className="relative h-px bg-[var(--color-neutral-300)]">
          <div
            className="absolute inset-y-0 left-0 bg-[var(--color-accent)] transition-[width] duration-200 ease-out motion-reduce:transition-none"
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
    </div>
  );
}

/** Чек-лист шагов хода. Выполненные ЗАЧЁРКНУТЫ — по списку сразу видно, что
 *  уже позади, а не только по подписи справа. */
function PlanList({
  steps,
  onStepClick,
}: {
  steps: PlanStep[];
  onStepClick?: (index: number, step: PlanStep) => void;
}) {
  // Сколько идёт каждый шаг. Времени в событиях плана нет — метки ставит
  // редьюсер на смене статуса (`startedAt`/`endedAt` в PlanStep). Это ровно
  // то, что видит пользователь, и врать тут нечему.
  //
  // 🔴 РАНЬШЕ МЕТКИ ЖИЛИ ЗДЕСЬ, в `useRef` компонента (правка 17.08). Они
  // умирали вместе с ним: переключил чат, вернулся на вкладку, перезагрузил
  // страницу — и у завершённого хода все счётчики пустые, хотя вопрос «сколько
  // потрачено» задают как раз ПОСЛЕ прогона. Плюс запись в ref во время
  // отрисовки — то, на что справедливо ругается react-hooks/refs. Теперь метка
  // одна и лежит в сообщении, вместе с самим шагом.
  const [now, setNow] = useState(() => Date.now());
  // Пока что-то идёт — тикаем раз в секунду, чтобы счётчик рос на глазах.
  const anyRunning = steps.some((s) => s.status === "running");
  useEffect(() => {
    if (!anyRunning) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [anyRunning]);

  const elapsed = (i: number): string => {
    const s = steps[i];
    if (!s?.startedAt) return "";
    // Часы берём из состояния: `Date.now()` прямо в отрисовке — нечистый
    // вызов, и линтер прав, что от него результат прыгает между перерисовками.
    const ms = (s.endedAt ?? now) - s.startedAt;
    const sec = Math.max(0, Math.round(ms / 1000));
    return sec < 60 ? `${sec} с` : `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  };

  return (
    <ol className="flex flex-col gap-1.5 px-4 pb-2">
      {steps.map((s, i) => {
        const row = (
          <>
            <span
              aria-hidden
              className={cn(
                "size-[7px] shrink-0 rounded-full transition-colors duration-300 motion-reduce:transition-none",
                s.status === "running" && "aifa-plan-dot-running",
              )}
              style={{ background: DOT_BG[s.status] }}
            />
            <span className="shrink-0 text-[11.5px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
              {i + 1}
            </span>
            <span
              className={cn(
                "min-w-0 truncate transition-colors duration-300 motion-reduce:transition-none",
                s.status === "done" && "line-through decoration-[1.5px]",
              )}
              style={{ color: TITLE_FG[s.status] }}
            >
              {s.title}
            </span>
            <span className="ml-auto shrink-0 text-[11.5px] tabular-nums text-[var(--color-neutral-500)]">
              {elapsed(i)}
            </span>
            <span className="shrink-0 text-[10.5px] uppercase tracking-[0.09em] text-[var(--color-neutral-500)]">
              {STATUS_LABEL[s.status]}
            </span>
          </>
        );

        return (
          <li key={i}>
            {onStepClick ? (
              <button
                type="button"
                onClick={() => onStepClick(i, s)}
                className="flex w-full items-center gap-2 rounded-[var(--radius-token-sm)] py-1 text-left text-[13.5px] transition-colors duration-150 hover:bg-[var(--color-accent-100)] motion-reduce:transition-none"
              >
                {row}
              </button>
            ) : (
              <div className="flex items-center gap-2 py-1 text-[13.5px]">{row}</div>
            )}
            {s.status === "running" && s.note && (
              <p
                title={s.note}
                className="truncate pl-[24px] text-[12px] text-[var(--color-neutral-500)]"
              >
                {s.note}
              </p>
            )}
          </li>
        );
      })}
    </ol>
  );
}

const WORKER_LABEL: Record<WorkerRun["status"], string> = {
  running: "работает",
  done: "готово",
  error: "ошибка",
  aborted: "прервано",
};

/**
 * Схема параллельных агентов: по строке на прогон, с полосой заполнения.
 * Данные настоящие — каждый воркер шлёт свой поток, отсюда и «сколько из
 * скольких». Если размер набора неизвестен, полосы нет: рисовать проценты
 * от неизвестного значения нельзя.
 */
function WorkerBoard({ runs }: { runs: WorkerRun[] }) {
  return (
    <div className="flex flex-col gap-1.5 px-4 pb-2">
      {runs.map((r) => {
        const pct = r.total > 0 ? Math.min(100, (r.found / r.total) * 100) : null;
        return (
          <div key={r.id} className="flex flex-col gap-[3px]">
            <div className="flex items-center gap-2 text-[12px]">
              <span
                aria-hidden
                className={cn(
                  "size-[6px] shrink-0 rounded-full",
                  r.status === "running" && "aifa-plan-dot-running",
                )}
                style={{
                  background:
                    r.status === "error"
                      ? "var(--destructive)"
                      : r.status === "done"
                        ? "var(--color-accent-700)"
                        : r.status === "aborted"
                          ? "var(--color-neutral-400)"
                          : "var(--color-accent)",
                }}
              />
              <span className="min-w-0 truncate text-[var(--color-text)]">{r.label}</span>
              <span className="ml-auto shrink-0 text-[10px] tabular-nums text-[var(--color-neutral-500)]">
                {r.total > 0 ? `${r.found}/${r.total}` : r.found || ""}
              </span>
              <span className="shrink-0 text-[9.5px] uppercase tracking-[0.11em] text-[var(--color-neutral-500)]">
                {WORKER_LABEL[r.status]}
              </span>
            </div>
            {pct !== null && (
              <div className="relative h-[3px] overflow-hidden rounded-full bg-[var(--color-neutral-200)]">
                <div
                  className="absolute inset-y-0 left-0 rounded-full bg-[var(--color-accent)] transition-[width] duration-300 ease-out motion-reduce:transition-none"
                  style={{ width: `${pct}%` }}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Страница «План»: одна линия — просто список; несколько — группы с
 *  подзаголовками, чтобы видно было ВСЮ задачу, а не последний кусок. */
function LaneLists({
  lanes,
  steps,
  onStepClick,
}: {
  lanes: PlanLane[];
  steps: PlanStep[];
  onStepClick?: (index: number, step: PlanStep) => void;
}) {
  if (lanes.length <= 1) return <PlanList steps={steps} onStepClick={onStepClick} />;
  return (
    <div className="flex flex-col gap-1 pb-1">
      {lanes.map((l) => (
        <div key={l.label}>
          <div className="px-4 pb-0.5 pt-1 text-[9.5px] uppercase tracking-[0.13em] text-[var(--color-neutral-500)]">
            {l.label}
          </div>
          <PlanList steps={l.steps} />
        </div>
      ))}
    </div>
  );
}
