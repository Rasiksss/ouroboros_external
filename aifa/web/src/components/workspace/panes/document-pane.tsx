"use client";

import { useEffect, useRef, useState } from "react";
import { Search, Upload, X } from "lucide-react";
import { documentFileUrl, listDocuments } from "@/lib/api";
import { fileKindOf } from "@/lib/file-kind";
import { useWorkspace } from "@/components/workspace/workspace-context";
import {
  uploadItemLabel,
  useDocUpload,
  type UploadItem,
} from "@/components/workspace/use-doc-upload";
import type { Project } from "@/lib/projects";
import { cn } from "@/lib/utils";
import { PageRibbon } from "./page-ribbon";

/**
 * Панель исходного документа: вкладки документов проекта (листаются колесом,
 * стрелок нет), поиск по документу, полоса «источник метрики» и ниже —
 * непрерывная лента страниц. Сюда же можно перетащить PDF.
 *
 * Активный документ живёт не здесь, а в общем контексте рабочего экрана: от
 * него зависят шапка и вкладки извлечённого, поэтому переключение вкладки
 * обязано идти через setActiveDocId.
 */
export function DocumentPane({ project }: { project: Project | null }) {
  const { docs, setDocs, activeDocId, setActiveDocId, activeDoc, sourceTarget } = useWorkspace();
  const tabsRef = useRef<HTMLDivElement | null>(null);
  const [query, setQuery] = useState("");

  const upload = useDocUpload(project);

  // Подсветка зоны перетаскивания. Считаем ГЛУБИНУ входов: dragenter/dragleave
  // стреляют на каждом вложенном элементе, и без счётчика подсветка мигала бы
  // при проходе курсора над вкладками и страницами.
  const [dragOver, setDragOver] = useState(false);
  const dragDepth = useRef(0);
  const dragHasFiles = (e: React.DragEvent) => e.dataTransfer.types.includes("Files");

  // Активный документ читаем через ref: положить его в зависимости эффекта
  // значило бы перезапрашивать список документов на каждое переключение вкладки.
  const activeIdRef = useRef("");
  useEffect(() => {
    activeIdRef.current = activeDocId;
  }, [activeDocId]);

  useEffect(() => {
    if (!project) {
      setDocs([]);
      setActiveDocId("");
      return;
    }
    let cancelled = false;
    listDocuments()
      .then((all) => {
        if (cancelled) return;
        const ids = new Set(project.doc_ids);
        const mine = all.filter((d) => ids.has(d.doc_id));
        setDocs(mine);
        // Список мог смениться под ногами — тогда активным становится первый.
        if (!mine.some((d) => d.doc_id === activeIdRef.current)) {
          setActiveDocId(mine[0]?.doc_id ?? "");
        }
      })
      .catch(() => {
        // ГРАБЛЯ: раньше сбой запроса обнулял и список, и выбранный документ.
        // Эффект перезапускается на КАЖДУЮ смену идентичности project — а она
        // случается при возврате на вкладку (подтяжка с сервера перечитывает
        // хранилище). Одна неудачная сетевая попытка — и зона «Документ»
        // писала «Документ не выбран» поверх живых данных. Ничего не трогаем:
        // список не обновился, значит прошлый по-прежнему верен.
      });
    return () => {
      cancelled = true;
    };
  }, [project, setDocs, setActiveDocId]);

  // Вкладки листаются колесом — стрелок в эталоне нет. Перехватываем колесо
  // только когда ленте вкладок действительно есть куда ехать.
  useEffect(() => {
    const el = tabsRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (el.scrollWidth <= el.clientWidth) return;
      if (Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;
      e.preventDefault();
      el.scrollLeft += e.deltaY;
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [docs.length]);

  // Переход к странице-источнику адресован конкретному документу.
  const target = sourceTarget && sourceTarget.docId === activeDocId ? sourceTarget : null;
  const targetPage = target && target.page >= 1 ? target.page : 0;
  const fileUrl = activeDoc ? documentFileUrl(activeDoc.doc_id) : "";
  // total_pages бэкенд заполняет только в pymupdf-ветке разбора; после
  // облачного OCR приходит 0. Врать «0 стр.» и рисовать пустую ленту нельзя.
  const totalPages = activeDoc?.pages ?? 0;

  return (
    <div
      className="relative flex h-full flex-col"
      onDragEnter={(e) => {
        if (!dragHasFiles(e)) return;
        e.preventDefault();
        dragDepth.current += 1;
        setDragOver(true);
      }}
      onDragOver={(e) => {
        if (!dragHasFiles(e)) return;
        // Без preventDefault браузер не считает зону принимающей и вместо
        // события drop просто откроет файл вместо страницы.
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(e) => {
        if (!dragHasFiles(e)) return;
        dragDepth.current -= 1;
        if (dragDepth.current <= 0) {
          dragDepth.current = 0;
          setDragOver(false);
        }
      }}
      onDrop={(e) => {
        if (!dragHasFiles(e)) return;
        e.preventDefault();
        dragDepth.current = 0;
        setDragOver(false);
        void upload.upload(Array.from(e.dataTransfer.files));
      }}
    >
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-20 grid place-items-center border-2 border-dashed border-[var(--color-accent)] bg-[var(--color-accent-100)]">
          <div className="flex flex-col items-center gap-1 px-6 text-center">
            <Upload className="size-6 text-[var(--color-accent-700)]" />
            <span className="text-[13px] text-[var(--color-accent-700)]">
              {project
                ? "Отпустите — загрузим документ в этот чат"
                : "Сначала выберите чат слева — документ некуда прикладывать"}
            </span>
            <span className="text-[11px] text-[var(--color-accent-700)]">только PDF</span>
          </div>
        </div>
      )}

      <div
        ref={tabsRef}
        className="flex shrink-0 gap-0 overflow-x-auto border-b border-[var(--color-divider)] [scrollbar-width:none]"
      >
        {docs.map((d) => {
          const isActive = d.doc_id === activeDocId;
          const ext = d.filename.split(".").pop()?.toUpperCase() ?? "";
          return (
            <button
              key={d.doc_id}
              type="button"
              onClick={() => setActiveDocId(d.doc_id)}
              title={d.filename}
              style={{ width: 118 }}
              className={cn(
                "shrink-0 border-b-2 px-2 py-[7px] text-left transition-colors",
                isActive
                  ? "border-[var(--color-accent)]"
                  : "border-transparent hover:bg-[var(--color-neutral-200)]",
              )}
            >
              <span
                className={cn(
                  "block truncate text-[11.5px] leading-tight",
                  isActive ? "text-[var(--color-text)]" : "text-[var(--color-neutral-600)]",
                )}
              >
                {d.company_name || d.filename}
              </span>
              <span className="mt-[2px] block truncate text-[9.5px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                {d.pages > 0 ? `${ext} · ${d.pages} стр.` : ext}
              </span>
            </button>
          );
        })}
        {!docs.length && (
          <span className="px-3 py-3 text-[12px] text-[var(--color-neutral-500)]">
            {project ? "в этом проекте нет документов" : "проект не выбран"}
          </span>
        )}
      </div>

      {upload.items.length > 0 && (
        <div className="flex shrink-0 flex-col gap-1 border-b border-[var(--color-divider)] px-3 py-1.5">
          {upload.items.map((it) => (
            <UploadRow key={it.id} item={it} onDismiss={upload.dismiss} />
          ))}
        </div>
      )}

      <div className="flex shrink-0 flex-col gap-1.5 border-b border-[var(--color-divider)] px-3 py-2">
        <div className="flex items-center gap-2">
          <Search className="size-3.5 shrink-0 text-[var(--color-neutral-500)]" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Поиск по документу"
            className="min-w-0 flex-1 border-0 border-b border-[var(--color-divider)] bg-transparent py-[3px] text-[12px] outline-none placeholder:text-[var(--color-neutral-500)]"
          />
          {query.trim() && (
            <span className="shrink-0 text-[10.5px] text-[var(--color-neutral-500)] italic">
              недоступен
            </span>
          )}
        </div>
        {query.trim() && (
          // Честно: искать не по чему. Текст документа наружу не отдаётся —
          // есть только внутренняя ручка /internal/doc-text под токеном
          // гейтвея, из браузера она закрыта. Число совпадений и переход к
          // ним появятся здесь, когда будет ручка текста документа для фронта.
          <p className="text-[11px] leading-snug text-[var(--color-neutral-600)]">
            Поиск по тексту документа пока не работает: сервер не отдаёт текст
            документа фронтенду. Число совпадений и переход к ним придумывать не
            будем — появятся, когда появится ручка текста.
          </p>
        )}
      </div>

      {target && (
        <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-divider)] bg-[var(--color-accent-100)] px-3 py-1.5 text-[11px] text-[var(--color-accent-700)] italic">
          <span className="size-[5px] shrink-0 rounded-full bg-[var(--color-accent)]" />
          {targetPage ? (
            <span className="[font-variant-numeric:tabular-nums]">
              {target.label ?? "источник метрики"} — стр. {targetPage}
            </span>
          ) : (
            <span>
              {target.label ?? "источник метрики"}: страница не указана — метрика
              извлечена без привязки к странице
            </span>
          )}
        </div>
      )}

      {!activeDoc ? (
        <div className="min-h-0 flex-1 bg-[var(--color-neutral-200)] p-4">
          <p className="text-[12.5px] text-[var(--color-neutral-600)]">
            {project
              ? "В этом проекте нет документов — перетащите PDF сюда или приложите его в чате кнопкой «+»."
              : "Выберите чат или проект слева."}
          </p>
        </div>
      ) : totalPages > 0 ? (
        <PageRibbon
          key={activeDoc.doc_id}
          fileUrl={fileUrl}
          totalPages={totalPages}
          target={targetPage ? { page: targetPage, label: target?.label } : null}
        />
      ) : (
        // Без числа страниц ленту не собрать: неизвестно, сколько паспарту
        // рисовать. Показываем документ целиком одним просмотрщиком — переход
        // к странице-источнику здесь работает только перезагрузкой кадра.
        <div className="flex min-h-0 flex-1 flex-col bg-[var(--color-neutral-200)]">
          <div className="min-h-0 flex-1 bg-[var(--color-surface)] p-2">
            <iframe
              key={`${activeDoc.doc_id}:${targetPage}`}
              src={`${fileUrl}#${targetPage ? `page=${targetPage}&` : ""}view=FitH&toolbar=0&navpanes=0`}
              title={activeDoc.filename}
              // Пока над панелью тащат файл, кадр отключаем от событий: иначе
              // просмотрщик внутри iframe перехватит drop, и файл откроется
              // в браузере вместо загрузки. В ленте страниц такой беды нет —
              // там поверх каждого кадра уже лежит своя накладка.
              className={cn(
                "size-full border-0 bg-[var(--color-neutral-100)]",
                dragOver && "pointer-events-none",
              )}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/** Строка одной загрузки: имя файла + честная стадия. Крестик — только у
 *  завершившихся: у идущей загрузки он читался бы как «отменить». */
function UploadRow({
  item,
  onDismiss,
}: {
  item: UploadItem;
  onDismiss: (id: string) => void;
}) {
  const bad = item.stage === "error" || item.stage === "unknown";
  const kind = fileKindOf(item.filename);
  return (
    <div className="flex items-baseline gap-2 text-[11px]">
      <kind.Icon
        className="size-3.5 shrink-0 self-center text-[var(--color-accent-700)]"
        aria-label={kind.label}
      />
      <span
        className="max-w-[45%] shrink-0 truncate text-[var(--color-text)]"
        title={`${kind.label} · ${item.filename}`}
      >
        {item.filename}
      </span>
      <span
        className={cn(
          "min-w-0 flex-1 truncate",
          bad ? "text-[var(--color-accent-700)]" : "text-[var(--color-neutral-600)]",
        )}
        title={uploadItemLabel(item)}
      >
        {uploadItemLabel(item)}
      </span>
      {(bad || item.stage === "ready") && (
        <button
          type="button"
          onClick={() => onDismiss(item.id)}
          title="Убрать эту строку"
          className="shrink-0 text-[var(--color-neutral-500)] transition-colors hover:text-[var(--color-text)]"
        >
          <X className="size-3" />
        </button>
      )}
    </div>
  );
}
