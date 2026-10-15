"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { notify, reportClientEvent } from "@/lib/notices";
import { AUTO_RETRY_MAX, вердиктОбрыва, можноПодписаться } from "@/lib/link-watchdog";
import { docNameOf } from "@/lib/doc-names";
import { syncDialogsDebounced } from "@/lib/dialog-sync";
import { useAuth } from "@/components/auth-context";
import {
  cancelChatTurn,
  fetchChatHistory,
  chatRunStream,
  resumeExtractRunStream,
  startChatTurn,
  type DocumentSummary,
  type ExtractStreamEvent,
} from "@/lib/api";
import { missingAssistantTail } from "@/lib/late-answer";
import { loadPrefs } from "@/lib/prefs";
import {
  createRunProbe,
  reconnectRun,
  RECONNECT_DELAYS_MS,
  type InterruptedState,
} from "@/lib/run-recovery";
import {
  resetForReplay,
  applyChatEvent,
  applyExtractEvent,
  demoteProseToNote,
  extractionFromStart,
  findRunningExtraction,
  loadFolders,
  loadProjects,
  loadTombstoneIds,
  updateProject,
  uid,
  type ExtractionMessage,
  type Project,
  type TextMessage,
} from "@/lib/projects";

/**
 * Рантайм хода чата для нового экрана: отправка, подписка на поток, реаттач
 * после перезагрузки, «Стоп».
 *
 * Ход = ФОНОВЫЙ ПРОГОН НА СЕРВЕРЕ: POST /chat/turns отдаёт run_id мгновенно,
 * ответ дописывается независимо от браузера, а GET /chat/runs/{id}/stream
 * реплеит его с начала и отдаёт живой хвост. Отсюда два следствия, вокруг
 * которых построен весь хук: (1) один и тот же потребитель событий обслуживает
 * и живую отправку, и реаттач; (2) незавершённый ход обязан пережить F5 —
 * поэтому маркер `activeChatRun` пишется в проект и сохраняется.
 *
 * ПОЧЕМУ ХУК ДЕРЖИТ СВОЮ КОПИЮ ПРОЕКТА. Страница /v2 читает проекты из
 * localStorage, но ничего не сохраняет и не отдаёт наружу сеттер — зацепиться
 * реаттачу было бы не за что. Пока страница не научится владеть состоянием
 * (это отдельная правка вне периметра этого хука), живая копия и персист живут
 * здесь: пишем в тот же ключ localStorage, откуда /v2 читает. На сервер
 * (/dialogs) НЕ зеркалим осознанно — /v2 не подтягивает серверную копию при
 * загрузке, и слепой PUT затёр бы историю, пришедшую с другого устройства.
 */

/** Ход завис, если событий нет 5 минут — рвём подписку, чтобы «Стоп» не висел вечно. */
const IDLE_MS = 5 * 60 * 1000;
/** Тишина связи: 60 с — четыре пропущенных биения подряд (биение раз в 15 с). */
const LINK_MS = 60 * 1000;
/** Тишина работы: 15 мин — больше самого длинного законного молчания (14,4 мин). */
const STALLED_MS = 15 * 60 * 1000;

export type ChatRun = {
  /** Живая копия проекта — сюда стримятся дельты хода. Рисовать надо ЕЁ. */
  project: Project | null;
  /** Идёт ход этого чата (живая отправка или реаттач). */
  streaming: boolean;
  send: (message: string, opts?: SendOpts) => Promise<void>;
  stop: () => void;
  /** Догнать ответ вручную после неудачной автопереподписки («Догнать ответ»). */
  reconnect: () => void;
};

/** Шорткаты (МОДЕЛЬ ВХОДОВ): кнопка фиксирует решение — тул и его аргументы.
 * С аргументами бэкенд зовёт тул НАПРЯМУЮ (детерминированно, без модели);
 * без аргументов — pin через tool_choice (модель обязана позвать этот тул). */
export type SendOpts = {
  pinnedTool?: string;
  pinnedToolArgs?: Record<string, unknown>;
  /** Документы, которые пользователь ВИДИТ приложенными в композере. Страховка
   * от рассинхрона с хранилищем: что показано на экране, то и уходит с ходом. */
  extraDocIds?: string[];
};

/**
 * Догон позднего ответа: шаг опроса и потолок ожидания.
 *
 * 🔴 БЫЛО ШЕСТЬ ШАГОВ ПО ДЕСЯТЬ СЕКУНД — ОДНА МИНУТА, и это было прямой
 * ошибкой в рассуждении (24.08.2026). Я считал так: «сервер даёт не больше
 * трёх продолжений, дольше ждать нечего». Но продолжение начинается не сразу —
 * оно начинается, КОГДА ЗАКОНЧИТСЯ ПРОГОН, а прогон идёт до двадцати пяти
 * минут. То есть догон успевал сдаться раньше, чем на сервере вообще
 * появлялось, что догонять.
 *
 * Владелец увидел ровно это: «Ход завершён без текстового ответа» в чате и
 * «Извлечение идёт» в рабочей области — и ответ не приходил никогда.
 *
 * Потолок теперь считается от ОБЕЩАНИЯ навыка (25 минут работы) плюс время на
 * само продолжение, а шаг растянут: одинаковый опрос раз в десять секунд
 * полчаса — это две сотни лишних запросов ни за чем.
 */
const LATE_ANSWER_STEP_MS = 15_000;
const LATE_ANSWER_MAX_MS = 30 * 60_000;
/** Кругов без живого прогона, после которых догон уходит. */
const БЕЗ_ПРОГОНА_КРУГОВ = 4;

