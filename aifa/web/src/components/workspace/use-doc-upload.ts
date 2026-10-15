"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { rememberDocName } from "@/lib/doc-names";
import { notify } from "@/lib/notices";
import { useAuth } from "@/components/auth-context";
import {
  getDocumentStatus,
  listDocuments,
  rememberLocalFile,
  startDocumentParse,
  uploadDocument,
} from "@/lib/api";
import { loadPrefs } from "@/lib/prefs";
import {
  detachDoc,
  lastStorageFailure,
  loadProjects,
  reportStorage,
  updateProject,
  uid,
  type Project,
} from "@/lib/projects";
import { useWorkspaceSafe } from "@/components/workspace/panes/files-in-chat";

/**
 * Загрузка документа на новом экране: перетаскивание в панель «Исходный
 * документ» и «+» в композере ведут сюда.
 *
 * Три вещи, из-за которых это не «просто fetch»:
 *
 * 1. Бэкенд отвечает СРАЗУ (`processing: true`), а текст документа появляется
 *    позже — фоновой задачей. События готовности нет вообще, есть только
 *    поллинг `/documents/{id}/status` (готово = сайдкар существует И непустой).
 *    Отсюда отдельная стадия «распознавание (OCR)» и потолок ожидания: облачный
 *    IDP-OCR медленный, но не бесконечный.
 * 2. Чат видит документ ТОЛЬКО через `project.doc_ids`, а владелец проекта на
 *    /v2 — localStorage. Пишем туда и будим страницу событием
 *    «finengine:projects»: `saveProjects` само по себе никого не уведомляет, а
 *    `storage` в своей же вкладке не стреляет.
 * 3. Упавший аплоад обязан быть виден С ИМЕНЕМ ФАЙЛА (прод-инцидент: 500 на
 *    длинном кириллическом имени → чип остался, ход ушёл без документа).
 *    Поэтому ошибка живёт в состоянии до закрытия крестиком, а не только в
 *    тосте, который через пару секунд исчезнет.
 *
 * ⚠️ ИЗВЕСТНЫЙ РАЗРЫВ (правится вне этого файла, в use-chat-run.ts): у хука
 * хода своя копия проекта, засеянная при переключении чата. Дописанные сюда
 * `doc_ids` она не видит — значит следующий ход уйдёт без свежего документа, а
 * дебаунс-персист хода перезапишет запись проекта своей копией. Пока `send` не
 * начнёт перечитывать проект из хранилища (или мержить doc_ids), загруженный
 * документ виден в панелях, но не в промпте агента.
 */

/** Шаг поллинга статуса — тот же, что на «/» (pdf-viewer и гейт кнопок). */
const POLL_MS = 2500;
/** Потолок ожидания OCR ≈ 5 минут. Дальше честно говорим, что не знаем. */
const POLL_LIMIT = 120;
/** Сколько держим плашку успешно загруженного файла, прежде чем убрать. */
const READY_LINGER_MS = 5000;

export type UploadStage =
  | "uploading"
  /** Файл на сервере, но разбор НЕ запускали: его просит ход чата (Enter). */
  | "queued"
  | "ocr"
  | "ready"
  | "error"
  | "unknown";

export type UploadItem = {
  id: string;
  /** Чат, в который грузили файл. Плашки НЕ стираются при переключении чата —
   *  они фильтруются по этому полю: стирание убивало плашку ещё летящего
   *  файла, и загрузка молча пропадала с экрана вместе с ошибкой. */
  chatId: string;
  filename: string;
  stage: UploadStage;
  doc_id?: string;
  /** Процент выгрузки файла (0–100) — показываем кружком у имени файла. */
  percent?: number;
  /** Причина — текст сервера/проверки как есть, без переформулировок. */
  detail?: string;
};

export type DocUpload = {
  items: UploadItem[];
  /** Хоть один файл льётся прямо сейчас — на это время гасим кнопку загрузки. */
  busy: boolean;
  upload: (files: File[]) => Promise<void>;
  /** Убрать плашку, не трогая документ (используется после разбора). */
  dismiss: (id: string) => void;
  /** Снять файл с отправки по крестику: плашка + отвязка документа от чата. */
  remove: (id: string) => void;
  /** Отвязать от чата документ БЕЗ плашки (персистентный чип из хранилища). */
  detachById: (docId: string) => void;
  /** Заказать разбор (OCR) — зовёт ход чата при отправке документа агенту. */
  startParseFor: (docIds: string[]) => Promise<void>;
  /** Убрать все чипы из композера: файлы ушли в сообщение, дальше их судьбу
   *  показывает панель документа. Ошибки оставляем — их нужно прочитать. */
  dismissAll: () => void;
  /** Дождаться завершения всех текущих загрузок (см. комментарий в хуке). */
  whenSettled: () => Promise<void>;
};

