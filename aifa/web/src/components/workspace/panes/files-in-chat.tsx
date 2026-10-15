"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { listDocuments, type DocumentSummary } from "@/lib/api";
import { deleteProject, type Project } from "@/lib/projects";
import { useAuth } from "@/components/auth-context";
import { useWorkspace } from "@/components/workspace/workspace-context";
import { cn } from "@/lib/utils";

/**
 * Меню чата («…» над первой репликой) и панель «Файлы в чате».
 *
 * Работают два пункта — «Файлы в чате» (список документов контекста, клик
 * открывает документ в панели документа) и «Удалить чат». Остальные пункты
 * эталона (перенести в проект, переименовать, дублировать) выключены и честно
 * подписаны «скоро» — рисовать живую кнопку под несуществующее действие нельзя.
 *
 * ⚠️ Почему удаление подключили 11.08.2026. На /v2 его не было вовсе:
 * deleteProject звался только со старой страницы. Владелец за месяцы набрал
 * 168 чатов, localStorage переполнился, загрузка документов встала — и почистить
 * из интерфейса он не мог физически, сообщение «удалите старые чаты» отправляло
 * его туда, где кнопки нет. Чистить за него по ssh — не починка.
 */

/**
 * WorkspaceProvider на /v2 ещё не смонтирован (страницу этот шаг не трогает), а
 * useWorkspace() вне провайдера бросает. Панель из-за этого падать не должна:
 * нет контекста — пункт «открыть документ» честно недоступен.
 * Порядок хуков стабилен: useWorkspace всегда делает ровно один useContext.
 */
export function useWorkspaceSafe() {
  try {
    return useWorkspace();
  } catch {
    return null;
  }
}

function extOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0 || dot === filename.length - 1) return "FILE";
  return filename.slice(dot + 1).toUpperCase().slice(0, 4);
}

/** Подпись под именем файла. Число страниц у бэкенда есть не всегда
 *  (IDP-ветка загрузки его не проставляет) — молчим вместо выдумки. */
function metaOf(doc: DocumentSummary): string {
  const kind = extOf(doc.filename);
  return doc.pages > 0 ? `${kind} · ${doc.pages} стр.` : kind;
}

