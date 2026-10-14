"use client";

import { useEffect, useState } from "react";
import { Bell, MoreHorizontal, X } from "lucide-react";
import { clearNotices, subscribeNotices, type Notice } from "@/lib/notices";
import { cn } from "@/lib/utils";

/**
 * Шапка 60px: марка, золотая черта, «FINANCIAL ANALYST», через разделитель —
 * название компании открытого документа и строка меты под ним. Справа «…».
 */
export function WorkspaceHeader({
  company,
  meta,
  onMenu,
}: {
  company: string;
  meta: string;
  onMenu?: () => void;
}) {
  return (
    <header className="flex h-[60px] shrink-0 items-center gap-4 border-b border-[var(--color-divider)] px-5">
      <div className="flex items-baseline gap-3">
        <span className="font-heading text-[22px] text-[var(--color-text)]">AIFA</span>
        {/* Черта — пустой flex-элемент, его baseline = нижний край; сдвиг поднимает
            её на оптическую середину строки марки (эталон). */}
        <span
          className="h-px w-6 bg-[var(--color-accent)]"
          style={{ transform: "translateY(-6px)" }}
        />
        <span className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-neutral-600)]">
          Financial Analyst
        </span>
      </div>

      {company && (
        <>
          <span className="h-[26px] w-px bg-[var(--color-divider)]" />
          <div className="min-w-0">
            <div className="font-heading truncate text-[18px] leading-tight text-[var(--color-text)]">
              {company}
            </div>
            {meta && (
              <div className="truncate text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
                {meta}
              </div>
            )}
          </div>
        </>
      )}

      <div className="ml-auto flex items-center gap-2">
        {/* Тумблер «Finengine | Gateway» снят 18.08 (решение Nate): выбор пути
            был нужен на время миграции, монолиты снесены 15.08, путь остался
            один. Значение зафиксировано в use-chat-run — см. комментарий там,
            почему константой, а не просто убранной кнопкой. */}
        <NoticeBell />

      <button
        type="button"
        onClick={onMenu}
        disabled={!onMenu}
        title={onMenu ? "Меню" : "Горячие клавиши и настройки — подключаются следующим шагом"}
        className={
          "grid size-[26px] shrink-0 place-items-center rounded-[var(--radius-token-sm)] transition-colors " +
          (onMenu
            ? "text-[var(--color-neutral-600)] hover:bg-[var(--color-accent-100)] hover:text-[var(--color-accent-700)]"
            : "cursor-not-allowed text-[var(--color-neutral-400)] opacity-60")
        }
      >
        <MoreHorizontal className="size-4" />
        </button>
      </div>
    </header>
  );
}


/**
 * История системных уведомлений. Всплывашка живёт секунды и исчезает — если
 * пользователь отошёл, он никогда не узнает, что случилось. Здесь они лежат.
 */
function NoticeBell() {
  const [items, setItems] = useState<Notice[]>([]);
  const [open, setOpen] = useState(false);

  useEffect(() => subscribeNotices(setItems), []);

  const unseen = items.filter((n) => n.kind !== "info").length;

  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        title="История уведомлений"
        className="relative grid size-[26px] place-items-center rounded-[var(--radius-token-sm)] text-[var(--color-neutral-600)] transition-colors hover:bg-[var(--color-accent-100)] hover:text-[var(--color-accent-700)]"
      >
        <Bell className="size-4" />
        {unseen > 0 && (
          <span className="absolute right-0.5 top-0.5 size-1.5 rounded-full bg-[var(--color-accent)]" />
        )}
      </button>

      {open && (
        <div className="absolute right-0 top-[30px] z-50 w-[360px] rounded-[var(--radius-token-md)] border border-[var(--color-divider)] bg-[var(--color-surface)] shadow-lg">
          <div className="flex items-center gap-2 border-b border-[var(--color-divider)] px-3 py-2">
            <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-600)]">
              Уведомления
            </span>
            <span className="text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
              {items.length}
            </span>
            <button
              type="button"
              onClick={clearNotices}
              className="ml-auto text-[11px] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
            >
              очистить
            </button>
            <button
              type="button"
              onClick={() => setOpen(false)}
              className="text-[var(--color-neutral-500)] transition-colors hover:text-[var(--color-text)]"
            >
              <X className="size-3.5" />
            </button>
          </div>
          <div className="max-h-[320px] overflow-y-auto">
            {items.length === 0 && (
              <p className="px-3 py-3 text-[12px] text-[var(--color-neutral-500)]">
                Пока ничего не происходило.
              </p>
            )}
            {items.map((n) => (
              <div
                key={n.id}
                className="border-b border-[var(--color-divider)] px-3 py-2 last:border-b-0"
              >
                <div className="flex items-baseline gap-2">
                  <span
                    className={cn(
                      "size-1.5 shrink-0 rounded-full",
                      n.kind === "error"
                        ? "bg-[var(--color-accent-700)]"
                        : n.kind === "warn"
                          ? "bg-[var(--color-accent-400)]"
                          : "bg-[var(--color-neutral-400)]",
                    )}
                  />
                  <span className="min-w-0 flex-1 text-[12px] leading-snug text-[var(--color-text)]">
                    {n.text}
                  </span>
                  <span className="shrink-0 text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                    {new Date(n.at).toLocaleTimeString("ru-RU", {
                      hour: "2-digit",
                      minute: "2-digit",
                    })}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