/** Единая формулировка стадии: панель документа и композер говорят одно и то же. */
export function uploadItemLabel(it: UploadItem): string {
  switch (it.stage) {
    case "uploading":
      return typeof it.percent === "number" ? `загружаю ${it.percent}%` : "загружаю…";
    case "queued":
      return "";
    case "ocr":
      return "распознаю (OCR)…";
    case "ready":
      return "";
    case "error":
      return `не загрузился: ${it.detail || "причина не сообщена"}`;
    case "unknown":
      return `статус неизвестен: ${it.detail || "сервер не ответил"}`;
  }
}

/** Путь документа (OCR + извлечение) в бэкенде — только PDF. */
function isPdf(f: File): boolean {
  return f.name.toLowerCase().endsWith(".pdf") || f.type === "application/pdf";
}

/** Дописать документы в проект в localStorage. false — проекта там уже нет. */
function attachDocIds(username: string, projectId: string, docIds: string[]): boolean {
  // Атомарно: список читается прямо перед записью, меняется только doc_ids.
  // Прежний вариант писал ВЕСЬ список из своей копии и затирал сообщения,
  // дописанные ходом чата в те же секунды.
  const updated = updateProject(username, projectId, (p) => ({
    ...p,
    doc_ids: Array.from(new Set([...p.doc_ids, ...docIds])),
  }));
  if (!updated) {
    // Проекта нет в хранилище — значит его кто-то стёр между загрузкой и
    // прикреплением. Раньше это было НЕМЫМ: сообщение об ошибке уходило в
    // плашку, а плашки к тому моменту уже могло не быть.
    reportStorage(
      "doc_attach_no_project",
      `docs=${docIds.length}; projects_on_disk=${loadProjects(username).length}`,
      projectId,
    );
    return false;
  }
  // Без этого события страница /v2 осталась бы на снимке, прочитанном при
  // монтировании: рельс, шапка и панель документа не узнали бы о новом файле.
  window.dispatchEvent(new Event("finengine:projects"));
  return true;
}

