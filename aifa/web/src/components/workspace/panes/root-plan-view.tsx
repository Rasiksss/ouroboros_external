"use client";

import { useState } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/utils";
import type { RootPlanRevision } from "@/lib/root-plan";

/**
 * План, который предложил автор плана: ревизии хода, шаги раскрываются.
 *
 * Это ПРЕДЛОЖЕНИЯ, а не состояние корня: принял ли корень, знает ouroboros. Поэтому на экране нет
 * статусов шагов и слова «принят»; прежняя ревизия подписана «заменена», потому что корень заказал
 * новую. Свернуто по умолчанию: основной остров — «План агента» выше.
 */
export function RootPlanView({ revisions }: { revisions: RootPlanRevision[] }) {
  const [open, setOpen] = useState(false);
  // Выбор ревизии пользователем; пока не выбрал — следуем за текущей (последней).
  const [picked, setPicked] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  if (!revisions.length) return null;
  const shown = revisions.find((r) => r.taskId === picked) ?? revisions[revisions.length - 1];

  return (
    <div className="border-b border-[var(--color-divider)]">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 px-4 pt-2 pb-1.5 text-left"
      >
        <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-500)]">
          План планировщика
        </span>
        <span className="min-w-0 truncate text-[10.5px] italic text-[var(--color-neutral-500)]">
          {revisions.length > 1
            ? `ревизия ${revisions.length}, прежних заменено: ${revisions.length - 1}`
            : `${shown.steps.length} шагов`}
        </span>
        <ChevronDown
          className={cn(
            "ml-auto size-[13px] shrink-0 text-[var(--color-neutral-600)] transition-transform duration-200",
            !open && "-rotate-90",
          )}
        />
      </button>

      {open && (
        <div className="px-4 pb-2">
          {revisions.length > 1 && (
            <div role="tablist" aria-label="Ревизии плана" className="mb-1.5 flex flex-wrap gap-1">
              {revisions.map((r) => (
                <button
                  key={r.taskId}
                  type="button"
                  role="tab"
                  aria-selected={r.taskId === shown.taskId}
                  onClick={() => setPicked(r.taskId)}
                  className={cn(
                    "rounded-[var(--radius-token-sm)] px-2 py-0.5 text-[11px]",
                    r.taskId === shown.taskId
                      ? "bg-[var(--color-accent-100)] text-[var(--color-text)]"
                      : "text-[var(--color-neutral-500)] hover:bg-[var(--color-accent-100)]",
                  )}
                >
                  {r.number}
                  {r.current ? " · текущая" : " · заменена"}
                </button>
              ))}
            </div>
          )}

          {shown.questions.length > 0 && (
            <p className="mb-1 text-[12px] text-[var(--color-neutral-600)]">
              Вопросы: {shown.questions.join(" · ")}
            </p>
          )}

          <ol className="flex flex-col gap-1">
            {shown.steps.map((step, i) => {
              const key = `${shown.taskId}:${step.id}`;
              const isOpen = Boolean(expanded[key]);
              return (
                <li key={key}>
                  <button
                    type="button"
                    aria-expanded={isOpen}
                    onClick={() => setExpanded({ ...expanded, [key]: !isOpen })}
                    className="flex w-full items-center gap-2 py-1 text-left text-[13px] hover:bg-[var(--color-accent-100)]"
                  >
                    <span className="shrink-0 text-[11.5px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                      {i + 1}
                    </span>
                    <span className="min-w-0 truncate text-[var(--color-text)]">{step.objective}</span>
                    <ChevronDown
                      className={cn(
                        "ml-auto size-[12px] shrink-0 text-[var(--color-neutral-500)] transition-transform duration-200",
                        !isOpen && "-rotate-90",
                      )}
                    />
                  </button>
                  {isOpen && (
                    <div className="space-y-0.5 pb-1.5 pl-5 text-[12px] text-[var(--color-neutral-600)]">
                      {step.execution && <p>Исполнение: {step.execution}</p>}
                      {step.depends_on.length > 0 && <p>После: {step.depends_on.join(", ")}</p>}
                      {step.outputs.length > 0 && <p>Результат: {step.outputs.join("; ")}</p>}
                      {step.criteria.map((c) => (
                        <p key={c.id}>
                          Критерий {c.id}: {c.description}
                          {c.verification ? ` (проверка: ${c.verification})` : ""}
                        </p>
                      ))}
                    </div>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </div>
  );
}
