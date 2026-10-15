"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronLeft,
  Download,
  ExternalLink,
  File as FileIcon,
  FileSpreadsheet,
  FileText,
  Loader2,
  RefreshCw,
  ScrollText,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import {
  getProjectSourceSheet,
  listProjectSources,
  projectSourceFileUrl,
  type AnalysisCard as AnalysisCardData,
  type AnalysisCardKpi,
  type ProjectSourceMeta,
  type SourceSheet,
} from "@/lib/api";
import { useAuth } from "@/components/auth-context";
import { triggerDownload } from "@/lib/download";
import { loadProjects, type Project } from "@/lib/projects";
import { useWorkspace, type ArtifactRef } from "@/components/workspace/workspace-context";
import { cn } from "@/lib/utils";
import { ExcelGrid } from "./excel-grid";

/**
 * Зона «Артефакты»: всё, что ход оставил после себя. Клик — открытие прямо
 * здесь (Excel сеткой, PDF страницей, анализ карточкой), кнопка возврата к списку.
 *
 * СПИСОК СКЛЕЕН ИЗ ДВУХ РАЗНЫХ ИСТОЧНИКОВ, и это не случайность:
 *  • файлы проекта (GET /projects/{id}/sources) — сюда движок кладёт Excel
 *    извлечения и факторного анализа, туда же попадают загрузки пользователя;
 *  • карточки анализа — файла на сервере у них НЕТ: fe_analyze возвращает только
 *    markdown + структуру (событие SSE analysis_card), поэтому карточка живёт в
 *    сообщении хода и берётся из него.
 *
 * Что открыто из ФАЙЛОВ — держит workspace-context (openArtifact/artifact), а не
 * локальное состояние: тот же артефакт может открыть другая панель или клик по
 * файлу в чате. Открытая карточка анализа — состояние панели: её ArtifactRef
 * описать нечем (нет file_id), и открывать её больше неоткуда.
 */

/** Сколько строк/колонок просим у ручки: больше в панель всё равно не влезает,
 *  а лишнее — мегабайты JSON. Ответ помечает обрезку флагом truncated. */
const SHEET_MAX_ROWS = 300;
const SHEET_MAX_COLS = 40;

/** Как часто перечитываем состав артефактов. Событие «в проекте появился файл»
 *  не существует ни на сервере, ни на клиенте: ход живёт в ChatPane (useChatRun),
 *  а эта панель о нём не знает — поэтому опрос. Зона размонтируется при уходе на
 *  «Документ», так что вне артефактов опроса нет; скрытая вкладка тоже молчит. */
const POLL_MS = 5000;

function extOf(filename: string): string {
  const i = filename.lastIndexOf(".");
  return i < 0 ? "" : filename.slice(i + 1).toLowerCase();
}

/** Тип артефакта: у ручки sources поля «тип» нет — выводим из имени файла.
 *  .xls тоже считаем таблицей, хотя сеткой он не откроется: пусть человек
 *  увидит внятный отказ от ручки, а не «формат не поддержан» от нас. */
function kindOf(filename: string): ArtifactRef["kind"] {
  const e = extOf(filename);
  if (e === "xlsx" || e === "xlsm" || e === "xls") return "xlsx";
  if (e === "pdf") return "pdf";
  return "other";
}

