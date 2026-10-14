"use client";

import {
  summarizeTrace,
  type AttemptStatus,
  type AuthorAttempt,
  type DynamicTrace,
  type MoveAction,
  type TraceMove,
} from "@/lib/dynamic-run-trace";
import { cn } from "@/lib/utils";

/**
 * Вкладка «Планировщик и автор» рядом с графом исполнения: по ходам видно, что решил planner
 * (цель, причина, шаги, новые атомы), что написал автор кода (каждая попытка, ошибка самопроверки,
 * код) и чем ход кончился. Компонент только рисует — слияние событий в `lib/dynamic-run-trace.ts`.
 */

const ACTION_LABEL: Record<MoveAction, string> = {
  execute: "выполнить",
  finalize: "ответить",
  give_up: "отказаться",
  rejected: "ответ не принят",
  entry: "стандартный граф упал",
};

const ATTEMPT_LABEL: Record<AttemptStatus, string> = {
  ok: "код принят",
  error: "самопроверка не прошла",
  bad_format: "ответ не в формате",
  no_response: "модель не ответила",
  station_fault: "станция недоступна",
};

const STYLES = `
.aifa-tr {
  --tr-success: #2f9e5c;
  --tr-run: #3b7de0;
  --tr-fail: var(--destructive);
}
:root[data-theme="terminal"] .aifa-tr { --tr-success: #3ecb7e; --tr-run: #5b9dff; }
.aifa-tr-card { background: var(--color-surface); border: 1px solid var(--color-divider); border-radius: 10px; }
.aifa-tr-card[data-move-status="running"] {
  border-color: var(--tr-run);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--tr-run) 14%, transparent);
}
.aifa-tr-card[data-move-status="rejected"] { border-style: dashed; border-color: var(--tr-fail); }
.aifa-tr-actor {
  font-size: 10px; letter-spacing: .12em; text-transform: uppercase; border-radius: 9999px; padding: 1px 8px;
  border: 1px solid var(--color-divider); color: var(--color-neutral-600);
}
.aifa-tr-attempt[data-attempt-status="ok"] { border-color: color-mix(in srgb, var(--tr-success) 55%, var(--color-divider)); }
.aifa-tr-attempt:not([data-attempt-status="ok"]) { border-color: color-mix(in srgb, var(--tr-fail) 55%, var(--color-divider)); }
.aifa-tr pre {
  white-space: pre-wrap; word-break: break-word; font-size: 11.5px; line-height: 1.45;
  background: color-mix(in srgb, var(--color-neutral-400) 12%, transparent); border-radius: 6px; padding: 8px;
}
`;

function AttemptRow({ attempt }: { attempt: AuthorAttempt }) {
  return (
    <details
      className="aifa-tr-attempt rounded-md border px-2.5 py-1.5"
      data-attempt-status={attempt.status}
      open={attempt.status !== "ok"}
    >
      <summary className="cursor-pointer select-none text-[12px]">
        <span className="font-medium">Попытка {attempt.attempt}</span>
        <span className="text-[var(--color-neutral-600)]"> — {ATTEMPT_LABEL[attempt.status]}</span>
      </summary>
      {attempt.error ? <pre className="mt-1.5 text-[var(--tr-fail)]">{attempt.error}</pre> : null}
      {attempt.code ? (
        <>
          <div className="mt-1.5 text-[10.5px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)]">код</div>
          <pre>{attempt.code}</pre>
        </>
      ) : null}
    </details>
  );
}

function AuthorBlock({ atom, attempts }: { atom: string; attempts: AuthorAttempt[] }) {
  return (
    <div className="mt-2.5 space-y-1.5 border-l-2 border-[var(--color-divider)] pl-3" data-author-atom={atom}>
      <div className="flex items-center gap-2">
        <span className="aifa-tr-actor">Автор</span>
        <span className="font-mono text-[12px]">{atom}</span>
      </div>
      {attempts.map((attempt) => (
        <AttemptRow key={`${attempt.atom}-${attempt.attempt}`} attempt={attempt} />
      ))}
    </div>
  );
}

function groupByAtom(attempts: AuthorAttempt[]): [string, AuthorAttempt[]][] {
  const groups = new Map<string, AuthorAttempt[]>();
  for (const attempt of attempts) groups.set(attempt.atom, [...(groups.get(attempt.atom) ?? []), attempt]);
  return [...groups.entries()];
}