export function ChatDotsMenu({
  project,
  onDeleted,
}: {
  project: Project | null;
  /** Чат удалён — странице надо снять выбор, иначе экран останется на
   *  несуществующем id и сторож напишет в журнал ложную «пропажу чата». */
  onDeleted?: (id: string) => void;
}) {
  const ws = useWorkspaceSafe();
  const { user } = useAuth();
  const [menuOpen, setMenuOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [filesOpen, setFilesOpen] = useState(false);
  const [allDocs, setAllDocs] = useState<DocumentSummary[]>([]);
  const [docsError, setDocsError] = useState<string>("");
  const boxRef = useRef<HTMLDivElement | null>(null);

  const docIds = project?.doc_ids;
  // Фильтруем в рендере, а не в стейте: иначе при переключении чата список
  // держал бы чужие файлы до ответа сети.
  const docs = useMemo(
    () => (docIds ? allDocs.filter((d) => docIds.includes(d.doc_id)) : []),
    [allDocs, docIds],
  );

  useEffect(() => {
    if (!docIds) return;
    let cancelled = false;
    listDocuments()
      .then((all) => {
        if (cancelled) return;
        setDocsError("");
        setAllDocs(all);
      })
      .catch((e: Error) => {
        if (cancelled) return;
        setDocsError(e.message || "не удалось загрузить список файлов");
      });
    return () => {
      cancelled = true;
    };
  }, [docIds]);

  // Клик мимо меню закрывает его. Панель файлов закрывается только крестиком —
  // по ней кликают, и автозакрытие мешало бы.
  useEffect(() => {
    if (!menuOpen) {
      // Закрыли меню — подтверждение удаления сбрасывается. Иначе следующее
      // открытие встречало бы пользователя уже взведённой красной кнопкой.
      setConfirmDelete(false);
      return;
    }
    const onDown = (e: MouseEvent) => {
      if (!boxRef.current?.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  if (!project) return null;

  return (
    <>
      <div ref={boxRef} className="absolute right-3 top-1 z-10">
        <button
          type="button"
          title="Действия с чатом"
          onClick={() => setMenuOpen((v) => !v)}
          className="px-1.5 py-0.5 text-[15px] leading-none tracking-[0.06em] text-[var(--color-neutral-500)] transition-colors hover:text-[var(--color-accent-700)]"
        >
          …
        </button>

        {menuOpen && (
          <div className="absolute right-0 top-7 flex w-[262px] flex-col rounded-[var(--radius-token-md)] border border-[var(--color-divider)] bg-[var(--color-bg)] p-1 shadow-lg">
            <MenuItem label="Перенести в проект" />
            <button
              type="button"
              onClick={() => {
                setMenuOpen(false);
                setFilesOpen(true);
              }}
              className="flex items-baseline gap-2 px-2.5 py-2 text-left text-[13px] text-[var(--color-text)] transition-colors hover:bg-[var(--color-accent-100)]"
            >
              Файлы в чате
              <span className="ml-auto text-[11px] tabular-nums text-[var(--color-neutral-500)]">
                {docs.length}
              </span>
            </button>
            <MenuItem label="Переименовать" />
            <MenuItem label="Дублировать диалог" />
            <div className="my-1 h-px bg-[var(--color-divider)]" />
            {confirmDelete ? (
              // Подтверждение прямо в меню, а не окном: удаление необратимо
              // (надгробие живёт 30 дней и уезжает на сервер), но и лишнего
              // диалога на один клик разводить незачем.
              <button
                type="button"
                onClick={() => {
                  if (!user || !project) return;
                  const id = project.id;
                  deleteProject(user.username, id);
                  setMenuOpen(false);
                  setConfirmDelete(false);
                  onDeleted?.(id);
                  window.dispatchEvent(new Event("finengine:projects"));
                }}
                className="flex items-baseline gap-2 px-2.5 py-2 text-left text-[13px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)]"
              >
                Удалить безвозвратно
                <span className="ml-auto text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
                  подтвердить
                </span>
              </button>
            ) : (
              <button
                type="button"
                disabled={!project || !user}
                onClick={() => setConfirmDelete(true)}
                className="flex items-baseline gap-2 px-2.5 py-2 text-left text-[13px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)] disabled:cursor-not-allowed disabled:opacity-55"
              >
                Удалить чат
              </button>
            )}
          </div>
        )}
      </div>

      {filesOpen && (
        <div className="absolute inset-0 z-20 flex flex-col bg-[var(--color-bg)]">
          <div className="flex shrink-0 items-baseline gap-2 border-b border-[var(--color-divider)] p-3">
            <span className="font-heading text-[17px] text-[var(--color-text)]">
              Файлы в чате
            </span>
            <span className="text-[10px] uppercase tracking-[0.12em] tabular-nums text-[var(--color-neutral-500)]">
              {docs.length}
            </span>
            <button
              type="button"
              onClick={() => setFilesOpen(false)}
              title="Закрыть"
              className="ml-auto px-1.5 text-[15px] leading-none text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
            >
              ✕
            </button>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
            {docsError && (
              <p className="py-2 text-[12px] text-[var(--color-accent-700)]">
                Список файлов не загрузился: {docsError}
              </p>
            )}
            {!docsError && docs.length === 0 && (
              <p className="py-2 text-[12.5px] text-[var(--color-neutral-500)]">
                В этом чате нет прикреплённых файлов.
              </p>
            )}
            {docs.map((d) => (
              <button
                key={d.doc_id}
                type="button"
                disabled={!ws}
                title={
                  ws
                    ? `Открыть ${d.filename} в панели документа`
                    : "Открытие документа отсюда подключается вместе с общим состоянием экрана"
                }
                onClick={() => {
                  ws?.setActiveDocId(d.doc_id);
                  setFilesOpen(false);
                }}
                className={cn(
                  "flex w-full items-baseline gap-2 border-b border-[var(--color-divider)] px-1 py-2 text-left transition-all",
                  ws
                    ? "hover:bg-[var(--color-accent-100)] hover:pl-2.5"
                    : "cursor-not-allowed opacity-60",
                )}
              >
                <span className="flex h-[26px] w-[22px] shrink-0 items-center justify-center rounded-[2px] border border-[var(--color-accent)] text-[7.5px] tracking-[0.06em] text-[var(--color-accent-700)]">
                  {extOf(d.filename)}
                </span>
                <span className="min-w-0">
                  <span className="block text-[12.5px] leading-tight text-[var(--color-text)]">
                    {d.company_name || d.filename}
                  </span>
                  <span className="mt-0.5 block text-[10px] uppercase tracking-[0.1em] tabular-nums text-[var(--color-neutral-500)]">
                    {metaOf(d)}
                  </span>
                </span>
              </button>
            ))}
          </div>

          <div className="shrink-0 border-t border-[var(--color-divider)] p-3 text-[10.5px] italic text-[var(--color-neutral-500)]">
            клик по файлу открывает его в панели документа
          </div>
        </div>
      )}
    </>
  );
}

/** Пункт меню, за которым ещё нет действия. Честно выключен и подписан. */
function MenuItem({ label, danger }: { label: string; danger?: boolean }) {
  return (
    <span
      title="Пока не подключено"
      className={cn(
        "flex cursor-not-allowed items-baseline gap-2 px-2.5 py-2 text-[13px] opacity-55",
        danger ? "text-[var(--color-accent-700)]" : "text-[var(--color-text)]",
      )}
    >
      {label}
      <span className="ml-auto text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
        скоро
      </span>
    </span>
  );
}