function fmtSize(n: number): string {
  if (n < 1024) return `${n} Б`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} КБ`;
  return `${(n / (1024 * 1024)).toFixed(1)} МБ`;
}

function fmtWhen(ms: number): string {
  return new Date(ms).toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtClock(ms: number): string {
  return new Date(ms).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

function KindIcon({ kind }: { kind: ArtifactRef["kind"] | "analysis" }) {
  const cls = "size-4 shrink-0 text-[var(--color-neutral-500)]";
  if (kind === "xlsx") return <FileSpreadsheet className={cls} />;
  if (kind === "pdf") return <FileText className={cls} />;
  if (kind === "analysis") return <ScrollText className={cls} />;
  return <FileIcon className={cls} />;
}

/** Скачивание идёт через blob, а не `<a download>`: бэкенд живёт на своём origin
 *  (NEXT_PUBLIC_API_BASE_URL), а для кросс-origin ссылки атрибут `download`
 *  игнорируется; сама ручка отдаёт файл с `Content-Disposition: inline`, так что
 *  ссылка ОТКРЫЛА бы файл вкладкой вместо сохранения на диск. */
async function saveSourceFile(
  projectId: string,
  fileId: string,
  filename: string,
): Promise<void> {
  try {
    const r = await fetch(projectSourceFileUrl(projectId, fileId), {
      credentials: "include",
      cache: "no-store",
    });
    if (!r.ok) throw new Error(`ручка файла ответила ${r.status}`);
    triggerDownload(await r.blob(), filename);
  } catch (e) {
    toast.error(`Не удалось скачать «${filename}»: ${(e as Error).message}`);
  }
}

// ───────────────────── источники списка ─────────────────────

type CardEntry = { id: string; when: number; card: AnalysisCardData };

/** Список файлов проекта переживает уход с вкладки: панель размонтируется, а
 *  пользователь ждёт мгновенного открытия, а не повторной загрузки. */
const _sourcesCache = new Map<string, SourceBox>();

const NO_CARDS: CardEntry[] = [];

function cardsOf(p: Project | null): CardEntry[] {
  if (!p) return NO_CARDS;
  const out: CardEntry[] = [];
  for (const m of p.messages) {
    if (m.role === "assistant" && m.analysisCard) {
      out.push({ id: m.id, when: m.created_at, card: m.analysisCard });
    }
  }
  return out;
}

/** Подпись состава списка — по ней решаем, менять ли состояние. Без этого
 *  каждый опрос подсовывал бы новый массив и дёргал перерисовку впустую. */
function sigOfSources(items: ProjectSourceMeta[]): string {
  return items.map((i) => `${i.file_id}:${i.size}:${i.uploaded_at}`).join("|");
}

/** Тик опроса: интервал, пока вкладка на экране, плюс мгновенный тик на возврат
 *  фокуса в окно и на кнопку «обновить». */
function useRefreshTick(): [number, () => void] {
  const [tick, setTick] = useState(0);
  const bump = useCallback(() => setTick((t) => t + 1), []);
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState === "visible") bump();
    }, POLL_MS);
    window.addEventListener("focus", bump);
    return () => {
      clearInterval(id);
      window.removeEventListener("focus", bump);
    };
  }, [bump]);
  return [tick, bump];
}

// ───────────────────── панель ─────────────────────

type SourceBox = { projectId: string; items: ProjectSourceMeta[]; failed: boolean };

type Entry =
  | { key: string; when: number; row: "file"; file: ProjectSourceMeta }
  | { key: string; when: number; row: "analysis"; card: CardEntry };

export function ArtifactsPane() {
  const { project, artifact, openArtifact } = useWorkspace();
  const { user } = useAuth();
  const username = user?.username ?? "";

  // Файлы агента лежат в неймспейсе ПАПКИ: ход шлёт project_id = folder_id ?? id
  // (use-chat-run). Спросить список по id чата — получить пустоту, хотя Excel
  // извлечения на сервере есть.
  const projectId = project ? (project.folder_id ?? project.id) : "";
  const chatId = project?.id ?? "";

  const [tick, refreshNow] = useRefreshTick();

  // Ответ храним вместе с id проекта, которому он принадлежит: пока для
  // текущего проекта ответа нет — это и есть «загружаю». Отдельного флага
  // загрузки нет намеренно, иначе при смене проекта на миг показывался бы
  // чужой список.
  const [box, setBox] = useState<SourceBox | null>(() => _sourcesCache.get(projectId) ?? null);
  const [checkedAt, setCheckedAt] = useState(0);

  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    listProjectSources(projectId)
      .then((items) => {
        if (cancelled) return;
        setCheckedAt(Date.now());
        setBox((prev) => {
          const next =
            prev &&
            prev.projectId === projectId &&
            !prev.failed &&
            sigOfSources(prev.items) === sigOfSources(items)
              ? prev
              : { projectId, items, failed: false };
          _sourcesCache.set(projectId, next);
          return next;
        });
      })
      .catch(() => {
        if (cancelled) return;
        setCheckedAt(Date.now());
        setBox((prev) =>
          prev && prev.projectId === projectId && prev.failed
            ? prev
            : { projectId, items: [], failed: true },
        );
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, tick]);

  // Карточки анализа берём из ЖИВОЙ копии чата в localStorage: ход пишет туда
  // (use-chat-run), а снимок проекта на странице не обновляется — иначе карточка
  // появлялась бы в списке только после перезагрузки страницы. Считаем на месте,
  // а не через состояние: при смене чата не будет кадра с чужими карточками.
  const cards = useMemo<CardEntry[]>(() => {
    if (!chatId) return NO_CARDS;
    const stored = username ? loadProjects(username).find((p) => p.id === chatId) : null;
    return cardsOf(stored ?? project);
    // tick в зависимостях намеренно: localStorage — не источник для React, о его
    // изменении узнать неоткуда, и перечитывание вешается ровно на тик опроса.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [username, chatId, project, tick]);

  const fresh = box && box.projectId === projectId ? box : null;

  // Открытая карточка ищется в живом списке: сменили чат — карточек нет, и зона
  // сама возвращается к списку, без эффекта на сброс.
  const [openCardId, setOpenCardId] = useState("");
  const openCard = cards.find((c) => c.id === openCardId) ?? null;

  const entries = useMemo<Entry[]>(() => {
    const files: Entry[] = (fresh?.items ?? []).map((f) => ({
      key: `f:${f.file_id}`,
      // uploaded_at приходит в секундах серверного времени, у карточки —
      // клиентские миллисекунды. В одной шкале порядок может слегка разъехаться,
      // если часы машины и сервера расходятся, — цифры при этом честные.
      when: f.uploaded_at * 1000,
      row: "file",
      file: f,
    }));
    const analyses: Entry[] = cards.map((c) => ({
      key: `a:${c.id}`,
      when: c.when,
      row: "analysis",
      card: c,
    }));
    return [...files, ...analyses].sort((a, b) => b.when - a.when);
  }, [fresh, cards]);

  if (!project) {
    return (
      <Shell>
        <p className="text-[12.5px] text-[var(--color-neutral-500)]">
          Проект не выбран. Артефакты живут внутри проекта — выберите его слева.
        </p>
      </Shell>
    );
  }

  if (artifact) {
    return (
      <ArtifactView
        projectId={projectId}
        artifact={artifact}
        onBack={() => openArtifact(null)}
      />
    );
  }

  if (openCard) {
    return <AnalysisView entry={openCard} onBack={() => setOpenCardId("")} />;
  }

  return (
    <Shell>
      <div className="flex shrink-0 items-center gap-2 pb-2">
        <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-600)]">
          Артефакты проекта
        </span>
        <span className="min-w-0 flex-1 truncate text-[10.5px] italic text-[var(--color-neutral-500)]">
          файлы проекта (загруженные и собранные агентом) и карточки анализа из
          переписки
        </span>
        {checkedAt > 0 && (
          <span
            title="Когда список последний раз перечитывали с сервера"
            className="shrink-0 text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]"
          >
            {fmtClock(checkedAt)}
          </span>
        )}
        <button
          type="button"
          onClick={refreshNow}
          title={`Перечитать список. Сам он обновляется каждые ${POLL_MS / 1000} с — события «появился файл» у сервера нет.`}
          className="grid size-6 shrink-0 place-items-center rounded-[var(--radius-token-sm)] text-[var(--color-neutral-500)] transition-colors hover:bg-[var(--color-accent-100)] hover:text-[var(--color-accent-700)]"
        >
          <RefreshCw className="size-3.5" />
        </button>
      </div>

      {!fresh && (
        <p className="flex items-center gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
          <Loader2 className="size-3.5 animate-spin" />
          Загружаю список…
        </p>
      )}

      {fresh?.failed && (
        <p className="text-[12.5px] text-[var(--color-neutral-600)]">
          Список файлов не пришёл — ручка{" "}
          <code className="text-[11.5px]">GET /projects/{projectId}/sources</code>{" "}
          ответила ошибкой. Файлов показать нечего
          {cards.length > 0 ? ", карточки анализа ниже — из переписки." : "."}
        </p>
      )}

      {fresh && !fresh.failed && !entries.length && (
        <p className="text-[12.5px] text-[var(--color-neutral-500)]">
          В проекте пока нет ни файлов, ни карточек анализа. Excel-таблица
          появляется здесь после прогона извлечения, карточка — после «Анализа».
        </p>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto">
        {entries.map((e) =>
          e.row === "file" ? (
            <Row
              key={e.key}
              kind={kindOf(e.file.filename)}
              badge={extOf(e.file.filename) || "файл"}
              name={e.file.filename}
              size={fmtSize(e.file.size)}
              when={fmtWhen(e.when)}
              onOpen={() => {
                setOpenCardId("");
                openArtifact({
                  id: e.file.file_id,
                  name: e.file.filename,
                  kind: kindOf(e.file.filename),
                });
              }}
              onDownload={() =>
                void saveSourceFile(projectId, e.file.file_id, e.file.filename)
              }
            />
          ) : (
            <Row
              key={e.key}
              kind="analysis"
              badge="анализ"
              name={cardTitle(e.card.card)}
              // Прочерк вместо размера — у карточки нет файла на сервере,
              // придумывать ей вес нельзя.
              size="—"
              sizeHint="файла на сервере нет: анализ возвращается текстом"
              when={fmtWhen(e.when)}
              onOpen={() => {
                openArtifact(null);
                setOpenCardId(e.card.id);
              }}
              onDownload={() => downloadCardMd(e.card.card)}
            />
          ),
        )}
      </div>
    </Shell>
  );
}

/** Общая рамка зоны — одинаковые поля в списке и в честных сообщениях. */
function Shell({ children }: { children: React.ReactNode }) {
  return <div className="flex min-h-0 flex-1 flex-col px-4 py-3">{children}</div>;
}

function Row({
  kind,
  badge,
  name,
  size,
  sizeHint,
  when,
  onOpen,
  onDownload,
}: {
  kind: ArtifactRef["kind"] | "analysis";
  badge: string;
  name: string;
  size: string;
  sizeHint?: string;
  when: string;
  onOpen: () => void;
  onDownload?: () => void;
}) {
  return (
    <div className="group flex items-center border-b border-[var(--color-divider)] transition-colors hover:bg-[var(--color-accent-100)]">
      <button
        type="button"
        onClick={onOpen}
        className="flex min-w-0 flex-1 items-center gap-3 px-1 py-2.5 text-left transition-[padding] group-hover:pl-2.5"
      >
        <KindIcon kind={kind} />
        {/* Колонка типа фиксированной ширины — иначе имена файлов не выстроятся
            в столбик; 52px хватает и на «xlsx», и на «анализ». */}
        <span className="w-[52px] shrink-0 text-[9.5px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)]">
          {badge}
        </span>
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-[var(--color-text)]" title={name}>
          {name}
        </span>
        <span
          title={sizeHint}
          className="shrink-0 text-[10.5px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]"
        >
          {size}
        </span>
        <span className="shrink-0 text-[10.5px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
          {when}
        </span>
      </button>
      {onDownload ? (
        <button
          type="button"
          onClick={onDownload}
          title="Скачать файл"
          className="mx-1 grid size-7 shrink-0 place-items-center rounded-[var(--radius-token-sm)] text-[var(--color-neutral-500)] transition-colors hover:bg-[var(--color-accent-200)] hover:text-[var(--color-accent-800)]"
        >
          <Download className="size-3.5" />
        </button>
      ) : (
        // Держим колонку кнопки, чтобы строки не «дышали» шириной.
        <span className="mx-1 size-7 shrink-0" />
      )}
    </div>
  );
}

// ───────────────────── открытый артефакт-файл ─────────────────────

type SheetState =
  | { phase: "loading" }
  | { phase: "ok"; data: SourceSheet }
  /** Лист не пришёл: code — статус ручки (404/413/415/422/…), detail — её текст. */
  | { phase: "failed"; code: number; detail: string };

/** getProjectSourceSheet кидает Error("<код>: <тело>") — разбираем обратно,
 *  потому что причина отказа у ручки разная и человеку нужно знать какая. */
function parseSheetError(e: unknown): { code: number; detail: string } {
  const raw = e instanceof Error ? e.message : String(e);
  const m = /^(\d{3}):\s*([\s\S]*)$/.exec(raw);
  if (!m) return { code: 0, detail: raw };
  let detail = m[2].trim();
  try {
    const parsed = JSON.parse(detail) as { detail?: unknown };
    if (typeof parsed.detail === "string") detail = parsed.detail;
  } catch {
    // тело не JSON — оставляем как есть
  }
  return { code: Number(m[1]), detail };
}

/** Человеческое объяснение отказа по коду ручки. */
function sheetFailure(code: number, detail: string): { title: string; lines: string[] } {
  if (code === 404) {
    return {
      title: "Лист не найден",
      lines: [
        "Ручка ответила 404: такого файла (или листа с таким именем) у проекта нет.",
        "Возможно, файл удалили из источников, пока артефакт был открыт.",
      ],
    };
  }
  if (code === 415) {
    return {
      title: "Сеткой этот формат не открывается",
      lines: [
        detail || "Сеткой показываются только .xlsx и .xlsm.",
        "Старый .xls нужно пересохранить в .xlsx — тогда лист откроется здесь.",
      ],
    };
  }
  if (code === 413) {
    return {
      title: "Файл слишком велик для сетки",
      lines: [detail, "Скачайте его по ссылке «Файл» и откройте в Excel."],
    };
  }
  if (code === 422) {
    return {
      title: "Файл не разобрался",
      lines: [
        detail || "Бэкенд не смог прочитать книгу.",
        "Показывать нечего — скачайте файл и проверьте его в Excel.",
      ],
    };
  }
  return {
    title: "Лист не пришёл",
    lines: [
      code ? `Ручка содержимого таблицы ответила ${code}: ${detail}` : `Запрос не прошёл: ${detail}`,
      "Сетку заполнить нечем. Файл можно открыть по ссылке «Файл» выше.",
    ],
  };
}

function ArtifactView({
  projectId,
  artifact,
  onBack,
}: {
  projectId: string;
  artifact: ArtifactRef;
  onBack: () => void;
}) {
  const fileUrl = projectSourceFileUrl(projectId, artifact.id);

  // Выбранный лист живёт вместе с id артефакта: открыли другой файл — выбор
  // сбрасывается сам, без эффекта и лишнего запроса.
  const [pick, setPick] = useState<{ id: string; sheet: string } | null>(null);
  const sheetName = pick?.id === artifact.id ? pick.sheet : "";

  // Ответ помечен тем запросом, на который он пришёл: пока ответа на текущий
  // (файл + лист) нет — это «читаю». Так со сменой листа не мелькает прошлый.
  const askedFor = `${artifact.id}|${sheetName}`;
  const [answer, setAnswer] = useState<{ key: string; state: SheetState } | null>(null);
  const sheet: SheetState =
    answer?.key === askedFor ? answer.state : { phase: "loading" };

  useEffect(() => {
    if (artifact.kind !== "xlsx") return;
    let cancelled = false;

    getProjectSourceSheet(projectId, artifact.id, {
      sheet: sheetName || undefined,
      maxRows: SHEET_MAX_ROWS,
      maxCols: SHEET_MAX_COLS,
    })
      .then((data) => {
        if (!cancelled) setAnswer({ key: askedFor, state: { phase: "ok", data } });
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setAnswer({ key: askedFor, state: { phase: "failed", ...parseSheetError(e) } });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [projectId, artifact.id, artifact.kind, sheetName, askedFor]);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ViewHeader name={artifact.name} kind={artifact.kind} onBack={onBack}>
        <a
          href={fileUrl}
          target="_blank"
          rel="noreferrer"
          title="Открыть файл в новом окне"
          className="flex shrink-0 items-center gap-1 text-[11.5px] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
        >
          <ExternalLink className="size-3.5" />
          Открыть
        </a>
        <button
          type="button"
          onClick={() => void saveSourceFile(projectId, artifact.id, artifact.name)}
          title="Сохранить файл на диск"
          className="flex shrink-0 items-center gap-1 text-[11.5px] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
        >
          <Download className="size-3.5" />
          Скачать
        </button>
      </ViewHeader>

      {artifact.kind === "pdf" && (
        // Тот же приём, что и в рабочем просмотрщике (components/pdf-viewer.tsx):
        // нативный вьюер браузера в iframe, cookie-сессия уходит сама.
        <iframe
          key={artifact.id}
          src={`${fileUrl}#view=FitH&toolbar=0&navpanes=0`}
          title={artifact.name}
          className="min-h-0 w-full flex-1 bg-[var(--color-neutral-100)]"
        />
      )}

      {artifact.kind === "other" && (
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          <p className="text-[12.5px] text-[var(--color-neutral-600)]">
            Просмотр для «{extOf(artifact.name) || "этого типа"}» в рабочей
            области не сделан — открыть можно только сам файл (кнопки выше).
            Сеткой показываем .xlsx, страницей — .pdf.
          </p>
        </div>
      )}

      {artifact.kind === "xlsx" && (
        <div className="flex min-h-0 flex-1 flex-col px-3 py-3">
          {sheet.phase === "loading" && (
            <p className="flex items-center gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
              <Loader2 className="size-3.5 animate-spin" />
              Читаю лист…
            </p>
          )}

          {sheet.phase === "ok" && (
            <ExcelGrid
              data={sheet.data}
              onPickSheet={(name) => setPick({ id: artifact.id, sheet: name })}
            />
          )}

          {sheet.phase === "failed" && (
            <Unavailable {...sheetFailure(sheet.code, sheet.detail)} />
          )}
        </div>
      )}
    </div>
  );
}

