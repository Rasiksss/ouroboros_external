"use client";

import { useEffect, useRef, useState } from "react";
import { Check } from "lucide-react";
import { toast } from "sonner";

import { DOC_TYPES, patchDocumentType } from "@/lib/api";
import { docTitle, useWorkspace } from "../workspace-context";
import { cn } from "@/lib/utils";

/**
 * Выбор документа и его вида — панель, раскрывающаяся ИЗ ВКЛАДКИ «Документы».
 *
 * ПОЧЕМУ ЗДЕСЬ, А НЕ ОТДЕЛЬНОЙ СТРОКОЙ (правка Nate 18.08). Сначала список
 * документов и выпадашка вида стояли строкой над поиском. Владелец назвал это
 * лишней кнопкой: выбор относится к тому, что написано на вкладке, там ему и
 * место. Шапка зоны освободилась под поиск целиком.
 *
 * Вид правится ЗДЕСЬ ЖЕ, у своего документа: он ставится при загрузке
 * автоматически и промахивается (годовой отчёт приезжает «прочим»), а от него
 * зависит выбор ячейки реестра. Экран меняется ПОСЛЕ ответа ручки —
 * оптимистичная отрисовка при отказе показала бы вид, который не сохранился.
 */
export function DocPicker({ onClose }: { onClose: () => void }) {
  const { docs, setDocs, activeDocId, setActiveDocId, activeDoc } = useWorkspace();
  const [saving, setSaving] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const changeType = async (docType: string) => {
    if (!activeDoc) return;
    setSaving(true);
    try {
      await patchDocumentType(activeDoc.doc_id, docType);
      setDocs(docs.map((d) => (d.doc_id === activeDoc.doc_id ? { ...d, doc_type: docType } : d)));
    } catch (e) {
      toast.error(
        `Не удалось сохранить вид документа: ${e instanceof Error ? e.message : String(e)}`,
      );
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      ref={boxRef}
      role="listbox"
      className="absolute inset-x-0 top-full z-40 max-h-[360px] overflow-y-auto rounded-b-[var(--radius-token-md)] border border-t-0 border-[var(--color-divider)] bg-[var(--color-surface)] shadow-lg"
    >
      {docs.map((d) => (
        <button
          key={d.doc_id}
          type="button"
          role="option"
          aria-selected={d.doc_id === activeDocId}
          onClick={() => {
            setActiveDocId(d.doc_id);
            onClose();
          }}
          className={cn(
            "flex w-full items-baseline gap-2 px-4 py-1.5 text-left transition-colors hover:bg-[var(--color-accent-100)]",
            d.doc_id === activeDocId && "bg-[var(--color-accent-100)]",
          )}
        >
          <span className="min-w-0 flex-1 truncate text-[12.5px] text-[var(--color-text)]">
            {docTitle(d)}
          </span>
          {(d.period || d.pages) && (
            <span className="shrink-0 text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
              {[d.period, d.pages ? `${d.pages} стр.` : ""].filter(Boolean).join(" · ")}
            </span>
          )}
        </button>
      ))}

      {activeDoc && (
        <div className="border-t border-[var(--color-divider)] px-4 py-2">
          <p className="mb-1 text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)]">
            вид документа {saving && "· сохраняю…"}
          </p>
          <div className="flex flex-wrap gap-1">
            {DOC_TYPES.map((t) => {
              const on = (activeDoc.doc_type || "") === t.id;
              return (
                <button
                  key={t.id}
                  type="button"
                  disabled={saving}
                  onClick={() => void changeType(t.id)}
                  className={cn(
                    "flex items-center gap-1 rounded-[var(--radius-token-sm)] border px-2 py-0.5 text-[11.5px] transition-colors disabled:opacity-50",
                    on
                      ? "border-[var(--color-accent)] text-[var(--color-accent-700)]"
                      : "border-[var(--color-divider)] text-[var(--color-neutral-600)] hover:border-[var(--color-accent)]",
                  )}
                >
                  {on && <Check className="size-3" />}
                  {t.label}
                </button>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}