function MoveCard({
  item,
  graphCount,
  onSelectGraphMove,
}: {
  item: TraceMove;
  graphCount: number;
  onSelectGraphMove?: (move: number) => void;
}) {
  const terminal = item.action === "finalize" || item.action === "give_up";
  const entry = item.action === "entry";
  const failedOutcome = item.outcome && !item.outcome.startsWith("✓");
  return (
    <section
      className="aifa-tr-card px-3.5 py-3"
      data-move={item.move}
      data-move-status={item.status}
      data-move-action={item.action}
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[12px] font-medium">{entry ? "Вход в цикл" : `Ход ${item.move}`}</span>
        <span className="aifa-tr-actor">{entry ? "Стандартный граф" : "Планировщик"}</span>
        <span className="text-[12px] text-[var(--color-neutral-600)]">{ACTION_LABEL[item.action]}</span>
        {item.status === "running" ? <span className="text-[11px] text-[var(--tr-run)]">исполняется…</span> : null}
      </div>

      {item.objective ? <div className="mt-1.5 text-[13px] font-medium">{item.objective}</div> : null}
      {item.reason ? <div className="mt-0.5 text-[12px] text-[var(--color-neutral-600)]">{item.reason}</div> : null}

      {item.goals.length ? (
        <ul className="mt-2 list-disc pl-5 text-[12px]">
          {item.goals.map((part) => (
            <li key={part.goal_ref} data-move-goal={part.goal_ref}>
              часть цели {part.goal_ref}: {part.description}
            </li>
          ))}
        </ul>
      ) : null}

      {item.scenarios.length ? (
        <ul className="mt-2 list-disc pl-5 text-[12px]">
          {item.scenarios.map((text) => (
            <li key={text}>сценарий AIFA: {text}</li>
          ))}
        </ul>
      ) : null}

      {item.newAtoms.map((atom) => (
        <div key={atom.name} className="mt-2 rounded-md border border-[var(--color-divider)] px-2.5 py-1.5 text-[12px]" data-new-atom={atom.name}>
          <span className="font-mono">{atom.name}</span>
          <span className="text-[var(--color-neutral-600)]"> · новый атом · {atom.kind === "download" ? "загрузка" : "код"}</span>
          <div className="text-[var(--color-neutral-600)]">{atom.purpose}</div>
          {atom.url ? <div className="break-all font-mono text-[11px] text-[var(--color-neutral-500)]">{atom.url}</div> : null}
        </div>
      ))}

      {item.steps.length ? (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {item.steps.map((step, index) => (
            <span key={`${step.atom}-${index}`} className="rounded-full border border-[var(--color-divider)] px-2 py-[1px] font-mono text-[11px]">
              {[step.atom, step.concept, step.period_ref].filter(Boolean).join(" · ")}
            </span>
          ))}
        </div>
      ) : null}

      {groupByAtom(item.attempts).map(([atom, attempts]) => (
        <AuthorBlock key={atom} atom={atom} attempts={attempts} />
      ))}

      {item.outcome ? (
        <div className={cn("mt-2.5 text-[12px] font-medium", failedOutcome ? "text-[var(--tr-fail)]" : "text-[var(--tr-success)]")}>
          {item.outcome}
        </div>
      ) : null}

      {item.details ? (
        terminal || item.status === "rejected" ? (
          <p className={cn("mt-2 whitespace-pre-wrap text-[12.5px]", item.status === "rejected" && "text-[var(--tr-fail)]")}>
            {item.details}
          </p>
        ) : (
          <details className="mt-1.5 text-[12px]">
            <summary className="cursor-pointer select-none text-[var(--color-neutral-600)]">
              {entry ? "итог узлов первой попытки" : "итог хода подробно"}
            </summary>
            <pre>{item.details}</pre>
          </details>
        )
      ) : null}
      {graphCount > 0 ? (
        <button
          type="button"
          className="mt-2 text-[11px] font-medium text-[var(--tr-run)]"
          data-move-graphs={graphCount}
          onClick={() => onSelectGraphMove?.(item.move)}
        >
          Показать {graphCount === 1 ? "граф" : `графы: ${graphCount}`}
        </button>
      ) : null}
    </section>
  );
}

export function DynamicRunTraceView({
  trace,
  live,
  graphCounts = {},
  onSelectGraphMove,
}: {
  trace: DynamicTrace;
  live: boolean;
  graphCounts?: Record<number, number>;
  onSelectGraphMove?: (move: number) => void;
}) {
  const summary = summarizeTrace(trace);
  if (summary.state === "empty") return null;

  const state =
    summary.state === "finished" ? "ответ получен" : live ? "идёт" : "прогон завершён";
  return (
    <div className="aifa-tr flex h-full min-h-0 flex-col" data-trace-state={summary.state}>
      <style>{STYLES}</style>
      <div className="border-b border-[var(--color-divider)] px-4 pb-2.5 pt-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-[var(--color-neutral-600)]">
          <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-500)]">Планировщик и автор</span>
          <span>ходов: {summary.moves}</span>
          {summary.rejected ? <span>не принято: {summary.rejected}</span> : null}
          <span>попыток автора: {summary.authorAttempts}</span>
          <span className="font-medium">{state}</span>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
        {trace.plan?.length ? (
          <section className="aifa-tr-card px-3.5 py-3" data-trace-plan={trace.plan.length}>
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-[12px] font-medium">План</span>
              <span className="aifa-tr-actor">Планировщик</span>
            </div>
            <ol className="mt-2 list-decimal pl-5 text-[12.5px]">
              {trace.plan.map((part, index) => (
                <li key={`${index}-${part}`}>{part}</li>
              ))}
            </ol>
            {trace.files?.length ? (
              <div className="mt-2 text-[12px] text-[var(--color-neutral-600)]">Файл в ответе: {trace.files.join("; ")}</div>
            ) : null}
          </section>
        ) : null}
        {trace.moves.map((item) => (
          <MoveCard
            key={item.move}
            item={item}
            graphCount={graphCounts[item.move] ?? 0}
            onSelectGraphMove={onSelectGraphMove}
          />
        ))}
      </div>
    </div>
  );
}
