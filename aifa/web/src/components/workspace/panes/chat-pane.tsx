"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Check, Copy, Download, X } from "lucide-react";
import { fileKindOf } from "@/lib/file-kind";
import { docNameOf } from "@/lib/doc-names";
import ReactMarkdown from "react-markdown";
import { AnalysisCard } from "@/components/analysis-card";
import { TypedErrors, TypedResultView } from "@/components/typed-result-view";
import { CopyableTable } from "@/components/copyable-table";
import { ExtractedGrid } from "@/components/workspace/panes/extracted-table";
import { stripGfmTables } from "@/lib/gfm-tables";
import { thinkingLines } from "@/lib/thinking-lines";
import { activeStepOf } from "@/lib/active-step";
import remarkGfm from "remark-gfm";
import { projectSourceDownloadUrl } from "@/lib/api";
import { rowsFromExtraction, typedResultId } from "@/lib/projects";
import type { Beat, ExtractionMessage, Project, TextMessage } from "@/lib/projects";
import { interruptedNotice, type InterruptedState } from "@/lib/run-recovery";
import { toolLabel } from "@/lib/tool-labels";
import { useChatRun, type SendOpts } from "@/components/workspace/use-chat-run";
import {
  uploadItemLabel,
  useDocUpload,
  type UploadItem,
} from "@/components/workspace/use-doc-upload";
import { ChatDotsMenu, useWorkspaceSafe } from "@/components/workspace/panes/files-in-chat";
import { PlusMenu, SendControl } from "@/components/workspace/panes/composer-menus";
import { cn } from "@/lib/utils";
import { assistantAnswerLayout } from "@/lib/assistant-answer-layout";

/**
 * Панель чата: живая переписка проекта. Сообщения пользователя — вправо на
 * подложке поверхности, ответы агента — текстом без рамки; время появляется при
 * наведении. Композер: «+» слева, «Отправить» справа, никаких переключателей
 * режима — навык выбирает агент сам.
 *
 * Рантайм хода (отправка, поток, реаттач, «Стоп») живёт в useChatRun — панель
 * его только рисует. Рисуем ИМЕННО `run.project`, а не проп: страница читает
 * проекты один раз при монтировании и о дельтах хода не знает.
 */