/** Шапка открытого артефакта — общая для файла и карточки анализа. */
function ViewHeader({
  name,
  kind,
  onBack,
  children,
}: {
  name: string;
  kind: ArtifactRef["kind"] | "analysis";
  onBack: () => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-divider)] px-3 py-2">
      <button
        type="button"
        onClick={onBack}
        className="flex items-center gap-1 text-[11.5px] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
      >
        <ChevronLeft className="size-3.5" />
        Все артефакты
      </button>
      <span className="h-3.5 w-px shrink-0 bg-[var(--color-divider)]" />
      <KindIcon kind={kind} />
      <span className="min-w-0 flex-1 truncate text-[12.5px]" title={name}>
        {name}
      </span>
      {children}
    </div>
  );
}

/** Честное «этого нет» — с точным указанием, чего именно не хватает. */
function Unavailable({ title, lines }: { title: string; lines: string[] }) {
  return (
    <div className="max-w-[560px] rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] p-4">
      <div className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-600)]">
        Недоступно
      </div>
      <div className="font-heading mt-1 text-[16px] leading-snug text-[var(--color-text)]">
        {title}
      </div>
      {lines.map((l, i) => (
        <p key={i} className="mt-2 text-[12.5px] leading-relaxed text-[var(--color-neutral-700)]">
          {l}
        </p>
      ))}
    </div>
  );
}

