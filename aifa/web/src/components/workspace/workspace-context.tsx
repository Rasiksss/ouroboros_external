"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { DocumentSummary } from "@/lib/api";
import type { Project } from "@/lib/projects";

/**
 * Общее состояние рабочего экрана — контракт между тремя пространствами.
 *
 * Панели независимы, но связаны сценариями: щёлкнул документ в ленте — сменилась
 * шапка и набор вкладок извлечённого; щёлкнул строку метрики — панель документа
 * доехала до страницы-источника; открыл артефакт — рабочая область показала
 * Excel/PDF. Всё это ходит через ЭТОТ контекст, чтобы панели не знали друг о
 * друге напрямую (иначе их нельзя было бы свободно отстёгивать и переставлять).
 */

/** Тип исходника определяет набор вкладок извлечённого под документом. */
export type SourceKind = "statements" | "press_release" | "transcript" | "other";

export type ArtifactRef = {
  id: string;
  name: string;
  kind: "xlsx" | "pdf" | "other";
};

/** Куда доехать в панели документа (клик по строке таблицы). */
export type SourceTarget = { docId: string; page: number; label?: string } | null;

/** Просьба к раскладке: какие панели свернуть/развернуть. Идёт через контекст,
 *  потому что просят её ЧУЖИЕ места (создание чата, отправка документа), а
 *  владеет раскладкой WorkspaceFrame. `seq` отличает повторные одинаковые
 *  просьбы — иначе второй «Новый чат» подряд не сработал бы. */
export type LayoutIntent = {
  seq: number;
  collapsed: Partial<Record<"chat" | "doc" | "ws", boolean>>;
};

/** Кусок рантайма хода (useChatRun из панели чата), нужный кнопкам рабочей
 *  области. Живёт в контексте, потому что подписка на прогон обязана быть
 *  ОДНА на чат — свой useChatRun в другой панели затирал бы копии проекта. */
export type WorkspaceChatRun = {
  streaming: boolean;
  send: (
    message: string,
    opts?: { pinnedTool?: string; pinnedToolArgs?: Record<string, unknown> },
  ) => Promise<void>;
};

type WorkspaceState = {
  project: Project | null;
  docs: DocumentSummary[];
  setDocs: (d: DocumentSummary[]) => void;

  activeDocId: string;
  setActiveDocId: (id: string) => void;
  activeDoc: DocumentSummary | null;
  /** Вид исходника активного документа — по нему строятся вкладки. */
  activeKind: SourceKind;

  /** Открытый артефакт (Excel/PDF) в рабочей области; null — список артефактов. */
  artifact: ArtifactRef | null;
  openArtifact: (a: ArtifactRef | null) => void;

  /** Переход к странице-источнику: ставится таблицей, читается панелью документа. */
  sourceTarget: SourceTarget;
  gotoSource: (t: SourceTarget) => void;

  /** Просьба к раскладке (см. LayoutIntent) — читает WorkspaceFrame. */
  layoutIntent: LayoutIntent | null;
  requestLayout: (collapsed: LayoutIntent["collapsed"]) => void;

  /** Рантайм хода чата (регистрирует ChatPane) — для «Извлечь метрики» и
   *  прочих кнопок рабочей области. null, пока панель чата не смонтирована. */
  chatRun: WorkspaceChatRun | null;
  setChatRun: (r: WorkspaceChatRun | null) => void;
};

const Ctx = createContext<WorkspaceState | null>(null);

/** Определить вид исходника по метаданным документа. */
export function sourceKindOf(doc: DocumentSummary | null): SourceKind {
  if (!doc) return "other";
  const t = (doc.doc_type || "").toLowerCase();
  if (t.includes("press")) return "press_release";
  if (t.includes("transcript")) return "transcript";
  if (t.includes("ifrs") || t.includes("rsbu") || t.includes("report")) return "statements";
  // Без типа судим по имени файла — документы старых загрузок его не несут.
  const n = (doc.filename || "").toLowerCase();
  if (n.includes("пресс") || n.includes("press") || n.includes("релиз")) return "press_release";
  if (n.includes("транскрипт") || n.includes("transcript") || n.includes("звонк")) return "transcript";
  return "statements";
}

/**
 * Как документ называется на экране: «Отчётность ОТП Банк», «Пресс-релиз ОТП
 * Банк» (правка Nate 18.08).
 *
 * Тип идёт ПЕРВЫМ словом, потому что при нескольких документах в чате именно
 * по нему их и различают глазами: компания у них одна, а отчёт, пресс-релиз и
 * транскрипт — разные. Раньше тип висел отдельной надписью в шапке рабочей
 * области («ОТЧЁТНОСТЬ») — лишний шум: зона и так про документ.
 *
 * ⚠️ ВРЕМЕННАЯ ЭВРИСТИКА. Настоящая классификация приедет из ветки мультидока
 * FinEngine (#273); тогда этот вывод заменяется её полем, а ручной правкой
 * останется PATCH /documents/{id}.
 */
