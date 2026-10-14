"use client";

import { useState } from "react";
import { ChevronDown, SlidersHorizontal } from "lucide-react";
import { cn } from "@/lib/utils";

export const RAIL_WIDTH = 232;

export type RailItem = {
  id: string;
  label: string;
  badge?: string | number;
  dot?: string;
  /** Пункт есть, но открыть его пока нельзя — вместе с hint показываем честно. */
  disabled?: boolean;
  hint?: string;
};
export type RailGroup = { id: string; title: string; items: RailItem[]; hint?: string };

/**
 * Левый рельс 232px: «Новый чат», группы (Автоматизации / Проекты / Чаты) со
 * сворачиванием и иконкой настроек группировки, внизу — пользователь.
 */
export function WorkspaceRail({
  groups,
  activeId,
  user,
  onNewChat,
  onPick,
  onGroupSettings,
  onUserMenu,
}: {
  groups: RailGroup[];
  activeId?: string;
  user: { initials: string; name: string; role: string };
  onNewChat: () => void;
  onPick: (groupId: string, itemId: string) => void;
  onGroupSettings?: (groupId: string) => void;
  onUserMenu?: () => void;
}) {
  const [closed, setClosed] = useState<Record<string, boolean>>({});

  return (
    <aside
      style={{ width: RAIL_WIDTH }}
      className="flex shrink-0 flex-col border-r border-[var(--color-divider)] bg-[var(--color-bg)]"
    >
      <div className="p-3">
        <button
          type="button"
          onClick={onNewChat}
          className="w-full rounded-[var(--radius-token-md)] border border-[var(--color-accent)] px-3 py-2 text-sm text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)]"
        >
          Новый чат
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        {groups.map((g) => {
          const isClosed = !!closed[g.id];
          return (
            <div key={g.id} className="mb-4">
              <div className="flex items-center gap-1.5 py-1">
                <button
                  type="button"
                  onClick={() => setClosed((s) => ({ ...s, [g.id]: !s[g.id] }))}
                  className="flex flex-1 items-center gap-1.5 text-left"
                >
                  <ChevronDown
                    className={cn(
                      "size-3 text-[var(--color-neutral-500)] transition-transform",
                      isClosed && "-rotate-90",
                    )}
                  />
                  <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-600)]">
                    {g.title}
                  </span>
                  {g.hint && (
                    <span className="text-[10px] text-[var(--color-neutral-500)] italic">
                      {g.hint}
                    </span>
                  )}
                </button>
                <button
                  type="button"
                  title={onGroupSettings ? "Группировка и сортировка" : "Группировка и сортировка — подключается следующим шагом"}
                  disabled={!onGroupSettings}
                  onClick={() => onGroupSettings?.(g.id)}
                  className={cn(
                    "grid size-[18px] shrink-0 place-items-center transition-colors",
                    onGroupSettings
                      ? "text-[var(--color-neutral-500)] hover:text-[var(--color-accent-700)]"
                      : "cursor-not-allowed text-[var(--color-neutral-400)] opacity-60",
                  )}
                >
                  <SlidersHorizontal className="size-3.5" />
                </button>
              </div>

              {!isClosed && (
                <div className="mt-0.5">
                  {g.items.map((it) => (
                    <button
                      key={it.id}
                      type="button"
                      onClick={() => !it.disabled && onPick(g.id, it.id)}
                      disabled={it.disabled}
                      title={it.hint}
                      className={cn(
                        "flex w-full items-center gap-2 rounded-[var(--radius-token-sm)] px-2 py-1.5 text-left text-[13px] transition-colors",
                        it.disabled
                          ? "cursor-not-allowed text-[var(--color-neutral-500)] opacity-60"
                          : activeId === it.id
                            ? "bg-[var(--color-accent-100)] text-[var(--color-accent-700)]"
                            : "text-[var(--color-text)] hover:bg-[var(--color-accent-100)]",
                      )}
                    >
                      {it.dot && (
                        <span
                          className="size-1.5 shrink-0 rounded-full"
                          style={{ background: it.dot }}
                        />
                      )}
                      <span className="min-w-0 flex-1 truncate">{it.label}</span>
                      {it.badge !== undefined && (
                        <span className="shrink-0 text-[11px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                          {it.badge}
                        </span>
                      )}
                    </button>
                  ))}
                  {!g.items.length && (
                    <div className="px-2 py-1.5 text-[12px] text-[var(--color-neutral-500)]">
                      пусто
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <button
        type="button"
        onClick={onUserMenu}
        disabled={!onUserMenu}
        title={onUserMenu ? undefined : "Меню пользователя — подключается следующим шагом"}
        className={cn(
          "flex items-center gap-2 border-t border-[var(--color-divider)] px-3 py-3 text-left transition-colors",
          onUserMenu ? "hover:bg-[var(--color-accent-100)]" : "cursor-not-allowed",
        )}
      >
        <span className="grid size-6 shrink-0 place-items-center rounded-full border border-[var(--color-divider)] text-[10px] text-[var(--color-neutral-700)]">
          {user.initials}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[12.5px] text-[var(--color-text)]">{user.name}</span>
          <span className="block truncate text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
            {user.role}
          </span>
        </span>
        <ChevronDown className="size-3.5 shrink-0 text-[var(--color-neutral-500)]" />
      </button>
    </aside>
  );
}