export function useDocUpload(project: Project | null): DocUpload {
  const { user } = useAuth();
  const username = user?.username ?? "";
  const ws = useWorkspaceSafe();
  const [allItems, setAllItems] = useState<UploadItem[]>([]);
  const chatId = project?.id ?? "";

  // Плашки принадлежат КОНКРЕТНОМУ чату (панели не размонтируются, поэтому без
  // привязки плашка от прошлого чата оставалась в композере нового и врала).
  // Именно ФИЛЬТР, а не стирание: пока файл летит, чат может на миг оказаться
  // не выбран (перечитывание списка, подтяжка с сервера) — стирание убивало бы
  // плашку и вместе с ней всю обратную связь, включая ошибку прикрепления.
  const items = useMemo(() => allItems.filter((i) => i.chatId === chatId), [allItems, chatId]);
  const setItems = setAllItems;

  // Аплоад и поллинг живут дольше рендера, в котором запущены, — проект и
  // контекст читаем через рефы, иначе поллинг доедет до уже другого чата.
  const wsRef = useRef(ws);
  const projectRef = useRef(project);
  useEffect(() => {
    wsRef.current = ws;
    projectRef.current = project;
  });

  const aliveRef = useRef(true);
  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  // startParseFor зовут ИЗВНЕ (из хода чата), поэтому список файлов читаем
  // через ref: замыкание на состояние к тому моменту уже протухло бы.
  const itemsRef = useRef<UploadItem[]>([]);
  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  const patchItem = useCallback((id: string, next: Partial<UploadItem>) => {
    setItems((prev) => prev.map((i) => (i.id === id ? { ...i, ...next } : i)));
  }, []);

  /** Убрать ТОЛЬКО плашку. Документ остаётся в чате — так гасится строка
   *  успешно разобранного файла (он уже в панели документа). */
  const dismiss = useCallback((id: string) => {
    setItems((prev) => prev.filter((i) => i.id !== id));
  }, []);

  /** Снятые пользователем файлы: если такой ещё летит, по приземлении его НЕ
   *  прикладываем — иначе отменённый документ вернулся бы в чат сам. */
  const cancelledRef = useRef<Set<string>>(new Set());

  /** Перечитать ленту документов проекта и положить её в общее состояние экрана. */
  const refreshDocs = useCallback(
    async (projectId: string, focusDocId?: string) => {
      const fresh = loadProjects(username).find((p) => p.id === projectId);
      const ids = new Set(fresh?.doc_ids ?? []);
      const all = await listDocuments().catch(() => null);
      if (!all || !aliveRef.current) return;
      if (projectRef.current?.id !== projectId) return; // пользователь ушёл в другой чат
      wsRef.current?.setDocs(all.filter((d) => ids.has(d.doc_id)));
      if (focusDocId) wsRef.current?.setActiveDocId(focusDocId);
    },
    [username],
  );

  /** Дождаться готовности текста документа. Событий нет — только поллинг. */
  const watchOcr = useCallback(
    async (itemId: string, docId: string, projectId: string) => {
      for (let i = 0; i < POLL_LIMIT; i++) {
        await new Promise((r) => setTimeout(r, POLL_MS));
        if (!aliveRef.current) return;
        try {
          const st = await getDocumentStatus(docId);
          if (st.processing) continue;
          patchItem(itemId, { stage: "ready" });
          setTimeout(() => dismiss(itemId), READY_LINGER_MS);
          // Число страниц и компанию бэкенд проставляет только ПОСЛЕ разбора:
          // без этого повтора вкладка осталась бы без «N стр.», а лента страниц
          // не собралась бы вовсе (её высота считается от числа страниц).
          await refreshDocs(projectId);
          return;
        } catch {
          // Разовая осечка статуса — продолжаем поллить, как на «/».
        }
      }
      patchItem(itemId, {
        stage: "unknown",
        detail: "готовность не подтверждена за 5 минут, слежение прекращено",
      });
    },
    [patchItem, dismiss, refreshDocs],
  );

  /**
   * Пользователь передумал отправлять файл (крестик на чипе). Убираем не только
   * плашку, но и сам документ из чата: иначе он остался бы в `doc_ids` и всё
   * равно ушёл бы агенту — крестик врал бы. Файл на сервере не трогаем: удалять
   * загруженное по снятию чипа мы не вправе.
   */
  const remove = useCallback(
    (id: string) => {
      const item = itemsRef.current.find((i) => i.id === id);
      cancelledRef.current.add(id);
      setItems((prev) => prev.filter((i) => i.id !== id));
      const projectId = projectRef.current?.id;
      if (!projectId || !username) return;
      if (item?.doc_id) {
        detachDoc(username, projectId, item.doc_id);
        void refreshDocs(projectId);
      }
    },
    [username, refreshDocs],
  );

  /**
   * Снять с чата документ, у которого уже НЕТ чипа-плашки (переоткрытие чата
   * стирает in-memory плашки, а документ в `doc_ids` остаётся). Для таких
   * композер рисует персистентный чип из хранилища — его крестик зовёт сюда.
   */
  const detachById = useCallback(
    (docId: string) => {
      const projectId = projectRef.current?.id;
      if (!projectId || !username || !docId) return;
      detachDoc(username, projectId, docId);
      void refreshDocs(projectId);
    },
    [username, refreshDocs],
  );

  /** Промис текущей пачки загрузок — его дожидается отправка сообщения. */
  const inflightRef = useRef<Promise<void> | null>(null);

  const uploadInner = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      const p = projectRef.current;
      if (!p) {
        toast.error("Выберите чат слева — документ некуда прикладывать.");
        return;
      }
      if (!username) {
        toast.error("Сессия не опознана — перезайдите: документ не к чему привязать.");
        return;
      }
      const projectId = p.id;

      // Не-PDF отбиваем сразу и честно: маршрут для xlsx/csv/txt — «Источники
      // проекта», на этом экране его ещё нет, и делать вид, что файл принят,
      // нельзя.
      const pdfs = files.filter(isPdf);
      const rejected = files.filter((f) => !isPdf(f));
      if (rejected.length) {
        const detail = "не PDF — на этом экране пока грузятся только PDF";
        setItems((prev) => [
          ...prev,
          ...rejected.map((f) => ({
            id: uid(),
            chatId: projectId,
            filename: f.name,
            stage: "error" as const,
            detail,
          })),
        ]);
        rejected.forEach((f) => toast.error(`${f.name}: ${detail}`));
      }
      if (!pdfs.length) return;

      const ocrEngine = loadPrefs().extraction?.ocrEngine ?? "pymupdf";
      const started = pdfs.map((file) => ({ file, id: uid() }));
      setItems((prev) => [
        ...prev,
        ...started.map((s) => ({
          id: s.id,
          chatId: projectId,
          filename: s.file.name,
          stage: "uploading" as const,
        })),
      ]);

      const results = await Promise.all(
        started.map(async (s) => {
          try {
            const r = await uploadDocument(s.file, {
              company_name: "",
              period: "",
              company_type: "unknown",
              ocr_engine: ocrEngine,
              // Разбор откладываем: его запросит ход чата, когда пользователь
              // отправит документ агенту. Иначе облачный OCR тратится на файл,
              // о котором ещё не сказали, что с ним делать.
              defer_parse: true,
              onProgress: (percent) => patchItem(s.id, { percent }),
            });
            // Имя кладём в общий реестр СРАЗУ: список документов приедет с
            // сервера позже, а чип в сообщении нужен уже в этом ходе.
            rememberDocName(r.doc_id, s.file.name);
            // Файл уже здесь — незачем скачивать его обратно ради просмотра
            // (см. rememberLocalFile: на медленном канале это стоило секунд
            // сверх и без того долгой заливки, причём дважды).
            rememberLocalFile(r.doc_id, s.file);
            patchItem(s.id, { stage: "queued", doc_id: r.doc_id, percent: 100 });
            return { itemId: s.id, docId: r.doc_id };
          } catch (e) {
            const msg = (e as Error).message || "не удалось загрузить";
            patchItem(s.id, { stage: "error", detail: msg });
            toast.error(`${s.file.name}: ${msg}`);
            return null;
          }
        }),
      );
      const ok = results
        .filter((x): x is { itemId: string; docId: string } => x !== null)
        // Файл, снятый крестиком, пока летел, прикладывать нельзя — он бы
        // вернулся в чат сам, уже без плашки, и ушёл бы агенту незаметно.
        .filter((o) => !cancelledRef.current.has(o.itemId));
      if (!ok.length) return;

      const attached = attachDocIds(
        username,
        projectId,
        ok.map((o) => o.docId),
      );
      if (!attached) {
        // ⚠️ ПРИЧИНА, А НЕ СИМПТОМ (09.08.2026). Здесь стояло «чат не найден в
        // хранилище — обновите страницу». У владельца это выстрелило при
        // переполненном localStorage (168 чатов, QuotaExceededError): чат не
        // сохранился ИМЕННО поэтому, а совет обновить страницу места не
        // освобождает — человек жмёт обновление по кругу и остаётся ни с чем.
        const fail = lastStorageFailure();
        const why = fail?.quota
          ? `хранилище браузера переполнено (${fail.projects} чатов) — удалите старые чаты и повторите загрузку`
          : "файл загружен, но чат не найден в хранилище — обновите страницу";
        ok.forEach((o) => patchItem(o.itemId, { stage: "error", detail: why }));
        // Плашка могла быть уже не видна (ушли в другой чат) — тогда ошибка
        // осталась бы немой. Уведомление живёт в истории и не зависит от неё.
        notify("error", `Документ не прикреплён к чату: ${why}`);
        return;
      }

      await refreshDocs(projectId, ok[0].docId);
      // Слежение за разбором НЕ запускаем: разбора ещё нет. Его закажет ход
      // чата, и тогда же начнётся слежение (см. startParseFor).
    },
    [username, patchItem, refreshDocs],
  );

  const upload = useCallback(
    async (files: File[]) => {
      const task = uploadInner(files);
      inflightRef.current = task;
      try {
        await task;
      } finally {
        if (inflightRef.current === task) inflightRef.current = null;
      }
    },
    [uploadInner],
  );

  /** Дождаться, пока долетят все файлы. Отправка хода зовёт это ПЕРЕД стартом:
   *  ход, ушедший на 99% загрузки, уходил без документа, и агент спрашивал
   *  «где отчётность?» — при том что файл вот-вот дозагрузился. */
  const whenSettled = useCallback(async () => {
    // Цикл, а не одиночное ожидание: пока ждали первую пачку, пользователь мог
    // добросить ещё файлы.
    while (inflightRef.current) await inflightRef.current;
  }, []);

  /**
   * Заказать разбор ранее загруженных файлов. Зовётся из хода чата в момент
   * отправки: пользователь сказал агенту, что с документами делать — значит
   * распознавание теперь оправдано.
   */
  const startParseFor = useCallback(
    async (docIds: string[]) => {
      const projectId = projectRef.current?.id;
      if (!projectId || !docIds.length) return;
      await Promise.all(
        docIds.map(async (docId) => {
          const item = itemsRef.current.find((i) => i.doc_id === docId);
          try {
            await startDocumentParse(docId);
          } catch {
            // Ручка могла не ответить — статус всё равно поллим ниже: если
            // разбор уже шёл (документ из прошлой сессии), он завершится сам.
          }
          if (item) {
            patchItem(item.id, { stage: "ocr" });
            void watchOcr(item.id, docId, projectId);
          }
        }),
      );
    },
    [patchItem, watchOcr],
  );

  const dismissAll = useCallback(() => {
    // Только ЭТОТ чат: плашки соседних чатов не наши, чтобы их гасить.
    setItems((prev) =>
      prev.filter(
        (i) =>
          i.chatId !== projectRef.current?.id ||
          i.stage === "error" ||
          i.stage === "unknown",
      ),
    );
  }, []);

  return {
    items,
    busy: items.some((i) => i.stage === "uploading"),
    upload,
    dismiss,
    remove,
    detachById,
    startParseFor,
    dismissAll,
    whenSettled,
  };
}