export function docTypeRu(doc: DocumentSummary | null): string {
  const t = (doc?.doc_type || "").toLowerCase();
  if (t.includes("press")) return "Пресс-релиз";
  if (t.includes("transcript")) return "Транскрипт";
  if (t.includes("present")) return "Презентация";
  if (t.includes("excel")) return "Таблица";
  if (t.includes("ifrs") || t.includes("rsbu") || t.includes("report")) return "Отчётность";
  // Тип проставлен «прочим» ОСОЗНАННО — гадать по имени файла поверх этого
  // нельзя, иначе выбор пользователя в ручке типа не имел бы силы.
  if (t === "other") return "Документ";
  const kind = sourceKindOf(doc);
  if (kind === "press_release") return "Пресс-релиз";
  if (kind === "transcript") return "Транскрипт";
  if (kind === "statements") return "Отчётность";
  return "Документ";
}

/** Полное имя для вкладки и списка. Без документа — «Документы»: так вкладка
 *  называется до загрузки файлов. */
export function docTitle(doc: DocumentSummary | null): string {
  if (!doc) return "Документы";
  const name = (doc.company_name || "").trim() || doc.filename || "";
  return name ? `${docTypeRu(doc)} ${name}` : docTypeRu(doc);
}

export function WorkspaceProvider({
  project,
  children,
}: {
  project: Project | null;
  children: ReactNode;
}) {
  const [docs, setDocs] = useState<DocumentSummary[]>([]);
  const [activeDocId, setActiveDocId] = useState("");
  const [artifact, setArtifact] = useState<ArtifactRef | null>(null);
  const [sourceTarget, setSourceTarget] = useState<SourceTarget>(null);
  const [layoutIntent, setLayoutIntent] = useState<LayoutIntent | null>(null);
  const [chatRun, setChatRun] = useState<WorkspaceChatRun | null>(null);
  const layoutSeq = useRef(0);

  const activeDoc = useMemo(
    () => docs.find((d) => d.doc_id === activeDocId) ?? null,
    [docs, activeDocId],
  );

  const gotoSource = useCallback((t: SourceTarget) => {
    setSourceTarget(t);
    if (t) setActiveDocId(t.docId);
  }, []);

  const requestLayout = useCallback((collapsed: LayoutIntent["collapsed"]) => {
    layoutSeq.current += 1;
    setLayoutIntent({ seq: layoutSeq.current, collapsed });
  }, []);

  const value = useMemo<WorkspaceState>(
    () => ({
      project,
      docs,
      setDocs,
      activeDocId,
      setActiveDocId,
      activeDoc,
      activeKind: sourceKindOf(activeDoc),
      artifact,
      openArtifact: setArtifact,
      sourceTarget,
      gotoSource,
      layoutIntent,
      requestLayout,
      chatRun,
      setChatRun,
    }),
    [
      project,
      docs,
      activeDocId,
      activeDoc,
      artifact,
      sourceTarget,
      gotoSource,
      layoutIntent,
      requestLayout,
      chatRun,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useWorkspace(): WorkspaceState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace вне WorkspaceProvider");
  return v;
}

/** То же состояние, но `null` вне рабочего экрана — без исключения.
 *
 * Нужно тем компонентам, которые живут в ОБОИХ экранах: карточка анализа
 * рендерится и в рабочей области (провайдер есть, прыжок в документ работает),
 * и в старом чате `/` (провайдера нет). Строгий `useWorkspace` там уронил бы
 * весь ответ агента ради необязательной возможности. Получил null — просто не
 * делай ссылки: текст со страницей остаётся, кликать нечего.
 */
export function useWorkspaceOptional(): WorkspaceState | null {
  return useContext(Ctx);
}

/** Вкладки извлечённого по виду исходника (спека Nate). */
export const KIND_TABS: Record<SourceKind, { id: string; label: string }[]> = {
  statements: [
    { id: "metrics", label: "Показатели" },
    { id: "segments", label: "Сегменты" },
    { id: "group", label: "Состав холдинга" },
  ],
  press_release: [
    { id: "metrics", label: "Показатели" },
    { id: "events", label: "События" },
    { id: "insights", label: "Инсайты" },
  ],
  transcript: [
    { id: "guidance", label: "Guidance" },
    { id: "events", label: "События" },
    { id: "drivers", label: "Драйверы" },
    { id: "facts", label: "Факты" },
  ],
  other: [{ id: "metrics", label: "Показатели" }],
};