export function useChatRun(
  source: Project | null,
  opts: {
    /** Список документов — из него берётся человеческое имя для карточки извлечения. */
    docs?: DocumentSummary[];
    /** Документ, открытый в панели исходника, — уходит в ход подсказкой
     * бэкенду: запрос без уточнения бьёт в него, а не в первый из списка. */
    activeDocId?: string;
    /** Агент начал извлечение по документу — панель документа может доехать до него. */
    onFocusDoc?: (docId: string) => void;
    /** Ход уходит с документами: заказать их разбор и открыть панель документа. */
    /** `docIds` — все документы хода; `freshIds` — только те, что приложены
     * ИМЕННО этим ходом (пустой на всех последующих сообщениях в чате). */
    onTurnDocs?: (docIds: string[], freshIds: string[]) => void;
  } = {},
): ChatRun {
  const { user } = useAuth();
  const username = user?.username ?? "";

  const [project, setProject] = useState<Project | null>(source);
  const [streaming, setStreaming] = useState(false);
  // Будильник реаттача. Обрыв живого потока не меняет НИ ОДНОЙ зависимости
  // реаттач-эффекта (id чата и run_id те же, followedRuns — реф), поэтому
  // написанная там переподписка не выполнялась ни разу: пользователь оставался
  // с «Load failed» и уходил перезагружать страницу руками.
  const [dropTick, setDropTick] = useState(0);

  // Смена чата в рельсе: пересеваем копию прямо в рендере (React-паттерн
  // «adjusting state during render») — через эффект был бы кадр с чужой
  // перепиской. Берём при этом СВЕЖУЮ версию из localStorage, а не проп:
  // список на странице читается один раз при монтировании и ничего не знает
  // о ходах, которые дописал этот хук (в т.ч. в этом же чате минуту назад).
  const [seededId, setSeededId] = useState<string | null>(source?.id ?? null);
  if ((source?.id ?? null) !== seededId) {
    setSeededId(source?.id ?? null);
    const stored = source ? loadProjects(username).find((p) => p.id === source.id) : null;
    setProject(stored ?? source);
  }

  // Асинхронные потребители (стрим, обработчики) читают проект и опции через
  // рефы: замыкание живёт дольше рендера, в котором создано.
  const projectRef = useRef<Project | null>(project);
  const optsRef = useRef(opts);
  useEffect(() => {
    projectRef.current = project;
    optsRef.current = opts;
  });

  const abortRef = useRef<AbortController | null>(null);
  // Гвард от ДВОЙНОЙ подписки на один ран. Не state: ререндер тут не нужен, а
  // запись обязана быть видна синхронно (см. комментарий в send). Хранится
  // именно КОНТРОЛЛЕР подписки, а не просто флаг: при уходе с чата гвард
  // чистится целиком, и снимать запись «своего» рана из finally можно только
  // убедившись, что она всё ещё наша, — иначе finally брошенной подписки снял
  // бы гвард у новой и вернул дубль дельт.
  const followedRuns = useRef<Map<string, AbortController>>(new Map());
  // Автодогон после обрыва связи. Потолок нужен: прогон живёт до 25 минут, и
  // без него мёртвая сеть крутила бы переподписку раз в минуту до закрытия
  // вкладки. Исчерпали — отдаём решение человеку кнопкой «Догнать ответ».
  const autoRetries = useRef(0);
  const followedBgRuns = useRef<Set<string>>(new Set());

  const patch = useCallback((projectId: string, fn: (p: Project) => Project) => {
    setProject((p) => (p && p.id === projectId ? { ...fn(p), updated_at: Date.now() } : p));
  }, []);

  // ── Восстановление пропавшего треда из серверной сессии ──────────────────
  // Клиентская лента может быть повреждена (гонки писателей, потерянная
  // вкладка). Серверная сессия пишется агентом на каждом ходе — если локально
  // пусто, а session_id есть, забираем историю оттуда и кладём обратно.
  const recoveredRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    const p = project;
    if (!p || !p.session_id) return;
    if ((p.messages?.length ?? 0) > 0) return;
    if (recoveredRef.current.has(p.id)) return;
    recoveredRef.current.add(p.id);
    let cancelled = false;
    (async () => {
      const h = await fetchChatHistory(p.session_id!).catch(() => null);
      if (cancelled || !h?.history?.length) return;
      const base = Date.now() - h.history.length * 1000;
      const msgs: TextMessage[] = [];
      let pendingTools: { name: string; count: number; status: "done"; args_summary: string }[] = [];
      h.history.forEach((e, i) => {
        if (e.role === "tool_call") {
          pendingTools.push({ name: e.name, count: 1, status: "done", args_summary: "" });
          return;
        }
        const m: TextMessage = {
          id: uid(),
          role: e.role,
          content: e.content,
          created_at: base + i * 1000,
        };
        if (e.role === "assistant" && pendingTools.length) {
          m.toolCalls = pendingTools;
          pendingTools = [];
        }
        msgs.push(m);
      });
      if (!msgs.length) return;
      patch(p.id, (pp) => ((pp.messages?.length ?? 0) > 0 ? pp : { ...pp, messages: msgs }));
      notify("info", "Переписка восстановлена из серверной копии.");
      reportClientEvent({ kind: "thread_recovered", project_id: p.id, detail: `${msgs.length} msgs` });
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [project?.id, (project?.messages?.length ?? 0) === 0]);

  // Персист в тот же ключ localStorage, откуда /v2 читает проекты.
  //
  // Было: дебаунс 400 мс, который ОТМЕНЯЛСЯ при размонтировании и при каждой
  // следующей дельте. Уход со страницы, переключение вкладки браузера или
  // просто конец потока — и последние сообщения не доезжали до хранилища;
  // вернулся — переписка пустая. Плюс писался ВЕСЬ список целиком, затирая
  // чужие изменения.
  //
  // Стало: троттлинг (пишем не чаще раза в 400 мс, но пишем ОБЯЗАТЕЛЬНО) через
  // атомарный updateProject, который сливает сообщения по id. Плюс досрочный
  // сброс при уходе со страницы и сворачивании вкладки.
  const pendingRef = useRef<Project | null>(null);
  const lastWriteRef = useRef(0);
  const flushTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(() => {
    const p = pendingRef.current;
    if (!username || !p) return;
    pendingRef.current = null;
    lastWriteRef.current = Date.now();
    updateProject(username, p.id, () => p);
    // Сервер — источник истины списка чатов: локальное хранилище может убить
    // старая вкладка или чистка браузера, серверную копию — нет. Пуш дебаунсит
    // сам dialog-sync (1.5 c), так что дельты не спамят сеть.
    syncDialogsDebounced({
      projects: loadProjects(username),
      folders: loadFolders(username),
      last_project_id: p.id,
      deleted_project_ids: loadTombstoneIds(username),
    });
    // Страница и соседние панели (рабочая область, рельс) читают проекты из
    // хранилища и обновляются ТОЛЬКО по этому событию — без него карточка
    // извлечения из чат-хода не доезжала до зоны «Документ».
    window.dispatchEvent(new Event("finengine:projects"));
  }, [username]);

  useEffect(() => {
    if (!username || !project) return;
    pendingRef.current = project;
    const since = Date.now() - lastWriteRef.current;
    if (since >= 400) {
      flush();
      return;
    }
    if (flushTimerRef.current) clearTimeout(flushTimerRef.current);
    flushTimerRef.current = setTimeout(flush, 400 - since);
  }, [project, username, flush]);

  // Вкладку свернули/закрыли — дописываем немедленно: фоновая вкладка тормозит
  // таймеры, и отложенная запись могла не состояться вовсе.
  useEffect(() => {
    const onHide = () => flush();
    document.addEventListener("visibilitychange", onHide);
    window.addEventListener("pagehide", onHide);
    return () => {
      document.removeEventListener("visibilitychange", onHide);
      window.removeEventListener("pagehide", onHide);
      // Размонтирование НЕ должно съедать несохранённое.
      flush();
    };
  }, [flush]);

  /**
   * Фоновый воркер извлечения: чат-ход анонсирует run_id (`bg_runs`), карточка
   * собирается из отдельного реплей-стрима. Создаём её ТОЛЬКО на событии
   * `start` — если спавна не было, поток тихо закроется и мусора в треде не
   * останется.
   */
  const followBgRun = useCallback(
    (projectId: string, runId: string) => {
      if (followedBgRuns.current.has(runId)) return;
      followedBgRuns.current.add(runId);
      void (async () => {
        let emId: string | null = null;
        try {
          for await (const ev of resumeExtractRunStream(runId)) {
            if (ev.type === "run_id" || ev.type === "session_lost") continue;
            if (ev.type === "start" && !emId) {
              const id = uid();
              emId = id;
              const em: ExtractionMessage = extractionFromStart(ev, {
                id,
                run_id: runId,
                // Диспетчер (#116) кладёт doc_id в стартовое событие — карточка
                // привязывается к документу, и рабочая область фильтром его видит.
                doc_id: (ev as { doc_id?: string }).doc_id || "",
                doc_label: ev.company_name || "фоновое извлечение",
                at: Date.now(),
              });
              patch(projectId, (p) => ({ ...p, messages: [...p.messages, em] }));
              continue;
            }
            if (!emId) continue;
            const id = emId;
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === id && m.role === "extraction" ? applyExtractEvent(m, ev) : m,
              ),
            }));
            if (ev.type === "done" || ev.type === "error") break;
          }
        } catch {
          // Поток оборвался — итог доложит сам чат-ход; зомби-карточку гасим.
          if (emId) {
            const id = emId;
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === id && m.role === "extraction" && m.status === "running"
                  ? { ...m, status: "aborted" as const }
                  : m,
              ),
            }));
          }
        } finally {
          followedBgRuns.current.delete(runId);
        }
      })();
    },
    [patch],
  );

  /**
   * 🔴 ХОД КОНЧИЛСЯ, А РАБОТА — НЕТ (дефект владельца 23.08.2026).
   *
   * Прогон станции длиннее хода — нормальный исход: извлечение идёт минутами,
   * ход кончается раньше. Сервер для этого возвращает агента в петлю, тот
   * дописывает ответ и кладёт его в серверную сессию. Но SSE хода к этому
   * моменту закрыт, а восстановление треда срабатывало ТОЛЬКО на ПУСТОЙ ленте —
   * то есть в единственном случае, когда лента непуста и ждёт последнее слово,
   * оно не приезжало никогда. Человек десять минут смотрел в молчащий чат при
   * готовой работе (нонс 8eefde6f: агент ответил через минуту после конца хода,
   * ответ остался лежать на сервере).
   *
   * Догоняем ОПРОСОМ, а не подпиской: подписки к этому моменту нет ни одной, а
   * заводить новую ради одного сообщения дороже, чем несколько запросов раз в
   * десять секунд. Потолок жёсткий: продолжений у сервера не больше трёх, и
   * висеть здесь дольше пары минут не за чем.
   */
  const chasing = useRef<Set<string>>(new Set());
  const chaseLateAnswer = useCallback(
    (projectId: string) => {
      // 🔴 ПРИБОРЫ, А НЕ ДОГАДКИ (24.08.2026). Прогон 105f8dce: сервер отдал в
      // браузер 3690 знаков ответа (журнал хода: `text_delta×435` прямо перед
      // `done`), а человек увидел его только после обновления страницы. Значит
      // теряется на клиенте — но ГДЕ именно, из кода не следует: догон мог не
      // стартовать, уйти по сроку или не найти расхождения. Каждый мой заход
      // «починить по рассуждению» стоил владельцу прогона; поэтому здесь не
      // правка, а запись в журнал клиента — следующий прогон скажет сам.
      if (chasing.current.has(projectId)) return;
      const sid = projectRef.current?.session_id;
      if (!sid) {
        reportClientEvent({ kind: "late_answer_no_session", project_id: projectId });
        return;
      }
      chasing.current.add(projectId);
      reportClientEvent({
        kind: "late_answer_started",
        project_id: projectId,
        detail: `msgs=${projectRef.current?.messages?.length ?? 0}; `
          + `assistant=${(projectRef.current?.messages ?? []).filter(
              (m) => m.role === "assistant" && (m as { content?: string }).content?.trim()).length}`,
      });
      const дедлайн = Date.now() + LATE_ANSWER_MAX_MS;
      let пустых = 0;
      void (async () => {
        try {
          while (Date.now() < дедлайн) {
            await new Promise((r) => setTimeout(r, LATE_ANSWER_STEP_MS));
            const cur = projectRef.current;
            // Начался новый ход — догонять нечего, он всё покажет сам.
            if (!cur || cur.id !== projectId || cur.activeChatRun) {
              // Прибор, не поведение: этот выход был ЕДИНСТВЕННЫМ без своего
              // события — по журналу 19:47–19:49 (кейс 9d2dad93) причину
              // конца догона пришлось восстанавливать перебором веток.
              reportClientEvent({
                kind: "late_answer_yielded",
                project_id: projectId,
                detail: !cur ? "проект пропал"
                  : cur.id !== projectId ? "переключился чат"
                  : "появился живой ход",
              });
              return;
            }
            // 🔴 ЖДЁМ, ПОКА ПРОГОН ЖИВ, А НЕ ФИКСИРОВАННУЮ МИНУТУ. Ответ
            // появляется на сервере только ПОСЛЕ конца прогона — до тех пор
            // догонять нечего, но и сдаваться незачем. Карточка извлечения в
            // состоянии «идёт» и есть признак, что работа продолжается.
            //
            // Прогона нет — даём ещё несколько кругов и уходим: это обычный
            // короткий ход, у которого ответ либо уже пришёл, либо не придёт.
            if (!findRunningExtraction(cur, null)) {
              пустых += 1;
              if (пустых > БЕЗ_ПРОГОНА_КРУГОВ) {
                reportClientEvent({
                  kind: "late_answer_gave_up", project_id: projectId,
                  detail: `живого прогона нет ${пустых} кругов подряд`,
                });
                return;
              }
            } else {
              пустых = 0;
            }
            const h = await fetchChatHistory(sid).catch(() => null);
            if (!h?.history?.length) continue;
            const tail = missingAssistantTail(
              (cur.messages ?? []).map((m) => ({
                role: m.role,
                content: (m as { content?: string }).content,
              })),
              h.history,
            );
            if (!tail.length) continue;
            reportClientEvent({
              kind: "late_answer_appended", project_id: projectId,
              detail: `дописано ${tail.length}, знаков ${tail.join("").length}`,
            });
            patch(projectId, (pp) => ({
              ...pp,
              messages: [
                ...pp.messages,
                ...tail.map((content) => ({
                  id: uid(),
                  role: "assistant" as const,
                  content,
                  created_at: Date.now(),
                })),
              ],
            }));
            return;
          }
        } finally {
          reportClientEvent({ kind: "late_answer_finished", project_id: projectId });
          chasing.current.delete(projectId);
        }
      })();
    },
    [patch],
  );

  /** Состояние прерванного хода — ПОЛЕМ сообщения, а не строкой в его тексте.
   *  Строкой нельзя: гейт отправки считает непустой content законченным ходом
   *  (см. `send`), и «⚠️ Load failed» в тексте разрешал повторный запуск —
   *  тот затирал маркер живого прогона вместе с готовым результатом. */
  const markInterrupted = useCallback(
    (projectId: string, assistantId: string, state: InterruptedState | undefined) => {
      patch(projectId, (p) => ({
        ...p,
        messages: p.messages.map((m) =>
          m.id === assistantId && m.role === "assistant" ? { ...m, interrupted: state } : m,
        ),
      }));
    },
    [patch],
  );

  /**
   * ЕДИНЫЙ потребитель событий хода — и для живой отправки, и для реаттача.
   * `resumeExtractMessageId`: карточка извлечения прошлой подписки — реплей
   * ПЕРЕИСПОЛЬЗУЕТ её (сброс + пересборка), а не плодит дубль.
   *
   * Возвращает хвост журнала: он и есть машинный признак, погиб прогон или
   * дожил (см. `@/lib/run-recovery`).
   */
  const consumeRun = useCallback(
    async (o: {
      projectId: string;
      runId: string;
      assistantId: string;
      arch: string;
      signal?: AbortSignal;
      resumeExtractMessageId?: string;
      onEvent?: (kind: "real" | "ping") => void;
      /** Повторная попытка догона: про обрыв уже сказали и уже записали в
       *  журнал клиента. Без этого один обрыв давал бы семь одинаковых
       *  предупреждений и семь записей `chat_stream_drop`. */
      quietDrop?: boolean;
      /** Реплей журнала: сообщение надо очистить, иначе текст задвоится. Но
       *  очищаем ЛЕНИВО — на первом же пришедшем событии. Раньше чистили сразу,
       *  и если реплей не доезжал (обрыв сети), ответ пропадал навсегда:
       *  пустое сообщение ещё и сохранялось в хранилище. */
      resetBeforeReplay?: boolean;
    }) => {
      const { projectId, assistantId, arch } = o;
      // 🔴 КАРТОЧКУ ИЗВЛЕЧЕНИЯ БЕРЁМ СРАЗУ, А НЕ ЖДЁМ `extract_start` (31.08.2026).
      //
      // ЖАЛОБА ВЛАДЕЛЬЦА: «таблица должна появляться живьём по мере готовности,
      // а не путём ручного обновления страницы».
      //
      // ПРИЧИНА. `extract_start` приходит ОДИН РАЗ, в начале прогона. Обрыв SSE
      // посреди извлечения (а он штатный — сторож связи для того и заведён)
      // открывал НОВУЮ подписку, и она догоняла поток с текущего места: второго
      // `extract_start` в нём уже нет. Восстановление `extractId` при этом жило
      // ВНУТРИ ветки `extract_start`, то есть не срабатывало никогда, а строки
      // дальше отбрасывались молча:
      //
      //     } else if (ev.type === "metric_row") {
      //       const emId = extractId;
      //       if (!emId) continue;     // ← сюда и уходили все 122 строки
      //
      // Таблица появлялась только по F5, когда состояние перечитывалось с
      // бэкенда целиком. Ставим id ДО первого события: он известен из прошлой
      // подписки и ждать его неоткуда.
      let extractId: string | null = o.resumeExtractMessageId ?? null;
      let resumePending = Boolean(o.resumeExtractMessageId);
      // Терминальным считается только «done» (или ошибка сервера). Обрыв
      // подписки терминальным НЕ является: ход продолжается на сервере.
      let terminal = false;
      let needsReset = Boolean(o.resetBeforeReplay);
      // Пробник смотрит, ЧЕМ кончился журнал: с `session_lost` (прогон убит
      // перезапуском) или без него (прогон дожил). Отказ fetch этого не знает —
      // 05.08 оба случая дали одинаковое «Load failed».
      const probe = createRunProbe();
      try {
        for await (const ev of probe.wrap(chatRunStream(o.runId, o.signal))) {
          // Кадр-биения: связь жива, работы нет. Дальше он не идёт НИ ШАГУ —
          // иначе попал бы в общую ветку → patch() → штамп updated_at → сброс
          // хранилища и PUT /dialogs целиком каждые 15 секунд, ровно в ту
          // паузу, которую биение и лечит.
          if (ev.type === "ping") {
            o.onEvent?.("ping");
            continue;
          }
          o.onEvent?.("real");
          if (needsReset) {
            needsReset = false;
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === assistantId && m.role === "assistant"
                  // Сброс — ОДНОЙ функцией с реестром живых полей: инлайновый
                  // список здесь забыл `beats`, и реплей после обрыва задваивал
                  // шаги в ленте хода (см. resetForReplay).
                  ? resetForReplay(m)
                  : m,
              ),
            }));
          }
          if (ev.type === "session_lost") {
            // Прогон умер вместе с перезапуском бэкенда. Приговор забрал
            // пробник — он уедет в поле `interrupted` вместе с кнопкой.
            // В content его писать НЕЛЬЗЯ: гейт отправки считает непустой
            // content законченным ходом (см. `send`).
            continue;
          }

          if (ev.type === "extract_start") {
            // Всё, что модель написала ДО старта извлечения, — хроника
            // («создам файл и запущу расчёт»), не ответ. Демоция та же, что у
            // tool_call в редьюсере, — иначе проза остаётся в ответе: на
            // прокси-пути text_segment_end не приходит (прогон 9d2dad93).
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === assistantId && m.role === "assistant" ? demoteProseToNote(m) : m,
              ),
            }));
            const docIds = projectRef.current?.doc_ids ?? [];
            const docId = ev.doc_id || docIds[0] || "";
            const label =
              ev.company_name ||
              (optsRef.current.docs ?? []).find((d) => d.doc_id === docId)?.filename ||
              docId ||
              "извлечение";
            if (resumePending && o.resumeExtractMessageId) {
              // Реплей: пересобираем существующую карточку с нуля.
              resumePending = false;
              extractId = o.resumeExtractMessageId;
              const rid = extractId;
              patch(projectId, (p) => ({
                ...p,
                messages: p.messages.map((m) =>
                  m.id === rid && m.role === "extraction"
                    ? { ...m, status: "running" as const, metrics: [], event_count: 0 }
                    : m,
                ),
              }));
              continue;
            }
            const emId = uid();
            extractId = emId;
            // Карточку собирает ОБЩАЯ фабрика: ту же шапку разворачивает
            // читатель сохранённого прогона (#276), и двух редакций одной
            // карточки быть не должно.
            const em: ExtractionMessage = extractionFromStart(ev, {
              id: emId,
              doc_id: docId,
              doc_label: label,
              at: Date.now(),
              arch,
            });
            patch(projectId, (p) => ({
              ...p,
              messages: [...p.messages, em],
              activeChatRun:
                p.activeChatRun && p.activeChatRun.run_id === o.runId
                  ? { ...p.activeChatRun, extract_message_id: emId }
                  : p.activeChatRun,
            }));
            if (docId) optsRef.current.onFocusDoc?.(docId);
          } else if (ev.type === "metric_row") {
            const emId = extractId;
            if (!emId) continue;
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === emId && m.role === "extraction"
                  ? applyExtractEvent(m, { type: "metric", ...ev.metric } as ExtractStreamEvent)
                  : m,
              ),
            }));
          } else if (ev.type === "extract_done" || ev.type === "extract_error") {
            const emId = extractId;
            const isErr = ev.type === "extract_error";
            if (emId) {
              patch(projectId, (p) => ({
                ...p,
                messages: p.messages.map((m) => {
                  if (m.id !== emId || m.role !== "extraction") return m;
                  if (isErr) {
                    return {
                      ...m,
                      status: "error" as const,
                      error: ev.message || "ошибка извлечения",
                      current: undefined,
                      elapsed_ms: Date.now() - (m.started_at ?? Date.now()),
                    };
                  }
                  const applied = applyExtractEvent(m, {
                    type: "done",
                    role: ev.role,
                    role_label: ev.role_label,
                    role_format: ev.role_format,
                    // #141: событие тут пересобирается по полям — забудешь
                    // перечислить, и белый список роли снова потеряется.
                    table_output_enums: ev.table_output_enums,
                    role_not_applied: ev.role_not_applied,
                    role_notice: ev.role_notice,
                    formatted: ev.formatted ?? undefined,
                    periods: ev.periods,
                  } as ExtractStreamEvent);
                  return { ...applied, elapsed_ms: Date.now() - (m.started_at ?? Date.now()) };
                }),
              }));
            }
            extractId = null;
          } else if (ev.type === "bg_runs") {
            for (const r of ev.runs) followBgRun(projectId, r.run_id);
          } else if (ev.type === "session_id") {
            // Без этого следующий ход теряет серверную сессию.
            patch(projectId, (p) => ({ ...p, session_id: ev.session_id }));
          } else if (ev.type === "usage") {
            const u = ev.turn;
            patch(projectId, (p) => ({
              ...p,
              usage_total: ev.project_total,
              messages: p.messages.map((m) =>
                m.id === assistantId && m.role === "assistant"
                  ? {
                      ...m,
                      usage: {
                        model: u.model,
                        tokens_in: u.tokens_in,
                        tokens_out: u.tokens_out,
                        tokens_cached: u.tokens_cached,
                        turns: u.turns,
                        cost_usd: u.cost_usd,
                      },
                    }
                  : m,
              ),
            }));
          } else if (ev.type === "done") {
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === assistantId && m.role === "assistant" ? applyChatEvent(m, ev) : m,
              ),
            }));
            terminal = true;
            break;
          } else {
            if (ev.type === "error") {
              terminal = true;
              notify("error", `Ход прерван: ${ev.error}`);
            }
            patch(projectId, (p) => ({
              ...p,
              messages: p.messages.map((m) =>
                m.id === assistantId && m.role === "assistant" ? applyChatEvent(m, ev) : m,
              ),
            }));
          }
        }
        const tail = probe.tail();
        // Журнал мог кончиться на `session_lost` и без `done` — ход всё равно
        // закрыт, маркер снимаем.
        if (tail.kind !== "open") terminal = true;
        return tail;
      } catch (e) {
        // Стрим оборвался. Маркер НЕ снимаем — ход живёт на сервере, и реаттач
        // должен его догнать. Раньше маркер стирался в finally, и ответ терялся
        // навсегда, хотя сервер его дописывал (так пропал разбор Мегафона).
        //
        // НО отмена подписки бывает НАМЕРЕННОЙ: ушли в другой чат, свернули
        // панель, нажали «Стоп». Это штатный ход событий, а не авария —
        // пугать пользователя и звать это сбоем в журнале нельзя.
        const err = e as Error;
        const deliberate = o.signal?.aborted || err?.name === "AbortError";
        if (!deliberate && !o.quietDrop) {
          notify("warn", "Связь с прогоном оборвалась — догоняю ответ…");
          reportClientEvent({
            kind: "chat_stream_drop",
            run_id: o.runId,
            project_id: o.projectId,
            detail: err?.message || String(e),
          });
        }
        throw e;
      } finally {
        if (terminal) {
          patch(projectId, (p) =>
            p.activeChatRun && p.activeChatRun.run_id === o.runId
              ? { ...p, activeChatRun: undefined }
              : p,
          );
        }
        // 🔴 ДОГОН ЗАПУСКАЕМ И НА ОБРЫВЕ, А НЕ ТОЛЬКО НА ЧИСТОМ КОНЦЕ
        // (23.08.2026, второй заход по тому же дефекту).
        //
        // Первая редакция стояла внутри `if (terminal)`. Живой прогон
        // 49b398dc: модель ДОСЧИТАЛА и написала ответ на 1716 знаков в том же
        // ходе — но связь к этому моменту оборвалась («Связь с сервером
        // пропала»), терминала клиент не получил, догон не запустился, и
        // владелец увидел ответ только после ручного обновления страницы.
        // Обрыв — ровно тот случай, ради которого догон и заводился.
        //
        // Намеренную отмену («Стоп», уход со страницы) исключаем: там человек
        // сам отказался от ответа, и дописывать его в ленту нельзя.
        if (!o.signal?.aborted) chaseLateAnswer(projectId);
      }
    },
    [patch, followBgRun, chaseLateAnswer],
  );

  // ── Сторож живого потока: ОДИН на оба пути подписки ─────────────────────
  //
  // ДВА таймера, а не один, и это не усложнение ради усложнения. Одноуровневый
  // сторож после появления кадра-биения стал бы бесполезен: он взводился бы на
  // ЛЮБОМ пришедшем событии, а биение приходит каждые 15 с всегда — значит
  // «нет данных 5 минут» не наступило бы уже никогда, и зависший ход показывал
  // бы вечное «Думаю…». Биение отвечает за СВЯЗЬ, судить по нему о РАБОТЕ
  // нельзя. Поэтому: биение продлевает сторож связи, настоящие события —
  // сторож работы.
  //
  // LINK_MS = 60 с — четыре пропущенных биения подряд. Меньше нельзя (одиночная
  // потеря кадра не должна рвать ход), больше незачем. ⚠️ Короткий срок
  // включается ТОЛЬКО после первого увиденного биения: пока бэкенд старый и
  // биения не шлёт, действуют прежние 5 минут. Иначе в окне выкатки «новый
  // фронт + старый бэк» ход рвался бы на первой же штатной паузе.
  //
  // STALLED_MS = 15 мин — заведомо больше самой длинной ЗАКОННОЙ тишины (замер
  // 02.08: сбор показателей 193 с, написание анализа 176 с; худший наблюдённый
  // ход — 14,4 мин целиком).
  //
  // 🔴 ПОЧЕМУ ФАБРИКА, А НЕ ДВА ЭКЗЕМПЛЯРА (24.08.2026). Сторож жил внутри
  // `send` и только там. Переподписка (`consumeRun` в реаттач-эффекте) шла БЕЗ
  // `onEvent` — то есть без присмотра вообще. Пока обрыв лечила перезагрузка,
  // это не было видно: после F5 поток был свежий. Как только догон стал
  // автоматическим, вторая тишина осталась бы незамеченной навсегда, и
  // «переподключаюсь» снова стало бы обещанием без исполнения — на уровень
  // ниже. Один сторож на оба пути закрывает это по построению.
  const создатьСторож = useCallback(
    (ac: AbortController, projectId: string, runId: { current: string }) => {
      let sawPing = false;
      let linkTimer: ReturnType<typeof setTimeout> | null = null;
      let stalledTimer: ReturnType<typeof setTimeout> | null = null;
      // 🔴 СТОРОЖ ОБЯЗАН ДЕЛАТЬ ТО, ЧТО ОБЕЩАЕТ.
      //
      // Здесь стояло «переподключаюсь» и `ac.abort()` — и на этом всё. Обрыв
      // ловил `catch` по `AbortError` и глотал молча; эффект переподписки
      // висит на `[project.id, activeRunId, dropTick]`, а из них не менялась НИ
      // ОДНА. `dropTick` двигала только кнопка «Догнать ответ».
      //
      // То есть переподключения не было вовсе, а фраза о нём была. Ответ
      // возвращала ТОЛЬКО перезагрузка страницы: она обнуляет `followedRuns`, и
      // эффект подхватывает прогон заново. Владелец так и делал руками — трижды
      // за два дня, каждый раз считая это отдельным сбоем.
      //
      // Теперь сторож двигает `dropTick` сам. Со «Стоп» не спорит: тот снимает
      // `activeChatRun`, и эффект выходит на первой же проверке.
      const armLink = () => {
        if (linkTimer) clearTimeout(linkTimer);
        linkTimer = setTimeout(() => {
          const { догоняем, текст } = вердиктОбрыва(sawPing, autoRetries.current);
          notify("error", текст);
          ac.abort();
          if (!догоняем) return;
          autoRetries.current += 1;
          reportClientEvent({
            kind: "link_lost_reconnecting",
            project_id: projectId,
            run_id: runId.current,
            detail: `попытка ${autoRetries.current}/${AUTO_RETRY_MAX}; тишина ${LINK_MS / 1000} с`,
          });
          // Догон запускает ЭФФЕКТ, а не этот код: у него уже есть реплей с
          // начала, backoff и разбор хвоста журнала (жив / погиб / не дозвон).
          setDropTick((n) => n + 1);
        }, sawPing ? LINK_MS : IDLE_MS);
      };
      const armStalled = () => {
        if (stalledTimer) clearTimeout(stalledTimer);
        stalledTimer = setTimeout(() => {
          notify("error", "Прогон молчит 15 минут — похоже, он завис. Прерываю.");
          ac.abort();
        }, STALLED_MS);
      };
      const armWatchdog = (kind: "real" | "ping" = "real") => {
        if (kind === "ping") sawPing = true;
        // Событие ЛЮБОГО рода доказывает, что связь жива, — счётчик догонов
        // считает ПОДРЯД ИДУЩИЕ неудачи, а не все за время хода. Иначе долгий
        // прогон с редкими обрывами упирался бы в потолок из-за давних.
        autoRetries.current = 0;
        armLink();
        if (kind === "real") armStalled();
      };
      const disarm = () => {
        if (linkTimer) clearTimeout(linkTimer);
        if (stalledTimer) clearTimeout(stalledTimer);
      };
      return { armWatchdog, disarm };
    },
    [],
  );

  const send = useCallback(
    async (message: string, sendOpts?: SendOpts) => {
      const p = projectRef.current;
      if (!p) return;
      const text = message.trim();
      if (!text) return;
      if (abortRef.current) return; // гейт на ЭТОТ чат: один ход за раз

      // Предыдущий ход ещё не закрыт — второй запуск ОСИРОТИЛ БЫ его: маркер
      // `activeChatRun` один на чат, новый ход затирал бы старый, и ответ
      // (вместе с извлечением и анализом) уже некому было бы дорисовать.
      // Реальный случай: подписка оборвалась, «Стоп» погас, пользователь
      // задал следующий вопрос — и прошлые результаты пропали.
      const pending = p.activeChatRun;
      if (pending) {
        const owner = p.messages.find((m) => m.id === pending.assistant_id);
        const empty = owner?.role === "assistant" && !owner.content;
        if (empty) {
          notify(
            "warn",
            "Предыдущий ход ещё идёт — дождитесь ответа или нажмите «Стоп».",
          );
          return;
        }
      }

      const projectId = p.id;
      const prefs = loadPrefs();
      // 🔴 ПУТЬ ХОДА ОДИН (решение Nate 18.08, снос тумблера FINENGINE|GATEWAY).
      //
      // Выбор пути был нужен на время миграции, когда встать на петлю гейтвея
      // было иначе нельзя. Монолиты снесены 15.08, путь остался один, и тумблер
      // потерял предмет.
      //
      // ⚠️ ПОЧЕМУ КОНСТАНТА, А НЕ ПРОСТО УБРАННАЯ КНОПКА. Бэкенд выбирает петлю
      // ПО ЭТОМУ ЗНАЧЕНИЮ (`use_gateway_loop`, api/main.py), а переменная
      // окружения FINENGINE_CHAT_GATEWAY не задана НИ на dev, НИ на проде —
      // проверено 18.08. Убери мы только кнопку, у всех, кто когда-то нажал
      // «Finengine», в localStorage и в проектах остался бы `finengine_baseline`
      // — и они навсегда уехали бы мёртвым путём без единого способа вернуться.
      const arch = "aifa_gateway";
      const assistantId = uid();
      const isFirst = p.messages.length === 0;

      // Свежие doc_ids читаем из ХРАНИЛИЩА, а не из своей копии проекта:
      // загрузка документа пишет их туда напрямую, и без этого ход уходил бы
      // без только что приложенного файла — агент отвечал «документы не
      // загружены», хотя PDF уже лежал в чате.
      const storedDocIds = loadProjects(username).find((x) => x.id === projectId)?.doc_ids ?? [];
      const docIdsForTurn = Array.from(
        new Set([...(p.doc_ids ?? []), ...storedDocIds, ...(sendOpts?.extraDocIds ?? [])]),
      );
      if (docIdsForTurn.length !== (p.doc_ids ?? []).length) {
        patch(projectId, (pp) => ({
          ...pp,
          doc_ids: Array.from(new Set([...pp.doc_ids, ...docIdsForTurn])),
        }));
      }
      // Пользователь отправил документ агенту — теперь распознавание оправдано.
      // До этого файл лежал загруженным, но неразобранным (defer_parse).
      const docsForTurn = (opts.docs ?? []).filter((d) => docIdsForTurn.includes(d.doc_id));

      // Документы, приложенные к ЭТОМУ ходу: те, которых ещё НЕ НАЗЫВАЛО НИ ОДНО
      // СООБЩЕНИЕ. Их видно в пузыре — иначе непонятно, ушёл файл или нет.
      //
      // 🔴 РАНЬШЕ ЗДЕСЬ СТОЯЛО «которых не было в `doc_ids` проекта», и это
      // ломалось ровно тем, что пользователь ушёл на другую вкладку и вернулся
      // (жалоба владельца 20.08, Северсталь). Разница по шагам:
      //   приложил → `doc_ids` проекта пополняются сразу;
      //   ушёл и вернулся → копия проекта перечитана из хранилища, и документ
      //   в ней УЖЕ ЕСТЬ;
      //   отправил → `priorDocs` его содержит, `attachedNow` пуст, у сообщения
      //   нет `files`.
      // Дальше беда идёт по кругу: чип в поле ввода строится как «есть в
      // `doc_ids`, но не назван ни одним сообщением» (`chat-pane.attachedUnsent`),
      // и раз сообщение файл не назвало, чип не гаснет НИКОГДА. Человек видит
      // «файл завис в исходящих» и не может понять, ушёл он или нет.
      // Документ при этом уезжал с ходом исправно: он в `docIdsForTurn`.
      //
      // Критерий «названо сообщением» — тот же, по которому чип гаснет, поэтому
      // два места больше не могут разойтись.
      const priorDocs = new Set(
        p.messages.flatMap((m) =>
          "files" in m ? (m.files ?? []).map((f) => f.file_id) : [],
        ),
      );
      // Список документов экрана мог ещё не обновиться — имя берём из реестра
      // загрузки, иначе только что приложенный файл не показался бы в пузыре.
      const attachedNow = docIdsForTurn
        .filter((id) => !priorDocs.has(id))
        .map((id) => ({
          doc_id: id,
          filename:
            docsForTurn.find((d) => d.doc_id === id)?.filename ?? docNameOf(id) ?? "документ",
        }));
      // Пользователь отправил документ агенту — теперь распознавание оправдано.
      // До этого файл лежал загруженным, но неразобранным (defer_parse).
      // Второй аргумент — ТОЛЬКО что приложенные: раскладку трогает лишь
      // первая отправка документа, дальше обычные сообщения в том же чате
      // не имеют права двигать окна.
      if (docIdsForTurn.length) {
        void opts.onTurnDocs?.(docIdsForTurn, attachedNow.map((d) => d.doc_id));
      }

      const userMsg: TextMessage = {
        id: uid(),
        role: "user",
        content: text,
        created_at: Date.now(),
        files: attachedNow.length
          ? attachedNow.map((d) => ({
              file_id: d.doc_id,
              filename: d.filename,
              kind: "pdf",
            }))
          : undefined,
      };
      const assistantMsg: TextMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        created_at: Date.now(),
        thinking: "Думаю…",
        arch,
        model: p.model || prefs.model,
        mode: "chat",
      };
      patch(projectId, (pp) => ({
        ...pp,
        name: isFirst ? text.slice(0, 60) : pp.name,
        messages: [...pp.messages, userMsg, assistantMsg],
      }));

      // Помечаем чат занятым ДО сетевого вызова — «Стоп» живой с первой миллисекунды.
      const ac = new AbortController();
      abortRef.current = ac;
      setStreaming(true);

      // Новый ход — счётчик догонов с нуля: он про подряд идущие обрывы.
      autoRetries.current = 0;
      // id прогона приходит из `startChatTurn` ПОЗЖЕ, а сторож взводится ДО
      // сетевого вызова (иначе зависший старт никто не поймает). Коробка —
      // чтобы телеметрия обрыва называла прогон, когда он уже есть.
      const runIdBox = { current: "" };
      const { armWatchdog, disarm } = создатьСторож(ac, projectId, runIdBox);
      armWatchdog();


      // Ход уже заведён на сервере? Тогда обрыв потока — НЕ отказ отправки:
      // прогон живёт сам по себе, и его надо догонять, а не объявлять сбоем.
      let startedRunId = "";
      try {
        const { run_id } = await startChatTurn({
          session_id: p.session_id ?? "",
          message: text,
          doc_ids: docIdsForTurn,
          arch,
          // Чат внутри проекта скоупится на ПАПКУ — иначе не увидит её файлы.
          project_id: p.folder_id ?? p.id,
          role: prefs.role,
          pinned_tool: sendOpts?.pinnedTool ?? "",
          pinned_tool_args: sendOpts?.pinnedToolArgs ?? {},
          web_search: prefs.webSearch,
          // Открытый в панели документ — через ref: замыкание send живёт дольше
          // рендера, а активный документ пользователь мог переключить только что.
          active_doc_id:
            optsRef.current.activeDocId && docIdsForTurn.includes(optsRef.current.activeDocId)
              ? optsRef.current.activeDocId
              : "",
        });
        // Гвард ДО патча activeChatRun: патч будит реаттач-эффект, и без записи
        // в followedRuns он подписался бы на тот же ран ВТОРОЙ раз параллельно —
        // каждая дельта рисовалась бы дважды (закрытый прод-баг c9214e3).
        followedRuns.current.set(run_id, ac);
        runIdBox.current = run_id;
        startedRunId = run_id;
        patch(projectId, (pp) => ({
          ...pp,
          activeChatRun: { run_id, assistant_id: assistantId, started_at: Date.now() },
        }));
        try {
          await consumeRun({
            projectId,
            runId: run_id,
            assistantId,
            arch,
            signal: ac.signal,
            onEvent: armWatchdog,
          });
        } finally {
          if (followedRuns.current.get(run_id) === ac) followedRuns.current.delete(run_id);
        }
      } catch (e) {
        const err = e as Error;
        if (err.name === "AbortError") {
          // Намеренная отмена — молча.
        } else if (startedRunId) {
          // Прогон на сервере ЖИВ (или уже дописался) — обрыв потока про его
          // судьбу не говорит НИЧЕГО. Раньше сюда падал сырой текст браузера
          // («Load failed» у Safari) прямо в ответ агента, и это был тупик:
          // одинаковая надпись и для погибшего прогона, и для живого, без
          // единого действия. Теперь показываем состояние и идём догонять —
          // ответ придёт из журнала сам.
          markInterrupted(projectId, assistantId, {
            state: "reconnecting",
            attempt: 1,
            total: RECONNECT_DELAYS_MS.length,
          });
          setDropTick((n) => n + 1);
        } else {
          // Ход даже не завёлся — догонять нечего, это честный отказ отправки.
          notify("error", err.message);
          patch(projectId, (pp) => ({
            ...pp,
            messages: pp.messages.map((m) =>
              m.id === assistantId && m.role === "assistant"
                ? { ...m, thinking: undefined, content: m.content || `⚠️ ${err.message}` }
                : m,
            ),
          }));
        }
      } finally {
        disarm();
        // «Думаю…» — транзиентная подсказка, в сохранённом сообщении ей не место.
        // Заодно ГАСИМ незавершённые шаги плана: ход кончился (успешно, с
        // ошибкой или обрывом), а шаг со статусом running оставался «выполняется»
        // навсегда — остров врал и крутил секундомер после «Load failed».
        patch(projectId, (pp) => ({
          ...pp,
          messages: pp.messages.map((m) => {
            if (m.id !== assistantId || m.role !== "assistant") return m;
            const stale = (m.plans ?? []).some((l) =>
              l.steps.some((s) => s.status === "running"),
            );
            if (!m.thinking && !stale) return m;
            return {
              ...m,
              thinking: undefined,
              plans: stale
                ? (m.plans ?? []).map((l) => ({
                    ...l,
                    steps: l.steps.map((s) =>
                      s.status === "running" ? { ...s, status: "error" as const, note: "ход прерван" } : s,
                    ),
                  }))
                : m.plans,
            };
          }),
        }));
        if (abortRef.current === ac) {
          abortRef.current = null;
          setStreaming(false);
        }
      }
    },
    // `opts` и `username` тут отсутствуют СОЗНАТЕЛЬНО и были такими до меня:
    // send читает их через рефы, а включение пересоздавало бы отправщик на
    // каждый ввод. `создатьСторож` добавлен — он стабилен (deps []).
    [patch, consumeRun, markInterrupted, создатьСторож],
  );

  /** «Догнать ответ» из плашки прерванного хода — та же переподписка вручную. */
  const reconnect = useCallback(() => setDropTick((n) => n + 1), []);

  const stop = useCallback(() => {
    // Ход живёт на СЕРВЕРЕ: закрыть подписку мало — без cancel ответ допишется
    // и всплывёт в истории при следующей загрузке.
    const p = projectRef.current;
    const run = p?.activeChatRun;
    if (run?.run_id) void cancelChatTurn(run.run_id);
    abortRef.current?.abort();
    // Снимаем маркер хода САМИ. Обрыв по abort не даёт терминального события,
    // поэтому `activeChatRun` оставался висеть, и дальше ломалось ДВА раза:
    // (1) гейт отправки считал ход живым — «Предыдущий ход ещё идёт», хотя
    // пользователь его только что остановил; (2) реаттач-эффект видел маркер
    // без записи в followedRuns и заново подписывался на отменённый прогон.
    // Пустой ответ помечаем явно — иначе в истории остаётся немой пузырь.
    if (p && run) {
      patch(p.id, (pp) =>
        pp.activeChatRun?.run_id === run.run_id
          ? {
              ...pp,
              activeChatRun: undefined,
              messages: pp.messages.map((m) => {
                if (m.id !== run.assistant_id || m.role !== "assistant") return m;
                // Плашка прерванного хода после «Стоп» врала бы: предлагать
                // «догнать ответ» у отменённого прогона нечестно.
                if (m.content) return { ...m, interrupted: undefined };
                return {
                  ...m,
                  interrupted: undefined,
                  thinking: undefined,
                  content: "⏹ Остановлено — отправьте запрос ещё раз.",
                };
              }),
            }
          : pp,
      );
    }
  }, [patch]);

  // Реаттач после перезагрузки: прогон жил на сервере, подписка реплеит его
  // С НАЧАЛА — сообщение ассистента обязательно сбрасываем, иначе текст удвоится.
  const activeRunId = project?.activeChatRun?.run_id;
  useEffect(() => {
    const p = projectRef.current;
    const run = p?.activeChatRun;
    if (!p || !run) return;
    // Сторож ставится ПРОТИВ ДВОЙНОЙ подписки (закрытый баг c9214e3: каждая
    // дельта рисовалась дважды). Но оборванная подписка — не подписка: её
    // контроллер уже прерван, событий из неё не придёт никогда. Проверять
    // одно лишь НАЛИЧИЕ записи значило запереть догон навсегда — карта
    // чистится в `finally` потребителя, а он завершается АСИНХРОННО, уже
    // после того как эффект прочитал её синхронно.
    if (!можноПодписаться(followedRuns.current.get(run.run_id))) return;

    const projectId = p.id;
    const ac = new AbortController();
    followedRuns.current.set(run.run_id, ac);
    // Сообщение НЕ трогаем: очистку сделает consumeRun на первом событии
    // реплея. Иначе неудачный догон оставлял бы пустоту вместо ответа.
    patch(projectId, (pp) => ({
      ...pp,
      messages: pp.messages.map((m) =>
        m.id === run.assistant_id && m.role === "assistant"
          ? { ...m, thinking: m.content ? m.thinking : "Догоняю ответ…" }
          : m,
      ),
    }));

    abortRef.current = ac;
    setStreaming(true);
    // Догон идёт ПОД ТЕМ ЖЕ сторожем, что и первая подписка: без него вторая
    // тишина не будет замечена никем, и «переподключаюсь» снова окажется
    // обещанием без исполнения — просто уровнем ниже.
    const { armWatchdog: сторож, disarm: снятьСторож } = создатьСторож(ac, projectId, {
      current: run.run_id,
    });
    сторож();
    // Обрыв сети во время хода — не приговор: журнал на сервере полон, и
    // подписаться можно снова. Пробуем несколько раз с нарастающей паузой,
    // иначе пользователю пришлось бы уходить из чата и возвращаться руками.
    // Приговор выносит ХВОСТ ЖУРНАЛА, а не текст сетевой ошибки: в нём и
    // видно, погиб прогон при перезапуске или дожил до конца.
    const withRetries = async () => {
      const verdict = await reconnectRun({
        attempt: (n) =>
          consumeRun({
            projectId,
            runId: run.run_id,
            assistantId: run.assistant_id,
            arch: p.arch ?? "",
            signal: ac.signal,
            resumeExtractMessageId: run.extract_message_id,
            resetBeforeReplay: true,
            quietDrop: n > 1,
            onEvent: сторож,
          }),
        // Первая попытка идёт без объявления: обычный догон после F5 успевает
        // раньше, чем пользователь успел бы прочитать «переподключаюсь».
        onAttempt: (n, total) => {
          if (n > 1) markInterrupted(projectId, run.assistant_id, {
            state: "reconnecting",
            attempt: n,
            total,
          });
        },
        aborted: () => ac.signal.aborted,
      });
      if (verdict.kind === "aborted") return;
      if (verdict.kind === "lost") {
        // Прогона больше нет — повторять можно и нужно.
        markInterrupted(projectId, run.assistant_id, {
          state: "lost",
          notice: verdict.notice,
        });
        return;
      }
      if (verdict.kind === "failed") {
        // Журнал кончился на `error` — прогон упал или его отменили. Работы за
        // ним нет, догонять нечего. Без этой ветки такой хвост читался как
        // «поток ещё открыт», мы семь раз перечитывали журнал мёртвого прогона
        // и в конце обещали сохранённый результат, которого не существует.
        markInterrupted(projectId, run.assistant_id, {
          state: "failed",
          notice: verdict.notice,
        });
        return;
      }
      if (verdict.kind === "unreachable") {
        // НЕ «прогон погиб»: мы просто не дозвонились. Работа на сервере
        // продолжается, и повторный запуск осиротил бы её вместе с готовым
        // результатом — поэтому предлагаем догнать, а не отправить заново.
        notify("error", "Не удалось догнать ответ: связь не восстановилась.");
        markInterrupted(projectId, run.assistant_id, { state: "unreachable" });
        return;
      }
      // Догнали: результат нарисован журналом, объясняться больше не о чем.
      markInterrupted(projectId, run.assistant_id, undefined);
    };

    void withRetries()
      .catch(() => undefined)
      .finally(() => {
        снятьСторож();
        if (followedRuns.current.get(run.run_id) === ac) followedRuns.current.delete(run.run_id);
        patch(projectId, (pp) => ({
          ...pp,
          messages: pp.messages.map((m) =>
            m.id === run.assistant_id && m.role === "assistant" && m.thinking
              ? { ...m, thinking: undefined }
              : m,
          ),
        }));
        if (abortRef.current === ac) {
          abortRef.current = null;
          setStreaming(false);
        }
      });
    // dropTick — обрыв живого потока и кнопка «Догнать ответ»: без него эффект
    // не перезапускался, потому что остальные зависимости при обрыве те же.
  }, [project?.id, activeRunId, dropTick, patch, consumeRun, markInterrupted, создатьСторож]);

  // Ушли с чата (или размонтировали панель) — рвём подписку и чистим гвард
  // целиком: подписки больше нет, и при возврате реаттач обязан суметь
  // подписаться заново. Ход при этом продолжается на сервере, а `activeChatRun`
  // уже сохранён, так что ответ дорисуется с начала.
  useEffect(() => {
    const runs = followedRuns.current;
    return () => {
      for (const ac of runs.values()) ac.abort();
      runs.clear();
      abortRef.current?.abort();
      abortRef.current = null;
      setStreaming(false);
    };
  }, [project?.id]);

  return { project, streaming, send, stop, reconnect };
}