// ───────────────────── карточка анализа ─────────────────────

function cardTitle(card: AnalysisCardData): string {
  const head = [card.company, card.period].filter(Boolean).join(" · ");
  return head ? `Анализ — ${head}` : "Финансовый анализ";
}

function companyTypeLabel(t: string): string {
  const v = t.toLowerCase();
  if (v.includes("bank") || v.includes("банк")) return "банк";
  if (v.includes("non") || v.includes("нефин") || v.includes("corp")) return "нефин.";
  return t;
}

/** Статус показателя. Светофора в палитре редизайна нет — токены дают только
 *  акцент и нейтрали, поэтому важность передаём ПЛОТНОСТЬЮ акцента: спокойный
 *  нейтральный → подложка акцента → сплошной акцент. Так пилюли читаются во
 *  всех трёх темах, включая тёмный «Терминал». */
const FLAG_STYLE: Record<AnalysisCardKpi["flag"], { label: string; cls: string }> = {
  // «Позитивный» и «критический» — два конца той же шкалы плотности (#157-C):
  // позитивный светлее «нормы» акцентом, критический плотнее «риска» рамкой.
  // Отдельного цвета им не даём: светофора в палитре редизайна нет, а
  // выкрасить их «как-нибудь» значило бы сломать три темы разом.
  good: {
    label: "позитивный",
    cls: "bg-[var(--color-neutral-100)] text-[var(--color-neutral-900)] font-semibold",
  },
  ok: {
    label: "норма",
    cls: "bg-[var(--color-neutral-100)] text-[var(--color-neutral-700)]",
  },
  warn: {
    label: "внимание",
    cls: "bg-[var(--color-accent-100)] text-[var(--color-accent-700)]",
  },
  risk: {
    label: "риск",
    cls: "bg-[var(--color-accent)] text-[var(--color-surface)]",
  },
  crit: {
    label: "критический",
    cls: "bg-[var(--color-accent)] text-[var(--color-surface)] font-semibold ring-1 ring-[var(--color-accent-700)]",
  },
  na: {
    label: "нет данных",
    cls: "text-[var(--color-neutral-500)]",
  },
};