export function ChatPane({
  project,
  onNeedProject,
  onDeleted,
}: {
  project: Project | null;
  /** Чат ещё не выбран — создать его на лету, чтобы «+» и отправка не были
   *  тупиком (пользователь пришёл работать, а не выбирать пункт в рельсе). */
  onNeedProject?: () => void;
  /** Чат удалён из меню «…» — странице надо снять выбор. */
  onDeleted?: (id: string) => void;
}) {
  const ws = useWorkspaceSafe();
  // Хук загрузки объявлен ниже, а нужен ходу — держим ссылку, иначе пришлось
  // бы поднимать загрузку выше панели и тянуть её через контекст.
  const uploadRef = useRef<{
    startParseFor: (ids: string[]) => Promise<void>;
    dismissAll: () => void;
  } | null>(null);
  const run = useChatRun(project, {
    docs: ws?.docs,
    // Документ, открытый в панели исходника: в чате с несколькими документами
    // запрос без уточнения должен бить в него (бэкенд получает подсказку).
    activeDocId: ws?.activeDocId,
    onFocusDoc: ws?.setActiveDocId,
    onTurnDocs: (docIds, freshIds) => {
      // Документ ушёл агенту: заказываем распознавание и убираем чипы из
      // композера (файл уже в сообщении и в панели документа — в поле ввода
      // ему больше не место).
      void uploadRef.current?.startParseFor(docIds);
      uploadRef.current?.dismissAll();
      // Раскладку двигаем ТОЛЬКО при первой отправке документа в этот чат:
      // тогда исходник и рабочая область появляются осмысленно. Раньше это
      // срабатывало на КАЖДОМ сообщении в чате с документом, и любой Enter
      // перекраивал окна, которые пользователь расставил руками.
      if (freshIds.length) ws?.requestLayout({ doc: false, ws: false });
    },
  });
  const live = run.project;

  // Рантайм хода — в общий контекст: кнопки рабочей области («Извлечь
  // метрики») шлют ходы ЭТОЙ подпиской, своей у них нет и быть не должно.
  const setChatRun = ws?.setChatRun;
  useEffect(() => {
    if (!setChatRun) return;
    setChatRun({ streaming: run.streaming, send: run.send });
    return () => setChatRun(null);
  }, [setChatRun, run.streaming, run.send]);
  const messages = useMemo(() => live?.messages ?? [], [live?.messages]);

  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atBottomRef = useRef(true);
  // Прикрепление файла из композера («+»): загрузка и статус живут в use-doc-upload,
  // сюда возвращаются только строки состояния.
  const fileRef = useRef<HTMLInputElement | null>(null);
  const upload = useDocUpload(live);
  uploadRef.current = upload;

  // Автопрокрутка за потоком — но только если пользователь и так внизу, иначе
  // чтение старых сообщений во время ответа дёргало бы ленту.
  const onScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && atBottomRef.current) el.scrollTop = el.scrollHeight;
  }, [messages]);
  // Смена чата всегда открывается на последнем сообщении.
  useEffect(() => {
    atBottomRef.current = true;
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live?.id]);

  /** Ждём дозагрузку файла перед отправкой — показываем это на кнопке. */
  const [waitingUpload, setWaitingUpload] = useState(false);

  const submit = async (text: string, opts?: SendOpts) => {
    if (!text.trim() || run.streaming || waitingUpload) return;
    if (!live) {
      // Пользователь начал писать до выбора чата — заводим его; текст
      // останется в поле, повторный Enter уйдёт уже в живой чат.
      onNeedProject?.();
      return;
    }
    // Сообщение НЕ появляется в чате, пока документы не долетели: пользователь
    // должен видеть в переписке готовый ход с вложением, а не растущий обрубок.
    // Черновик до этого момента остаётся в поле — если загрузка упадёт, текст
    // не потеряется.
    if (upload.busy) {
      setWaitingUpload(true);
      try {
        await upload.whenSettled();
      } finally {
        setWaitingUpload(false);
      }
      // Загрузка могла упасть — тогда отправлять нечего, ошибка уже видна в чипе.
      if (upload.items.some((i) => i.stage === "error")) return;
    }
    // Что пользователь ВИДИТ приложенным, то и уходит с ходом. Раньше ход брал
    // документы только из хранилища: если запись туда не состоялась, сообщение
    // улетало без файла, а чип оставался в композере — агент спрашивал «где
    // отчётность?», хотя документ был на экране.
    const chipDocIds = upload.items
      .filter((i) => i.doc_id && i.stage !== "error" && i.stage !== "unknown")
      .map((i) => i.doc_id!);
    setDraft("");
    void run.send(text, { ...opts, extraDocIds: chipDocIds });
  };

  // Документ для шорткатов: активный в рабочем экране, иначе первый в чате.
  const shortcutDoc = useMemo(() => {
    const d = ws?.activeDoc ?? ws?.docs?.[0] ?? null;
    return d ? { doc_id: d.doc_id, label: d.company_name?.trim() || d.filename } : null;
  }, [ws?.activeDoc, ws?.docs]);

  // Прикреплено к чату, но ещё НЕ отправлено ни одним сообщением. Плашки
  // загрузчика живут в памяти вкладки и не переживают переоткрытие чата, а
  // документ в `doc_ids` остаётся — чат выглядел пустым, хотя внутри лежал
  // отчёт, и агент «внезапно» им располагал (кейс intel_Q3 на «привет»).
  // Эти чипы строятся из ХРАНИЛИЩА, поэтому видны всегда — до первой отправки.
  const attachedUnsent = useMemo(() => {
    if (!live) return [] as { doc_id: string; label: string }[];
    const inMessages = new Set<string>();
    for (const m of live.messages) {
      if (m.role !== "user" && m.role !== "assistant") continue;
      for (const f of (m as TextMessage).files ?? []) inMessages.add(f.file_id);
    }
    const inChips = new Set(upload.items.map((i) => i.doc_id).filter(Boolean));
    return (live.doc_ids ?? [])
      .filter((id) => !inMessages.has(id) && !inChips.has(id))
      .map((id) => {
        const d = ws?.docs?.find((x) => x.doc_id === id);
        return { doc_id: id, label: d?.filename || d?.company_name || docNameOf(id) || "документ" };
      });
  }, [live, upload.items, ws?.docs]);

  /** Шорткат — детерминированный ход: пин тула с аргументами, без интенций.
   * Извлечение по реестру дополнительно раскрывает РАБОЧУЮ ОБЛАСТЬ — его
   * результат живёт именно там (таблица + Excel в артефактах). Панель
   * исходника не трогаем: пользователь её мог свернуть намеренно. */
  const fireShortcut = (message: string, opts?: SendOpts) => {
    if (opts?.pinnedTool === "fe_extract_metrics") {
      ws?.requestLayout({ ws: false });
    }
    void submit(message, opts);
  };

  return (
    <div className="relative flex h-full flex-col">
      <ChatDotsMenu project={live} onDeleted={onDeleted} />

      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 flex-1 overflow-y-auto px-6 pb-2 pt-8"
      >
        {!live && (
          <p className="text-[13px] text-[var(--color-neutral-500)]">
            Выберите чат или проект слева — переписка появится здесь.
          </p>
        )}
        {live && messages.length === 0 && (
          <p className="text-[13px] text-[var(--color-neutral-500)]">
            Переписки ещё нет. Задайте вопрос по отчётности — агент сам выберет навык.
          </p>
        )}

        <div className="mx-auto flex max-w-[820px] flex-col gap-4">
          {messages.map((m, i) =>
            // Ход извлечения показывается в рабочей области (там же роль и
            // таблица) — в переписке он был безымянной цифрой в рамке.
            m.role === "extraction" ? null : m.role === "user" ? (
              <UserRow key={m.id} message={m} />
            ) : (
              <AssistantRow
                key={m.id}
                message={m}
                projectId={live?.id ?? ""}
                // Ход ЖИВ — это состояние ПРОГОНА, а не подписки и не текста
                // (жалобы Nate 19.08 п.4-6 и 31.08). При обрыве SSE сторож
                // связи пересоединяется, и на мгновение `run.streaming`
                // гаснет — сообщение успевало отрисоваться КАК ОТВЕТ, а после
                // реконнекта прыгало обратно в ленту хода. `activeChatRun`
                // живёт в состоянии проекта и снимается только терминалом —
                // по нему и решаем.
                streaming={
                  (run.streaming || live?.activeChatRun?.assistant_id === m.id) &&
                  i === messages.length - 1
                }
                // Числа этого ответа: ближайший прогон извлечения ВЫШЕ по
                // ленте. Само сообщение прогона не рисуется (см. выше), но
                // его структурные строки — единственный способ показать в
                // чате ту же таблицу, что и в рабочей области.
                extraction={
                  messages
                    .slice(0, i)
                    .reverse()
                    .find((x) => x.role === "extraction") as ExtractionMessage | undefined
                }
                onPickOption={(t) => void submit(t)}
                // «Отправить заново» — это тот же вопрос пользователя, что
                // породил погибший ход; искать его руками в ленте не надо.
                onRetry={() => {
                  const prior = messages
                    .slice(0, i)
                    .reverse()
                    .find((x) => x.role === "user");
                  if (prior && prior.role === "user") void submit(prior.content);
                }}
                onReconnect={run.reconnect}
              />
            ),
          )}
        </div>
      </div>

      <div className="shrink-0 p-3">
        <div className="rounded-[var(--radius-token-md)] border border-[var(--color-divider)] bg-[var(--color-surface)] p-2">
          <textarea
            rows={2}
            value={draft}
            disabled={!live && !onNeedProject}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit(draft);
              }
            }}
            placeholder="Спросите про отчётность — агент найдёт цифру и покажет страницу"
            className="w-full resize-none bg-transparent px-2 py-1 text-[13px] outline-none placeholder:text-[var(--color-neutral-500)] disabled:cursor-not-allowed"
          />
          <div className="flex items-center justify-between pt-1">
            {/* «+» — меню настроек хода: файл, роль, поиск в интернете.
                Неподключённое (модель, почта) показывается честно выключенным. */}
            <div className="flex min-w-0 flex-1 items-center gap-2">
              <input
                ref={fileRef}
                type="file"
                accept=".pdf,application/pdf"
                multiple
                className="hidden"
                onChange={(e) => {
                  const picked = Array.from(e.target.files ?? []);
                  // Сброс значения — иначе повторный выбор ТОГО ЖЕ файла не
                  // вызовет change и загрузка молча не начнётся.
                  e.target.value = "";
                  void upload.upload(picked);
                }}
              />
              <PlusMenu
                disabled={!live && !onNeedProject}
                onAttach={() => {
                  // Чата ещё нет — заводим его и сразу открываем выбор файла:
                  // «плюс» не должен быть тупиком на пустом экране.
                  if (!live) {
                    onNeedProject?.();
                    return;
                  }
                  fileRef.current?.click();
                }}
              />
              {attachedUnsent.map((d) => {
                const kind = fileKindOf(d.label);
                return (
                  <span
                    key={d.doc_id}
                    className="group/chip flex min-w-0 items-center gap-1.5 rounded-[var(--radius-token-sm)] bg-[var(--color-neutral-100)] px-2 py-1 text-[11px]"
                  >
                    <kind.Icon
                      className="size-3.5 shrink-0 text-[var(--color-accent-700)]"
                      aria-label={kind.label}
                    />
                    <span
                      className="max-w-[130px] truncate text-[var(--color-text)]"
                      title={`${kind.label} · ${d.label} — прикреплён к чату, уйдёт со следующим сообщением`}
                    >
                      {d.label}
                    </span>
                    <button
                      type="button"
                      onClick={() => upload.detachById(d.doc_id)}
                      title="Убрать документ из чата"
                      className="shrink-0 text-[var(--color-neutral-500)] opacity-0 transition-[opacity,color] hover:text-[var(--color-accent-700)] focus-visible:opacity-100 group-hover/chip:opacity-100"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                );
              })}
              {upload.items.map((it) => {
                const bad = it.stage === "error" || it.stage === "unknown";
                const kind = fileKindOf(it.filename);
                return (
                  <span
                    key={it.id}
                    className={cn(
                      // Пока файл летит — чип приглушён; долетел и готов к
                      // отправке — становится в полную силу. Словами это не
                      // объясняем: состояние читается яркостью.
                      "group/chip flex min-w-0 items-center gap-1.5 rounded-[var(--radius-token-sm)] px-2 py-1 text-[11px] transition-opacity",
                      bad ? "bg-[var(--color-accent-100)]" : "bg-[var(--color-neutral-100)]",
                      it.stage === "uploading" ? "opacity-55" : "opacity-100",
                    )}
                  >
                    {/* Пока файл летит — кружок с процентом; долетел — значок
                        формата, чтобы по чипу было видно, ЧТО приложено. */}
                    {it.stage === "uploading" || it.stage === "ocr" ? (
                      <UploadRing item={it} />
                    ) : (
                      <kind.Icon
                        className="size-3.5 shrink-0 text-[var(--color-accent-700)]"
                        aria-label={kind.label}
                      />
                    )}
                    <span
                      className="max-w-[130px] truncate text-[var(--color-text)]"
                      title={`${kind.label} · ${it.filename}`}
                    >
                      {it.filename}
                    </span>
                    <span
                      className={cn(
                        "max-w-[200px] truncate",
                        bad
                          ? "text-[var(--color-accent-700)]"
                          : "text-[var(--color-neutral-600)]",
                      )}
                      title={uploadItemLabel(it) || it.filename}
                    >
                      {uploadItemLabel(it)}
                    </span>
                    {/* Передумал отправлять — крестик. У проблемных файлов он
                        виден всегда, у обычных проявляется при наведении, чтобы
                        не шуметь в спокойном состоянии. */}
                    <button
                      type="button"
                      onClick={() => upload.remove(it.id)}
                      title={bad ? "Убрать сообщение о файле" : "Убрать файл из сообщения"}
                      className={cn(
                        "shrink-0 text-[var(--color-neutral-500)] transition-[opacity,color] hover:text-[var(--color-accent-700)] focus-visible:opacity-100",
                        bad ? "opacity-100" : "opacity-0 group-hover/chip:opacity-100",
                      )}
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                );
              })}
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              {waitingUpload ? (
                <button
                  type="button"
                  disabled
                  title="Документ ещё загружается — сообщение уйдёт в чат, как только файл долетит"
                  className="rounded-[var(--radius-token-md)] border border-[var(--color-divider)] px-4 py-1.5 text-[13px] text-[var(--color-neutral-600)]"
                >
                  Загружаю документ…
                </button>
              ) : run.streaming ? (
                <button
                  type="button"
                  onClick={run.stop}
                  title="Прервать ход — прогон на сервере тоже будет отменён"
                  className="rounded-[var(--radius-token-md)] border border-[var(--color-divider)] px-4 py-1.5 text-[13px] text-[var(--color-neutral-700)] transition-colors hover:bg-[var(--color-neutral-100)]"
                >
                  Стоп
                </button>
              ) : (
                <SendControl
                  doc={shortcutDoc}
                  canSend={Boolean(draft.trim()) && Boolean(live || onNeedProject)}
                  onSend={() => void submit(draft)}
                  onFire={fireShortcut}
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

/**
 * Кружок хода загрузки файла: пока льётся — дуга по проценту, дальше точка
 * стадии. Проценты именно у КАЖДОГО файла: их может быть несколько сразу.
 */
function UploadRing({ item }: { item: UploadItem }) {
  const bad = item.stage === "error" || item.stage === "unknown";
  if (item.stage === "uploading") {
    const pct = Math.max(0, Math.min(100, item.percent ?? 0));
    return (
      <span
        className="grid size-4 shrink-0 place-items-center rounded-full"
        style={{
          background: `conic-gradient(var(--color-accent) ${pct * 3.6}deg, var(--color-neutral-300) 0deg)`,
        }}
        title={`загружено ${pct}%`}
      >
        <span className="size-2.5 rounded-full bg-[var(--color-surface)]" />
      </span>
    );
  }
  if (item.stage === "ocr") {
    return (
      <span
        className="size-2 shrink-0 animate-pulse rounded-full bg-[var(--color-accent)]"
        title="распознавание"
      />
    );
  }
  return (
    <span
      className={cn(
        "size-2 shrink-0 rounded-full",
        bad ? "bg-[var(--color-accent-700)]" : "bg-[var(--color-accent-400)]",
      )}
    />
  );
}

function timeOf(ts: number): string {
  return new Date(ts).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/** Время сообщения: невидимо, проявляется при наведении на строку со сдвигом 4px. */
const TIME_CLS =
  "shrink-0 translate-x-1 text-[10px] uppercase tracking-[0.11em] tabular-nums text-[var(--color-neutral-500)] opacity-0 transition-[opacity,transform] duration-200 group-hover:translate-x-0 group-hover:opacity-100";

function UserRow({ message }: { message: TextMessage }) {
  const files = message.files ?? [];
  return (
    <div className="group flex items-baseline justify-end gap-3">
      <span className={TIME_CLS}>{timeOf(message.created_at)}</span>
      <div className="flex max-w-[88%] flex-col items-end gap-1.5">
        {/* Приложенные документы — отдельными чипами НАД текстом: пользователь
            должен видеть, что файл действительно ушёл в ход. */}
        {files.map((f) => {
          const kind = fileKindOf(f.filename);
          return (
            <span
              key={f.file_id}
              className="flex max-w-full items-center gap-1.5 rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1 text-[11px] text-[var(--color-neutral-700)]"
              title={`${kind.label} · ${f.filename}`}
            >
              <kind.Icon className="size-3.5 shrink-0 text-[var(--color-accent-700)]" />
              <span className="truncate">{f.filename}</span>
            </span>
          );
        })}
        {message.content && (
          <div className="rounded-[var(--radius-token-md)] bg-[var(--color-surface)] px-4 py-3 text-[13.5px] leading-relaxed whitespace-pre-wrap text-[var(--color-text)]">
            {message.content}
          </div>
        )}
      </div>
    </div>
  );
}

function AssistantRow({
  message,
  projectId,
  extraction,
  streaming,
  onPickOption,
  onRetry,
  onReconnect,
}: {
  message: TextMessage;
  projectId: string;
  /** Ход этого сообщения ИДЁТ ПРЯМО СЕЙЧАС. */
  streaming?: boolean;
  /** Прогон извлечения, к которому относится ответ. Есть — таблицу рисуем
   *  сеткой рабочей области; нет — оставляем markdown как был. */
  extraction?: ExtractionMessage;
  onPickOption: (text: string) => void;
  /** Прогон погиб — отправить тот же запрос заново. */
  onRetry: () => void;
  /** Прогон жив — догнать его ответ, НЕ запуская второй ход. */
  onReconnect: () => void;
}) {
  const hasFiles = (message.files?.length ?? 0) > 0;
  const hasTypedResults = (message.typedResults?.length ?? 0) > 0;
  const answerLayout = assistantAnswerLayout(message, Boolean(streaming));
  // Где ход сейчас: идущий шаг → очередной → последний упавший (см.
  // `activeStepOf`, жалоба Nate 19.08 п.5 про «замер на красном шаге»).
  const activeStep = activeStepOf(message.plan);
  // Ход мог закончиться одной карточкой/файлом и без текста — молчаливая
  // пустота выглядела бы как потерянный ответ, поэтому говорим прямо.
  // Прерванный ход сюда не относится: у него своя плашка с причиной и
  // действием, а «агент отработал инструментами» было бы враньём.
  const emptyFinish =
    !message.content &&
    !message.thinking &&
    !hasFiles &&
    !message.analysisCard &&
    !hasTypedResults &&
    !(message.typedErrors?.length ?? 0) &&
    !message.interrupted;

  return (
    <div className="group relative pl-12">
      <span className={cn(TIME_CLS, "absolute left-0 top-1")}>{timeOf(message.created_at)}</span>

      {/* Карточка анализа ЗАМЕНЯЕТ маркдаун-тело (контракт TextMessage:
          content — фолбэк того же отчёта). Пока карточки нет, текст стримится
          как обычно; с её приходом остаётся только карточка — иначе отчёт
          показывался дважды подряд. */}
      {(message.content || message.analysisCard || hasTypedResults) && (
        <CopyButton text={message.content} />
      )}

      {/* Лента хода: что агент писал по пути и какие инструменты брал —
          в порядке появления. Идёт ПЕРЕД ответом, потому что это дорога к
          нему. Пока ход живой, лента раскрыта; закончился — сворачивается. */}
      {((message.beats?.length ?? 0) > 0 ||
        (streaming && message.content && !message.analysisCard && !hasTypedResults)) && (
        <RunRibbon
          beats={message.beats ?? []}
          live={streaming || !!message.thinking}
          hasGrid={Boolean(extraction && extraction.metrics.length > 0)}
          // Живой текст рисуется ХВОСТОМ ленты, а не отдельным блоком
          // (жалоба 31.08: «где-то есть черта слева, а где-то нет»). Пока шёл
          // стрим, заметки стояли за чертой, а текущий текст — без неё и с
          // другим отступом: один лог выглядел двумя разными.
          liveTail={streaming && !message.analysisCard && !hasTypedResults ? message.content : undefined}
        />
      )}

      {/* ПОКА ХОД ИДЁТ — ЭТО НЕ ОТВЕТ, А ХОД (жалоба Nate 19.08, п.4).
          Написанное агентом по дороге показывалось телом ответа: 14,5 пункта,
          растущей простынёй во всю ширину. Теперь тот же текст идёт регистром
          хроники — три обновляющиеся строки мелким, как трейсинг статуса.
          Ничего не теряется: с очередным инструментом этот текст уходит
          заметкой в ленту хода, а по завершении хода `content` рисуется
          обычным ответом. */}
      {answerLayout.contentPlacement === "before-results" && (
        <ExtractionBody message={message} extraction={extraction} />
      )}

      {/* Срез 29a, §4.5.8: компактный граф-рендерер убран как дублирование —
          построенный `plan_graph` этого хода уже виден целиком в правой
          «Рабочей области» (`PlanGraphWorkspace`), автоматически и без
          отдельного клика; второй независимый рендерер статусов узлов не
          заводим, чтобы им было негде разойтись. */}
      {message.typedResults?.map((result) => (
        <TypedResultView
          key={typedResultId(result)}
          result={result}
        />
      ))}
      {answerLayout.contentPlacement === "after-results" && (
        <ExtractionBody message={message} extraction={extraction} />
      )}
      <TypedErrors errors={message.typedErrors ?? []} />

      {message.thinking && (
        <ThinkingLine text={message.thinking} since={message.created_at} />
      )}

      {/* Долгий ход выглядел зависшим: «Думаю…» и больше ничего, хотя план уже
          идёт. Показываем текущий шаг прямо в переписке — за прогрессом не надо
          лезть в рабочую область. Условие «нет текста» снято: текст по дороге
          есть почти всегда, а шаг нужен ровно тогда, когда ход идёт. */}
      {streaming && activeStep && (
        <p className="mt-0.5 text-[11.5px] text-[var(--color-neutral-600)]">
          <span className="[font-variant-numeric:tabular-nums]">
            шаг {activeStep.index + 1} из {message.plan?.length ?? 0}
          </span>
          {" · "}
          {activeStep.title}
          {activeStep.status === "error" && (
            <span className="text-[var(--color-accent-700)]"> — не вышло, ищу другой путь</span>
          )}
        </p>
      )}

      {message.analysisCard && !hasTypedResults && (
        // Карточка анализа — тот же компонент, что на старом экране: второй
        // реализации у структурного отчёта быть не должно.
        <div className="mt-2">
          <AnalysisCard card={message.analysisCard} />
        </div>
      )}

      {emptyFinish && (
        <p className="text-[12.5px] italic text-[var(--color-neutral-500)]">
          Ход завершён без текстового ответа — агент отработал инструментами.
          Результат смотрите в рабочей области справа.
        </p>
      )}

      {message.interrupted && (
        <InterruptedNote
          interrupted={message.interrupted}
          onRetry={onRetry}
          onReconnect={onReconnect}
        />
      )}

      {hasFiles && projectId && (
        <div className="mt-2 flex flex-wrap gap-3">
          {message.files!.map((f) => (
            <a
              key={f.file_id}
              href={projectSourceDownloadUrl(projectId, f.file_id)}
              download={f.filename}
              target="_blank"
              rel="noreferrer"
              title={`Скачать ${f.filename}`}
              // min-w-0 + truncate: в узкой колонке имя xlsx вылезало за край
              // чата (жалоба 31.08); полное имя остаётся в title.
              className="inline-flex max-w-full min-w-0 items-center gap-1.5 text-[12.5px] text-[var(--color-accent-700)] underline-offset-2 hover:underline"
            >
              <Download className="size-3.5 shrink-0" />
              <span className="truncate">{f.filename}</span>
            </a>
          ))}
        </div>
      )}

      {message.question && (
        <div className="mt-3 flex flex-col gap-2">
          <p className="text-[13px] text-[var(--color-text)]">{message.question.prompt}</p>
          <div className="flex flex-wrap gap-2">
            {message.question.options.map((o) => (
              <button
                key={o}
                type="button"
                onClick={() => onPickOption(o)}
                className="rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] px-3 py-1.5 text-[12.5px] text-[var(--color-text)] transition-colors hover:bg-[var(--color-accent-100)]"
              >
                {o}
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}


/**
 * Прерванный ход: причина и ОДНО правильное действие.
 *
 * Действия противоположны и перепутать их нельзя: у погибшего прогона работы
 * нет — надо гнать заново; у живого работа идёт и результат копится — второй
 * запуск затрёт маркер и осиротит готовый ответ, поэтому там кнопка «догнать».
 */
function InterruptedNote({
  interrupted,
  onRetry,
  onReconnect,
}: {
  interrupted: InterruptedState;
  onRetry: () => void;
  onReconnect: () => void;
}) {
  const notice = interruptedNotice(interrupted);
  return (
    <div className="mt-1 flex flex-wrap items-center gap-2">
      <p className="text-[12.5px] text-[var(--color-neutral-600)]">{notice.text}</p>
      {notice.action !== "none" && (
        <button
          type="button"
          onClick={notice.action === "resend" ? onRetry : onReconnect}
          className="rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] px-2.5 py-1 text-[12px] text-[var(--color-text)] transition-colors hover:bg-[var(--color-accent-100)]"
        >
          {notice.actionLabel}
        </button>
      )}
    </div>
  );
}

/**
 * Разметка ответа агента. Свой набор компонентов, а не общий с «/»: тот
 * завязан на палитру shadcn (bg-muted / text-primary), которая не переключается
 * вместе с тремя темами редизайна.
 */
/**
 * Тело ответа агента: текст + таблица прогона.
 *
 * ОДИН ВИД ЧИСЕЛ НА ВСЁ ПРИЛОЖЕНИЕ (правка Nate 17.08). Раньше таблицу в чате
 * рисовала модель — markdown'ом, со своими колонками и своим форматированием
 * чисел; в рабочей области та же таблица шла сеткой `ExtractedGrid`. На вопрос
 * «у нас одинакового формата таблица там и там?» ответ был «нет».
 *
 * Теперь markdown-таблица из текста ВЫРЕЗАЕТСЯ, а на её место встаёт та же
 * сетка. Подпись собирается из паспорта прогона (компания, период, единицы), а
 * не из текста модели: это данные, а не пересказ.
 *
 * ⚠️ Замена происходит, только если прогон найден И в тексте действительно
 * была таблица. Нет прогона — markdown остаётся как был: пустое место вместо
 * чисел хуже некрасивых чисел.
 */
function ExtractionBody({
  message,
  extraction,
}: {
  message: TextMessage;
  extraction?: ExtractionMessage;
}) {
  const ws = useWorkspaceSafe();
  const rows = useMemo(
    () => (extraction ? rowsFromExtraction(extraction) : []),
    [extraction],
  );
  const stripped = useMemo(
    () => (rows.length ? stripGfmTables(message.content) : { text: message.content, had: false }),
    [message.content, rows.length],
  );
  // ТАБЛИЦА МОГЛА УЕХАТЬ В ХРОНИКУ (#207). Всё, что агент напечатал ДО
  // очередного инструмента, уходит заметкой в «ход работы» — вместе с
  // таблицей, если он напечатал её там. Тогда в итоговом тексте таблицы нет,
  // и сетка не показывалась вовсе: числа оставались простынёй в хронике.
  const notesHadTable = useMemo(
    () =>
      rows.length > 0 &&
      (message.beats ?? []).some((b) => b.kind === "note" && stripGfmTables(b.text).had),
    [message.beats, rows.length],
  );
  const showGrid = rows.length > 0 && (stripped.had || notesHadTable);
  const meta = extraction?.meta;
  const caption = [meta?.company_name, meta?.period_current, meta?.unit]
    .filter(Boolean)
    .join(" · ");

  return (
    <>
      {stripped.text && <Markdown text={showGrid ? stripped.text : message.content} />}
      {showGrid && (
        <CopyableTable caption={caption || "Показатели"}>
          {/* Высота ограничена: таблица на полсотни строк иначе выдавливает из
              ленты весь разговор. Прокрутка своя, липкая шапка работает. */}
          <div className="max-h-[420px] overflow-auto">
            <ExtractedGrid
              rows={rows}
              variant="metrics"
              unit={meta?.unit}
              periodCurrent={meta?.period_current}
              periodPrev={meta?.period_prev}
              onOpenSource={ws?.gotoSource}
            />
          </div>
        </CopyableTable>
      )}
    </>
  );
}

/** Текст ближайшего заголовка ВЫШЕ таблицы — им таблица и подписывается.
 *
 *  Разбирать сам markdown не нужно и вредно: ReactMarkdown обходит документ по
 *  порядку, поэтому достаточно запомнить последний отрисованный заголовок.
 *  Заголовок прогона («## МегаФон · Ячейка … · Период … · Единицы …») его и
 *  даёт — того же текста, что стоит в ленте, без второго источника правды. */
export function headingText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(headingText).join("");
  if (node && typeof node === "object" && "props" in node) {
    return headingText((node as { props?: { children?: ReactNode } }).props?.children);
  }
  return "";
}

function Markdown({ text }: { text: string }) {
  // Не состояние: перерисовки от него не нужны, а ReactMarkdown обходит
  // документ синхронно — к моменту отрисовки таблицы заголовок уже записан.
  const lastHeading = { current: "" };
  const remember = (children: ReactNode) => {
    lastHeading.current = headingText(children).trim();
    return children;
  };
  return (
    <div className="text-[14.5px] leading-[1.72] text-[var(--color-text)] [text-wrap:pretty]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="my-2 first:mt-0 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="my-2 list-disc pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="my-2 list-decimal pl-5">{children}</ol>,
          li: ({ children }) => <li className="my-0.5">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          h1: ({ children }) => (
            <h1 className="font-heading mt-3 mb-1.5 text-[17px]">{remember(children)}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="font-heading mt-3 mb-1.5 text-[16px]">{remember(children)}</h2>
          ),
          h3: ({ children }) => (
            <h3 className="font-heading mt-3 mb-1.5 text-[15px]">{remember(children)}</h3>
          ),
          a: ({ href, children }) => (
            <a
              href={typeof href === "string" ? href : ""}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[var(--color-accent-700)] underline-offset-2 hover:underline"
            >
              {children}
            </a>
          ),
          code: ({ children }) => (
            <code className="rounded-[var(--radius-token-sm)] bg-[var(--color-neutral-100)] px-1 py-0.5 text-[12.5px]">
              {children}
            </code>
          ),
          pre: ({ children }) => (
            <pre className="my-2 overflow-x-auto rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] px-3 py-2 text-[12px]">
              {children}
            </pre>
          ),
          // Таблица показателей бывает шире панели — прокручиваем её саму,
          // а не всю ленту. Обёртка отделяет таблицу от сплошного текста
          // ответа и даёт копирование в Excel (см. CopyableTable).
          table: ({ children }) => (
            <CopyableTable caption={lastHeading.current}>
              {/* `w-max` БЕЗ `min-w-full` (правка Nate 17.08). С min-w-full
                  таблица растягивалась на всю ширину панели, и лишнее место
                  доставалось самой широкой ячейке — колонке названий: числа
                  улетали к правому краю, за десяток сантиметров от строки,
                  к которой относятся. Теперь таблица обнимает содержимое. */}
              <table className="w-max border-collapse text-[12.5px]">{children}</table>
            </CopyableTable>
          ),
          th: ({ children }) => (
            <th className="border border-[var(--color-divider)] px-2 py-1 text-left font-medium [&:not(:first-child)]:text-right">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-[var(--color-divider)] px-2 py-1 tabular-nums [&:not(:first-child)]:whitespace-nowrap [&:not(:first-child)]:text-right">
              {children}
            </td>
          ),
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

/**
 * Живая строка состояния хода: спиннер, фраза и секундомер.
 *
 * Не лог — строк не прибавляется, СМЕНЯЕТСЯ текст по мере того, как агент
 * берётся за очередной инструмент. Секундомер идёт от начала ожидания и
 * отвечает на главный вопрос долгого хода — «сколько уже?»; переливание
 * фразы и вращение кольца показывают, что ход жив, даже когда фраза долго
 * не меняется (модель может думать минуту и больше).
 */
/** Хроника хода: реплики агента и взятые инструменты по порядку.
 *
 *  Инструмент — не строка списка, а «чип» с точкой состояния: так видно, где
 *  агент сейчас, а где уже прошёл. Пока ход живой, лента раскрыта — это и есть
 *  ответ на «что вообще происходит»; после завершения она сворачивается в одну
 *  строку, чтобы не заслонять сам ответ, но остаётся раскрываемой. */
function RunRibbon({
  beats,
  live,
  hasGrid,
  liveTail,
}: {
  beats: Beat[];
  live: boolean;
  /** Числа прогона есть и показываются сеткой — значит таблицу из заметки
   *  можно убрать: она не информация, а второй экземпляр тех же чисел. */
  hasGrid: boolean;
  /** Текст, который агент печатает ПРЯМО СЕЙЧАС, — рисуется хвостом ленты,
   *  за той же чертой и с тем же отступом, что заметки. Кончится порция —
   *  этот же текст станет очередной заметкой или финальным ответом. */
  liveTail?: string;
}) {
  const [open, setOpen] = useState(true);
  const shown = live || open;
  const tools = beats.filter((b) => b.kind === "tool").length;
  // Лента прокручивается внутри себя (max-h) — свежие строки живого лога были
  // бы за нижним краем. Держим прокрутку у низа, пока ход идёт.
  const boxRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (live && boxRef.current) boxRef.current.scrollTop = boxRef.current.scrollHeight;
  }, [live, beats.length, liveTail]);

  // Заметок может не быть вовсе (ход без инструментов) — тогда и кнопка «ход
  // работы» с нулём инструментов только путает.
  const notes = beats.filter((b) => b.kind === "note").length;
  return (
    <div className="mb-4">
      {!live && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="mb-1 text-[11.5px] text-[var(--color-neutral-500)] transition-colors hover:text-[var(--color-accent-700)]"
        >
          {open ? "▾" : "▸"} ход работы
          {tools > 0 && (
            <> · {tools} {tools === 1 ? "инструмент" : tools < 5 ? "инструмента" : "инструментов"}</>
          )}
          {tools === 0 && notes > 0 && <> · заметок: {notes}</>}
        </button>
      )}
      {shown && (
        // Прокрутка ВНУТРИ ленты (жалоба владельца 31.08): простыня рассуждений
        // не должна раздвигать чат — при длинных логах листается сам блок, а
        // свернуть его целиком можно кнопкой выше.
        <div
          ref={boxRef}
          // Внутренняя прокрутка (max-h) — только пока ход ЖИВ: она держит чат
          // от прыжков при стриме. У ЗАКОНЧЕННОГО хода она прятала хвост логов
          // за нижним краем — в узкой колонке текст занимает больше строк, и
          // «показать целиком» раскрывал заметку в невидимую область (жалоба
          // 31.08: «логи пропадают»). Развёрнутую историю листает сама
          // страница, как любой длинный текст.
          className={cn(
            "flex min-w-0 flex-col gap-1.5 border-l-2 border-[var(--color-divider)] pl-3",
            live && "max-h-[200px] overflow-y-auto",
          )}
        >
          {beats.map((b, i) =>
            b.kind === "note" ? (
              <RunNote key={i} text={b.text} stripTable={hasGrid} />
            ) : (
              <span
                key={i}
                // `items-start` + отступ точки: у шага в две строки текст
                // держит одну левую границу, а не разъезжается по вертикали.
                // `min-w-0` и перенос — жалоба п.2: длинный путь или имя файла
                // выезжали за правый край области.
                className="flex min-w-0 items-start gap-2 break-words text-[12px] text-[var(--color-neutral-500)]"
              >
                <span
                  aria-hidden
                  className={cn(
                    "mt-[5px] size-[6px] shrink-0 rounded-full",
                    b.status === "running" && "aifa-plan-dot-running",
                  )}
                  style={{
                    background:
                      b.status === "error"
                        ? "var(--color-accent-700)"
                        : b.status === "running"
                          ? "var(--color-accent)"
                          : "var(--color-neutral-400)",
                  }}
                />
                {b.status === "running" ? toolLabel(b.name).doing : toolLabel(b.name).step}
                {/* Причина отказа рядом с шагом: красная точка без объяснения
                    оставляет человека гадать, что именно не получилось. */}
                {b.status === "error" && b.reason && (
                  <span className="text-[11.5px] text-[var(--color-accent-700)]">
                    — {b.reason}
                  </span>
                )}
              </span>
            ),
          )}
          {liveTail && <LiveProse text={liveTail} />}
        </div>
      )}
    </div>
  );
}

/**
 * Заметка агента в хронике хода (#207).
 *
 * ЖАЛОБА. «Результат извлечения вываливается простынёй в ход работы» — и это
 * не следствие отказа: так выглядит УДАЧНЫЙ прогон. Заметка — это всё, что
 * агент напечатал до очередного инструмента, и если он напечатал туда таблицу
 * на сотню строк, вся она вставала в ленту.
 *
 * ДВЕ РАЗНЫЕ ПРАВКИ, И ПУТАТЬ ИХ НЕЛЬЗЯ.
 *  1. Таблица убирается, только когда те же числа показаны сеткой. Это не
 *     фильтр, а снятие ВТОРОГО экземпляра: сетка рисуется из прогона, а не из
 *     текста модели, и она полнее (страницы, единицы, переход к источнику).
 *  2. Длинный текст не режется, а СВОРАЧИВАЕТСЯ до трёх строк с раскрытием.
 *     Выбрасывать написанное агентом нельзя: по этим строкам разбирают, что
 *     он делал и почему.
 *
 * Отучать модель печатать таблицы в промежуточный текст — дело тела навыка,
 * а не интерфейса; здесь мы только перестаём показывать это дважды.
 */
function RunNote({ text, stripTable }: { text: string; stripTable: boolean }) {
  const [open, setOpen] = useState(false);
  const body = stripTable ? stripGfmTables(text).text : text;
  if (!body) return null;
  const long = body.length > 400 || body.split("\n").length > 4;

  return (
    <div>
      <p
        className={cn(
          // `pl-[14px]` — ровно точка (6) плюс зазор (8) у строки шага: проза
          // агента и названия шагов стоят на одной левой границе (жалоба п.3).
          // `break-words` — чтобы длинная строка без пробелов переносилась,
          // а не вылезала за область (жалоба п.2).
          // Курсив — визуальная граница «это дорога, не ответ» (жалоба 31.08).
          "whitespace-pre-wrap break-words pl-[14px] text-[12.5px] italic leading-relaxed text-[var(--color-neutral-600)]",
          long && !open && "line-clamp-3",
        )}
      >
        {body}
      </p>
      {long && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="mt-0.5 text-[11.5px] text-[var(--color-neutral-500)] transition-colors hover:text-[var(--color-accent-700)]"
        >
          {open ? "свернуть" : "показать целиком"}
        </button>
      )}
    </div>
  );
}

/**
 * Текст, который агент пишет ПО ХОДУ работы, в регистре хроники.
 *
 * ЖАЛОБА (Nate, 19.08, п.4): «нижний текст крупным шрифтом — вместо простыни
 * держать 2–3 обновляющихся предложения как трейсинг статуса плана». Так и
 * было: пока ход идёт, `content` растёт и рисуется телом ОТВЕТА — 14,5 пункта
 * во всю ширину панели. Приём тот же, что у строки состояния (17.08): режем на
 * предложения и показываем последние три, свежее ярче.
 *
 * Текст не выбрасывается: с очередным инструментом он уходит заметкой в ленту
 * хода и остаётся раскрываемым, а по завершении хода рисуется обычным ответом.
 */
function LiveProse({ text }: { text: string }) {
  const lines = thinkingLines(text);
  if (!lines.length) return null;
  return (
    <div className="flex flex-col gap-0.5 pl-[14px]">
      {lines.map((l, i) => (
        <span
          key={`${i}-${l}`}
          title={l}
          className={cn(
            // Перенос с потолком строк вместо `truncate` (жалоба 31.08: в
            // узкой колонке обрезалось всё после трёх-четырёх слов). Потолок
            // держит защиту от простыни 17.08: показываются те же 2–3 записи,
            // просто каждая доносит свой текст.
            "block break-words text-[12.5px] italic",
            i === lines.length - 1
              ? "line-clamp-3 text-[var(--color-neutral-600)]"
              : "line-clamp-2 text-[11.5px] text-[var(--color-neutral-400)]",
          )}
        >
          {l}
        </span>
      ))}
    </div>
  );
}


function ThinkingLine({ text, since }: { text: string; since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const sec = Math.max(0, Math.floor((now - since) / 1000));
  const clock = sec < 60 ? `${sec} с` : `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
  const lines = thinkingLines(text);

  return (
    <p className="flex items-start gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
      <style>{`
@media (prefers-reduced-motion: no-preference) {
  @keyframes aifa-think-spin { to { transform: rotate(360deg) } }
  @keyframes aifa-think-sweep { 0% { background-position: 120% 0 } 100% { background-position: -20% 0 } }
  .aifa-think-ring { animation: aifa-think-spin 1s linear infinite }
  .aifa-think-word {
    background: linear-gradient(90deg,
      var(--color-neutral-500) 0%, var(--color-neutral-500) 35%,
      var(--color-accent-700) 50%,
      var(--color-neutral-500) 65%, var(--color-neutral-500) 100%);
    background-size: 220% 100%;
    -webkit-background-clip: text; background-clip: text; color: transparent;
    animation: aifa-think-sweep 2.4s linear infinite;
  }
}
      `}</style>
      <span
        aria-hidden
        className="aifa-think-ring mt-[3px] size-3 shrink-0 rounded-full border-[1.5px] border-[var(--color-neutral-300)] border-t-[var(--color-accent)]"
      />
      {/* ТРИ СТРОКИ, А НЕ ПРОСТЫНЯ (правка Nate 17.08). Весь поток размышлений
          печатался одним абзацем и рос без предела: пользователь читал
          «Bridge разорвал соединение… Progress застрял на 132 строках…»
          вместо работы. Показываем последние три: свежая мерцает, прошлые
          гаснут. Обрезку `truncate` сменил перенос с потолком (жалоба 31.08:
          в узкой колонке от строки оставалось три слова и многоточие) —
          записей по-прежнему три, простыня не возвращается. */}
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        {lines.map((l, i) => {
          const fresh = i === lines.length - 1;
          return (
            <span
              key={`${i}-${l}`}
              title={l}
              className={cn(
                "block break-words italic",
                fresh
                  ? "aifa-think-word line-clamp-3"
                  : "line-clamp-2 text-[11.5px] text-[var(--color-neutral-400)]",
              )}
            >
              {l}
            </span>
          );
        })}
      </span>
      <span className="shrink-0 self-start text-[11px] tabular-nums text-[var(--color-neutral-400)]">
        {clock}
      </span>
    </p>
  );
}

/**
 * Копирование ответа. Живёт в правом верхнем углу сообщения и проявляется при
 * наведении — постоянная кнопка спорила бы с текстом за внимание. После
 * копирования две секунды показывает галочку: без подтверждения непонятно,
 * сработало ли.
 */
function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  if (!text.trim()) return null;
  return (
    <button
      type="button"
      title="Скопировать ответ"
      onClick={() => {
        void navigator.clipboard
          .writeText(text)
          .then(() => {
            setDone(true);
            setTimeout(() => setDone(false), 2000);
          })
          .catch(() => undefined);
      }}
      className={cn(
        "absolute right-0 top-1 grid size-[26px] place-items-center rounded-[var(--radius-token-sm)]",
        "text-[var(--color-neutral-500)] transition-[opacity,color,background-color] duration-150",
        "hover:bg-[var(--color-neutral-100)] hover:text-[var(--color-text)]",
        done ? "text-[var(--color-accent-700)] opacity-100" : "opacity-0 group-hover:opacity-100",
      )}
    >
      {done ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
    </button>
  );
}