function AnalysisView({ entry, onBack }: { entry: CardEntry; onBack: () => void }) {
  const card = entry.card;
  const kpi = card.kpi ?? [];
  const sections = card.sections ?? [];
  const strengths = card.strengths ?? [];
  const watch = card.watch ?? [];
  const conclusions = card.conclusions ?? [];

  const meta = [
    card.period,
    card.company_type ? companyTypeLabel(card.company_type) : "",
    card.standard,
    card.language,
  ].filter(Boolean);

  const empty =
    !card.verdict &&
    !kpi.length &&
    !sections.length &&
    !strengths.length &&
    !watch.length &&
    !conclusions.length;


  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <ViewHeader name={cardTitle(card)} kind="analysis" onBack={onBack} />

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <div className="max-w-[720px]">
          {card.company && (
            <div className="font-heading text-[18px] leading-tight text-[var(--color-text)]">
              {card.company}
            </div>
          )}
          {meta.length > 0 && (
            <div className="mt-0.5 text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
              {meta.join(" · ")}
            </div>
          )}
          {/* Обложка: уровень риска ВЫШЕ вердикта и всех разделов. Он же
              показан в чат-карточке (analysis-card.tsx) — блок продублирован
              намеренно: панель редизайна рисует карточку своими токенами, и
              общий компонент притащил бы сюда чужую палитру. Уровень приезжает
              посчитанным правилом на бэкенде; здесь только показ. */}
          {card.risk_profile?.label && (
            <div className={cn(
              "mt-2 rounded-[var(--radius-token-sm)] border px-3 py-2",
              card.risk_profile.level === "critical" || card.risk_profile.level === "elevated"
                ? "border-[var(--color-accent-700)] bg-[var(--color-accent-100)]"
                : "border-[var(--color-divider)] bg-[var(--color-surface)]",
            )}>
              <div className="text-[12px] font-semibold text-[var(--color-text)]">
                Итоговый риск-профиль: {card.risk_profile.label.toUpperCase()}
              </div>
              {(card.risk_profile.why ?? []).length > 0 && (
                <ul className="mt-1 list-disc pl-4 space-y-0.5 text-[10.5px] leading-relaxed text-[var(--color-neutral-600)]">
                  {(card.risk_profile.why ?? []).map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <p className="mt-1 text-[10.5px] italic text-[var(--color-neutral-500)]">
            Разбор из ответа агента. Файла на сервере у анализа нет — скачивать
            нечего; показатели можно скопировать в Excel кнопкой выше.
          </p>

          {empty && (
            <p className="mt-3 text-[12.5px] text-[var(--color-neutral-600)]">
              Карточка пришла пустой — разбор ответа не нашёл ни вердикта, ни
              показателей, ни разделов. Текст анализа целиком остался в чате.
            </p>
          )}

          {card.verdict && (
            <div className="mt-3 rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] px-3 py-2.5">
              <div className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-600)]">
                Вердикт
              </div>
              <div className="mt-1 text-[13px] leading-relaxed text-[var(--color-text)]">
                {card.verdict}
              </div>
            </div>
          )}

          {kpi.length > 0 && (
            <div className="mt-4">
              <SectionTitle>Ключевые показатели</SectionTitle>
              <div className="mt-1.5 overflow-x-auto">
                <table className="w-full border-collapse text-[12.5px]">
                  <thead>
                    <tr className="border-b border-[var(--color-divider)] text-left text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-600)]">
                      <th className="py-1.5 pr-3 font-normal">Показатель</th>
                      <th className="py-1.5 px-3 text-right font-normal">Значение</th>
                      <th className="py-1.5 px-3 text-right font-normal">Пред.</th>
                      <th className="py-1.5 px-3 text-right font-normal">Δ</th>
                      <th className="py-1.5 pl-3 font-normal">Статус</th>
                    </tr>
                  </thead>
                  <tbody>
                    {kpi.map((k, i) => {
                      const style = FLAG_STYLE[k.flag] ?? FLAG_STYLE.na;
                      return (
                        <tr
                          key={`${k.name}-${i}`}
                          className="border-b border-[var(--color-divider)] align-top last:border-0"
                          title={k.why || undefined}
                        >
                          <td className="py-1.5 pr-3 text-[var(--color-text)]">{k.name}</td>
                          <td className="whitespace-nowrap py-1.5 px-3 text-right text-[var(--color-text)] [font-variant-numeric:tabular-nums]">
                            {k.value}
                          </td>
                          <td className="whitespace-nowrap py-1.5 px-3 text-right text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
                            {k.prev}
                          </td>
                          <td className="whitespace-nowrap py-1.5 px-3 text-right text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
                            {k.delta}
                          </td>
                          <td className="py-1.5 pl-3">
                            <span
                              className={cn(
                                "inline-flex items-center rounded-[var(--radius-token-sm)] px-2 py-0.5 text-[10px] uppercase tracking-[0.1em]",
                                style.cls,
                              )}
                            >
                              {style.label}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <p className="mt-1 text-[10.5px] italic text-[var(--color-neutral-500)]">
                Обоснование статуса — в подсказке к строке и в разделах ниже.
              </p>
            </div>
          )}

          {sections.map((s, i) => (
            <div key={`${s.title}-${i}`} className="mt-4">
              {s.title && <SectionTitle>{s.title}</SectionTitle>}
              {s.body && <Md text={s.body} />}
            </div>
          ))}

          {(strengths.length > 0 || watch.length > 0) && (
            <div
              className="mt-4 grid gap-3"
              style={{ gridTemplateColumns: "repeat(auto-fit, minmax(240px, 1fr))" }}
            >
              {strengths.length > 0 && (
                <div className="rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] p-3">
                  <SectionTitle>Сильные стороны</SectionTitle>
                  <Bullets items={strengths} />
                </div>
              )}
              {watch.length > 0 && (
                <div className="rounded-[var(--radius-token-sm)] border border-[var(--color-accent-300)] bg-[var(--color-accent-100)] p-3">
                  <SectionTitle>Зоны внимания</SectionTitle>
                  <Bullets items={watch} />
                </div>
              )}
            </div>
          )}

          {conclusions.length > 0 && (
            <div className="mt-4">
              <SectionTitle>Выводы</SectionTitle>
              <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-[12.5px] leading-relaxed text-[var(--color-text)]">
                {conclusions.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ol>
            </div>
          )}

          {card.footnote && (
            <p className="mt-4 border-t border-[var(--color-divider)] pt-2.5 text-[10.5px] text-[var(--color-neutral-500)]">
              {card.footnote}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="font-heading text-[15px] leading-snug text-[var(--color-text)]">{children}</div>
  );
}

function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="mt-1.5 list-disc space-y-1 pl-5 text-[12.5px] leading-relaxed text-[var(--color-text)]">
      {items.map((t, i) => (
        <li key={i}>{t}</li>
      ))}
    </ul>
  );
}

/**
 * Разметка тела раздела. Свой набор компонентов, а не общий с «/»: тот завязан
 * на палитру shadcn, которая не переключается вместе с тремя темами редизайна.
 * Тело раздела — сырой кусок markdown-отчёта, там бывают списки и таблицы.
 */
function Md({ text }: { text: string }) {
  return (
    <div className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--color-text)]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="my-1.5 list-disc pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="my-1.5 list-decimal pl-5">{children}</ol>,
          li: ({ children }) => <li className="my-0.5">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
          h1: ({ children }) => <SectionTitle>{children}</SectionTitle>,
          h2: ({ children }) => <SectionTitle>{children}</SectionTitle>,
          h3: ({ children }) => <SectionTitle>{children}</SectionTitle>,
          code: ({ children }) => (
            <code className="rounded-[var(--radius-token-sm)] bg-[var(--color-neutral-100)] px-1 py-0.5 text-[11.5px]">
              {children}
            </code>
          ),
          // Таблица бывает шире зоны — прокручиваем её саму, а не всю панель.
          table: ({ children }) => (
            <div className="my-2 overflow-x-auto">
              <table className="w-auto border-collapse text-[12px]">{children}</table>
            </div>
          ),
          th: ({ children }) => (
            <th className="border border-[var(--color-divider)] px-2 py-1 text-left font-medium [&:not(:first-child)]:text-right">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="border border-[var(--color-divider)] px-2 py-1 [font-variant-numeric:tabular-nums] [&:not(:first-child)]:whitespace-nowrap [&:not(:first-child)]:text-right">
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
 * Выгрузка разбора файлом. У анализа нет файла на сервере — он приходит
 * структурой в ответе агента, поэтому Markdown собираем из того же, что видно
 * на экране: ничего не досочиняем и ничего не теряем.
 */
function downloadCardMd(card: AnalysisCardData): void {
  const L: string[] = [];
  L.push(`# ${cardTitle(card)}`);
  const meta = [card.period, card.standard, card.language].filter(Boolean).join(" · ");
  if (meta) L.push(`_${meta}_`);
  if (card.verdict) L.push("\n## Вердикт\n", card.verdict);
  if (card.kpi?.length) {
    L.push("\n## Ключевые показатели\n");
    L.push("| Показатель | Значение | Пред. |", "|---|---:|---:|");
    for (const k of card.kpi) {
      L.push(`| ${k.name} | ${k.value ?? ""} | ${k.prev ?? ""} |`);
    }
  }
  for (const sec of card.sections ?? []) {
    L.push(`\n## ${sec.title}\n`, sec.body ?? "");
  }
  if (card.strengths?.length) L.push("\n## Сильные стороны\n", ...card.strengths.map((t) => `- ${t}`));
  if (card.watch?.length) L.push("\n## На что смотреть\n", ...card.watch.map((t) => `- ${t}`));
  if (card.conclusions?.length) L.push("\n## Выводы\n", ...card.conclusions.map((t) => `- ${t}`));

  const blob = new Blob([L.join("\n")], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `Анализ — ${cardTitle(card)}.md`.replace(/[\\/:*?"<>|]/g, "_");
  a.click();
  URL.revokeObjectURL(url);
}
