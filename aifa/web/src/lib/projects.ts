"use client";

import type {
  AnalysisCard,
  ExtractStreamEvent,
  FormattedTable,
  MetricEvent,
  PlanGraphContract,
  ScenarioRunEvent,
  TypedResult,
  TypedRunError,
} from "@/lib/api";
import { applyAuthorAttempt, applyDynamicMove, applyDynamicPlan, type DynamicTrace } from "@/lib/dynamic-run-trace";
import {
  addExecutionGraph,
  applyExecutionNodeRun,
  closeAllExecutionGraphs,
  closeExecutionMove,
  executionGraphsOf,
  latestExecutionGraph,
  type ExecutionGraph,
  type GraphTerminalStatus,
} from "@/lib/execution-graphs";
import { nodeStatusLabel, nodeStepStatus, type StoredNodeRun } from "@/lib/plan-graph-progress";
import type { InterruptedState } from "@/lib/run-recovery";
import { notify } from "@/lib/notices";
import { toolLabel } from "@/lib/tool-labels";

export type AttachedFile = {
  doc_id: string;
  filename: string;
};

export function typedResultId(result: TypedResult): string {
  if (result.output_type === "FileArtifact") return result.artifact_id;
  if (result.output_type === "ExtractedMetric" || result.output_type === "CalculatedMetric") {
    return result.metric_id;
  }
  if (result.output_type === "MonitorResult") return result.result_id;
  return result.output_id;
}

export type TextMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  created_at: number;
  /** Files the user uploaded with this turn — rendered as chips inside
   * the user bubble. Empty for assistant messages. */
  attachments?: AttachedFile[];
  /** For assistant messages: which arch / model produced the answer.
   * Stamped at request time and rendered as a small badge under the
   * bubble so the user can tell which agent each reply came from. */
  arch?: string;
  model?: string;
  /** What action produced this assistant reply. Currently always "chat" —
   * a regular composer send; the pinned «Быстрый/Полный анализ» buttons also
   * stream as an ordinary chat turn. Used by the chat header badge. */
  mode?: "chat";
  /** Transient "thinking" indicator shown while streaming — replaced
   * each time the backend emits a new phase ("📄 Загружаю…" → "🔎 Парсю
   * PDF…" → "Думаю…"). Cleared once the real answer text starts arriving
   * so it doesn't pollute the saved message. */
  thinking?: string;
  /** Порция текста закончилась (модель ушла в инструмент): следующая дельта
   *  сперва откладывает написанное заметкой в ленту хода. Транзиентное поле
   *  живого стрима — в сохранённое сообщение не попадает по смыслу. */
  segBreak?: boolean;
  /** Tool calls made by the agent during this turn. Rendered as a
   * compact strip of chips above the message body — keeps the text
   * itself clean of "🔧 Использует tavily_search…" noise. Aggregated by
   * tool name so 4× tavily_search shows as one chip with ×4 badge. */
  toolCalls?: ToolCallSummary[];
  /** Token usage + USD cost for this turn. Stamped after stream done so
   * the user sees per-message burn next to the timestamp. */
  usage?: TurnUsageInfo;
  /** A clarifying question the agent asked (ask_user) — rendered as option
   * cards under the message; picking one sends it as the next turn. */
  question?: { prompt: string; options: string[] };
  /** Files the agent produced this turn (e.g. the extraction Excel) —
   * rendered as clickable download links under the message body. */
  files?: { file_id: string; filename: string; kind?: string }[];
  /** A rendered financial-analysis card (SSE `analysis_card` event). When set,
   * the assistant bubble renders the rich card instead of the markdown body;
   * `content` still holds the markdown as a fallback. */
  analysisCard?: AnalysisCard;
  /** Versioned machine results. Multiple entries are valid (for example a
   * typed source result followed by its FileArtifact). */
  typedResults?: TypedResult[];
  /** Real executor state keyed by immutable NodeKey, not a model-written plan. */
  nodeRuns?: Record<string, StoredNodeRun>;
  planGraphHash?: string;
  /** Построенная топология (Срез 29, Этап 7) — один раз за исполнение,
   * статусы узлов приходят отдельно через nodeRuns по тому же node_key. */
  planGraph?: PlanGraphContract;
  /** Графы всех ходов. Старые сообщения хранят один planGraph и общий nodeRuns — их читает адаптер
   * `executionGraphsOf`; новые события пишут только сюда (зеркало удваивало размер сообщения). */
  planGraphs?: ExecutionGraph[];
  /** Лента гибридного планировщика: ходы planner и попытки автора (вкладка рядом с графом). */
  dynamicTrace?: DynamicTrace;
  typedErrors?: TypedRunError[];
  /** Task checklist for this turn (the «Прогресс» island): declared by a
   * long tool via a `plan` event, steps updated via `plan_step`. Persisted
   * so finished turns keep their completed checklist. */
  plan?: PlanStep[];
  /** Планы ПО ИСТОЧНИКАМ: событие плана несёт имя тула, и в одном ходе их
   * может быть несколько (анализ инлайн + извлечение). Раньше жил один
   * msg.plan — второй источник затирал первый, и остров показывал план
   * куска работы вместо плана всей задачи. */
  plans?: { name: string; steps: PlanStep[] }[];
  /** Ход оборвался НЕ по вине агента — состояние, а не строка в `content`.
   *  Строкой писать нельзя: гейт отправки считает непустой content
   *  законченным ходом и разрешал повторный запуск, который затирал маркер
   *  живого прогона вместе с готовым результатом (случай 05.08). Плюс к
   *  состоянию цепляется действие — «отправить заново» или «догнать ответ»;
   *  они противоположны, и путать их нельзя. */
  interrupted?: InterruptedState;
  /** Хроника хода: заметки агента и вызовы инструментов В ПОРЯДКЕ появления.
   * `toolCalls` для этого не годится — там СВЁРНУТЫЙ набор инструментов со
   * счётчиками, из него не восстановить, что за чем шло и что агент сказал
   * между шагами. */
  beats?: Beat[];
};

/** Один такт хода: либо реплика агента, либо взятый инструмент. */
export type Beat =
  | { kind: "note"; text: string }
  | {
      kind: "tool";
      name: string;
      status: "running" | "done" | "error";
      /** Причина отказа из конверта тула (Ш3). Красный чип без причины —
       *  тот же молчаливый отказ, только покрашенный. */
      reason?: string;
    };

export type PlanStep = {
  title: string;
  /** `empty` — шаг отработал без результата (нейтрально, не ошибка). */
  status: "pending" | "running" | "done" | "error" | "empty";
  /** Small live detail under the running step (e.g. "извлечено метрик: 40"). */
  note?: string;
  /** Когда шаг ПОШЁЛ и когда закончился — метки ставит клиент на переходе
   *  статуса (#120: «сколько времени было потрачено»).
   *
   *  ⚠️ Часы БРАУЗЕРНЫЕ, и это осознанно: событие приходит без времени, а
   *  выдумывать серверное — врать. Показываем длительность НАБЛЮДЕНИЯ шага
   *  клиентом; на живом ходе это и есть то, что видит пользователь. */
  startedAt?: number;
  endedAt?: number;
};

export type ToolCallSummary = {
  name: string;
  count: number;
  status: "running" | "done" | "error";
  /** Last seen args summary for this tool — e.g. the search query or
   * URL. Rendered next to the tool name in the chip so the user sees
   * what's being looked up, not just that *something* is. */
  lastArgs?: string;
};

export type TurnUsageInfo = {
  model: string;
  tokens_in: number;
  tokens_out: number;
  tokens_cached: number;
  turns: number;
  cost_usd: number;
};

export type ExtractionMessage = {
  id: string;
  role: "extraction";
  doc_id: string;
  doc_label: string;
  company_type: string;
  created_at: number;
  /** Фоновый воркер (#84): run_id серверного прогона. Сохраняется в проекте,
   * чтобы после перезагрузки страницы переподписаться на
   * GET /extract/runs/{run_id}/stream и дорисовать карточку (реплей+хвост). */
  run_id?: string;
  status: "running" | "done" | "error" | "aborted";
  /** Architecture used for this run, e.g. "finengine_baseline", "aifa_baseline".
   * Stamped at request time from prefs so re-extracting under a different agent
   * preserves the previous run instead of overwriting it. */
  arch?: string;
  /** Model id used for this run, e.g. "qwen/qwen3.6-plus". */
  model?: string;
  /** The analyst role this extraction was originally run under (from the
   * backend's start/done events). */
  extraction_role?: string;
  role_label?: string;
  role_format?: string;
  /** Точечный заказ (решение владельца 31.08.2026): пользователь назвал
   * показатели поимённо. Такая таблица живёт в ЧАТЕ; рабочая область её
   * live-стримом не рисует и обновляется сохранённым итогом. Признак ставит
   * скрипт навыка из сужения состава — слов-детектов нет. */
  narrow_order?: boolean;
  /** Current table layout role. Kept separate from extraction_role so a
   * general extraction can be re-rendered as a specialised role and back
   * without losing the original full metric cache. */
  table_role?: string;
  table_role_label?: string;
  table_role_format?: string;
  table_output_enums?: string[];
  /** Роль выбрана, но раскладки под эту отчётность нет — извлечён полный набор
   * ячейки (#141). Показываем это в карточке: молчаливый полный список
   * пользователь читает как «роль не работает». */
  role_not_applied?: boolean;
  role_notice?: string;
  table_formatted?: FormattedTable;
  meta?: {
    total_metrics: number;
    total_extractable: number;
    company_type: string;
    unit: string;
    period_current: string;
    period_prev: string;
    periods?: string[];
    company_name?: string;
    tokens_in?: number;
    tokens_out?: number;
    tokens_cached?: number;
    cost_usd?: number;
    role?: string;
    role_label?: string;
    role_format?: string;
  };
  metrics: MetricEvent[];
  /** Role-formatted table emitted with the terminal `done` event. */
  formatted?: FormattedTable;
  current?: { enum: string; name_ru: string };
  /** Live pipeline stage label (parse / OCR / …). Rendered under the progress
   * bar while running. Populated by a `status` event when the backend emits one. */
  stage?: { stage: string; message: string };
  /** Detected report profile (standard / language / company type). Populated by
   * a `profile_detected` event when the backend emits one; used by the table to
   * pick company-type-specific layouts. */
  profile?: {
    profile_id: string;
    standard: "ifrs" | "us_gaap";
    language: "ru" | "en";
    company_type: "non_financial" | "bank";
    confidence_low: boolean;
    reason: string;
  };
  event_count?: number;
  started_at?: number;
  elapsed_ms?: number;
  error?: string;
};

export type ChatMessage = TextMessage | ExtractionMessage;

/** Project template — drives which toolbar actions and artifact renderers
 * the workspace shows. New types are added by extending this union. */
export type ProjectType = "generic" | "ir_calendar" | "ir_daily";

/** Lightweight ref to a saved skill-run output. The full payload lives
 * server-side at data/artifacts/{user}/{project}/{run_id}/result.json
 * and is fetched on demand via getProjectArtifact(). */
export type ArtifactRef = {
  run_id: string;
  kind: string;             // "ir_calendar" | "ir_daily" | …
  created_at: number;       // unix seconds (server time)
  title: string;            // human-readable, shown in the artifact list
  summary?: string;
  counts?: Record<string, number>;
};

export type Project = {
  id: string;
  name: string;
  created_at: number;
  updated_at: number;
  /** Template type — generic chat, IR Calendar workspace, IR Daily News
   * workspace. Drives the toolbar buttons and artifact rendering.
   * Defaults to "generic" for legacy projects created before this field
   * existed. */
  project_type?: ProjectType;
  /** Which project-folder (container) this chat belongs to. Undefined =
   * a standalone chat, shown under «Недавние чаты». A chat in a folder
   * inherits the folder's files/context. See the `Folder` type below. */
  folder_id?: string;
  doc_ids: string[];
  /** The project's single chat thread. One project = one continuous
   * conversation (sessions were removed). */
  messages: ChatMessage[];
  /** Cached list of artifacts for this project. Server is the source of
   * truth; this is just a UI-side mirror so the artifact rail can render
   * without round-trips. Refreshed when a new run finishes. */
  artifacts?: ArtifactRef[];
  /** Running total of LLM spend for this project across all turns.
   * Updated on each chat response (event "usage") and on initial
   * project open (GET /projects/{id}/usage). Rendered under the
   * composer as "Потрачено в проекте: $0.42". */
  usage_total?: {
    tokens_in: number;
    tokens_out: number;
    tokens_cached: number;
    cost_usd: number;
    turns_total: number;
    user_turns: number;
  };
  session_id?: string;
  /** When set, this client project is the chat thread for a server-side
   * scenario — manual scenario runs append streaming assistant turns here,
   * and the thread shows up in Recents. find-or-create keyed by this id. */
  scenario_id?: string;
  /** Per-chat agent arch and model. When set, override the global prefs
   * for THIS project only, so switching modes in one chat doesn't bleed
   * into others. Empty / undefined → fall back to loadPrefs(). */
  arch?: string;
  model?: string;
  /** #84 C3-1: незавершённый ход чата — фронт переподключается на маунте
   * через GET /chat/runs/{run_id}/stream (реплей+хвост), перезагрузка
   * страницы больше не убивает ответ. */
  activeChatRun?: {
    run_id: string;
    /** ID сообщения ассистента этого хода (реплей пересобирает его с нуля). */
    assistant_id: string;
    /** ID карточки извлечения хода, если она была (реплей переиспользует её). */
    extract_message_id?: string;
    started_at: number;
  };
  /** Marks an in-flight metric extraction. Frontend reconnects on
   * mount via `GET /extract/runs/{run_id}/stream`. */
  activeExtractRun?: {
    run_id: string;
    /** ID of the ExtractionMessage that's streaming. */
    extraction_message_id: string;
    doc_id: string;
    started_at: number;
  };
};

/** A project-folder — the user-facing «Проект»: a container that groups
 * several chats (Project entities) under a shared set of files + context,
 * like a Claude Cowork project. A chat points back via `Project.folder_id`.
 * NOTE: the legacy `Project` type above is actually a single CHAT; `Folder`
 * is the real "project". Renaming Project→Chat is deferred to avoid churning
 * the whole app in one go. */
export type Folder = {
  id: string;
  name: string;
  /** Free-text brief shown in the project's «Контекст» block and available
   * to every chat in this folder. */
  instructions?: string;
  /** Documents uploaded to the project (shared by all its chats). Stored
   * server-side under this folder id via /projects/{id}/sources. */
  doc_ids: string[];
  created_at: number;
  updated_at: number;
};

// Storage keys are scoped per logged-in user so different testers on the same
// browser don't see each other's chat history. Pass `username` from AuthContext.
const projectsKey = (username: string) => `finengine:projects:${username}:v1`;
const foldersKey = (username: string) => `finengine:folders:${username}:v1`;
const lastProjectKey = (username: string) => `finengine:last_project:${username}:v1`;

function now(): number {
  return Date.now();
}

export function uid(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

export function loadProjects(username: string): Project[] {
  if (typeof window === "undefined" || !username) return [];
  try {
    const raw = localStorage.getItem(projectsKey(username));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Project[];
    if (!Array.isArray(parsed)) return [];
    return parsed.map(flattenProject).map(sanitizeStaleThinking);
  } catch {
    return [];
  }
}

/** Flatten a project to a single message thread (sessions were removed).
 * The active session's messages already live on `p.messages`; any OTHER
 * sessions kept their messages in `sessions[]` — merge those in (dedup by
 * id, ordered by time) so migrating a legacy multi-session project never
 * loses content. Idempotent: a project with no `sessions` is returned with
 * the legacy session fields stripped. */
export function flattenProject(p: Project): Project {
  const { sessions, active_session_id, ...rest } = p as Project & {
    sessions?: { id: string; messages: ChatMessage[] }[];
    active_session_id?: string;
  };
  if (!sessions || sessions.length === 0) return rest;
  const merged: ChatMessage[] = [...(rest.messages ?? [])];
  const seen = new Set(merged.map((m) => m.id));
  for (const s of sessions) {
    if (s.id === active_session_id) continue; // уже на p.messages
    for (const m of s.messages ?? []) {
      if (!seen.has(m.id)) {
        merged.push(m);
        seen.add(m.id);
      }
    }
  }
  merged.sort((a, b) => a.created_at - b.created_at);
  return { ...rest, messages: merged };
}

/** On page load there is no active stream, so any `thinking` indicator on a
 * message is STALE — left by a turn that was interrupted (e.g. the user
 * reloaded mid-request). Clear it; if an assistant turn was cut off before any
 * content arrived, mark it interrupted so it doesn't show a frozen spinner
 * forever and the user knows to resend.
 *
 * 🔴 «ОТВЕТ ПРЕРВАН» ПРИ ПЕРЕКЛЮЧЕНИИ ДИАЛОГА (#210, разобрано 18.08).
 *
 * Заголовок выше врал по двум причинам. Первая: `loadProjects` зовётся не
 * только при загрузке страницы, а перед КАЖДОЙ записью (см. updateProject) —
 * значит и посреди живого хода. Вторая: уход в другой чат СНИМАЕТ подписку
 * (это штатно, ход продолжается на сервере), сообщение остаётся с `thinking`,
 * и первая же чужая запись сохраняла поверх него «Ответ был прерван». Человек
 * возвращался в чат и видел приговор ходу, который в этот момент работал.
 *
 * Живой ход опознаётся не догадкой, а маркером `activeChatRun`: его ставит
 * рантайм при старте и снимает на терминальном событии.
 *
 * 🔴 ЖИВОЕ СООБЩЕНИЕ НЕ ТРОГАЕМ ВОВСЕ (26.08.2026). Здесь ему гасили спиннер и
 * вешали `unreachable` — «Связь оборвалась… Догнать ответ». Но эта функция
 * зовётся перед КАЖДОЙ записью, то есть и посреди работающего хода: владелец
 * запустил извлечение, на 31-й секунде получил баннер обрыва — и это при
 * живом, стримящем прогоне. В журнале клиента за тот день нет НИ ОДНОГО
 * `link_lost_reconnecting`: сторож связи не срабатывал, приговор вынесла
 * загрузка хранилища.
 *
 * Решать про связь может только тот, кто про неё знает: сторож живого потока
 * (молчание 60 с) и переподписка (`use-chat-run`, ветка `unreachable` — там
 * попытки уже исчерпаны). Функция, читающая localStorage, не знает ничего:
 * `thinking: true` в записи означает лишь «на момент сохранения ход шёл».
 *
 * Что будет при перезагрузке страницы: спиннер останется, и это ВЕРНО — эффект
 * переподписки подхватит прогон по `activeChatRun` и продолжит поток. Не
 * подхватит — приговор вынесет он, с полным знанием.
 */
function sanitizeStaleThinking(p: Project): Project {
  const liveId = p.activeChatRun?.assistant_id;
  let changed = false;
  const messages = p.messages.map((m) => {
    if (m.role !== "assistant") return m;
    const tm = m as TextMessage;
    if (!tm.thinking) return m;
    // Живой ход — не наше дело: ни спиннер, ни приговор. ⚠️ Проверка ДО
    // `changed = true`: сбросить флаг внутри `map` значило бы отменить уборку
    // соседних сообщений, законно вычищенных на этом же проходе.
    if (liveId && tm.id === liveId) return m;
    changed = true;
    if (!tm.content) {
      // 🔴 НЕ ПРИГОВОР ТЕКСТОМ, А ПОМЕТКА «НЕ ДОЗВОНИЛИСЬ» (кейс 9d2dad93,
      // 31.08.2026). Здесь стояло content = «Ответ был прерван (страница
      // перезагружена). Отправьте запрос ещё раз.» — и это врало дважды:
      // прогон в тот момент ЖИЛ на сервере (станция дописала ответ через две
      // минуты), а совет «отправьте ещё раз» осиротил бы его. Вдобавок
      // непустой content ломал догон позднего ответа: сверка хвоста считала
      // приговор ответом. `unreachable` говорит правду («работа, скорее
      // всего, идёт — догоните ответ») и даёт кнопку, которая выясняет
      // судьбу прогона по журналу, а не по догадке localStorage.
      return {
        ...tm,
        thinking: undefined,
        interrupted: { state: "unreachable" as const },
      };
    }
    return { ...tm, thinking: undefined };
  });
  return changed ? { ...p, messages } : p;
}

/**
 * АТОМАРНОЕ изменение одного проекта в хранилище.
 *
 * Раньше каждый писатель (рантайм чата, загрузка документов, страница) делал
 * «прочитал весь список → поменял свою копию → записал весь список». Копии
 * расходились, и последний писатель затирал чужие изменения — так пропадала
 * переписка при переключении вкладки браузера и уходе в другой чат.
 *
 * Здесь список читается НЕПОСРЕДСТВЕННО перед записью, меняется только нужный
 * проект, а сообщения СЛИВАЮТСЯ по id: ни один писатель не может удалить то,
 * чего не видел.
 */
export function updateProject(
  username: string,
  id: string,
  fn: (p: Project) => Project,
): Project | null {
  if (typeof window === "undefined" || !username) return null;
  const list = loadProjects(username);
  const idx = list.findIndex((p) => p.id === id);
  if (idx < 0) return null; // чат удалён — не воскрешаем
  const stored = list[idx];
  const next = fn(stored);
  // Любая правка двигает отметку времени: по ней сравнивается свежесть с
  // серверной копией, и без бампа приложенный документ выглядел «старее»
  // серверного снимка и терялся при следующей подтяжке.
  list[idx] = { ...next, updated_at: Date.now(), messages: mergeMessages(stored.messages, next.messages) };
  saveProjects(username, list);
  return list[idx];
}

/** Объединить ленты сообщений по id, сохранив хронологию. При совпадении id
 *  побеждает ВТОРОЙ аргумент (`live`) — писатель считается свежее хранимого. */
function mergeMessages(stored: ChatMessage[], live: ChatMessage[]): ChatMessage[] {
  if (!stored.length) return live;
  const seen = new Set(live.map((m) => m.id));
  const extra = stored.filter((m) => !seen.has(m.id));
  if (!extra.length) return live;
  return [...live, ...extra].sort((a, b) => a.created_at - b.created_at);
}

/**
 * Слить серверный снимок чатов с локальным. Возвращает список для записи или
 * null, если сервер не принёс ничего нового (тогда писать незачем).
 *
 * ГРАБЛЯ, из-за которой таблица пропадала из рабочей области. Серверная копия
 * СООБЩЕНИЯ заморожена на первой версии: серверный union берёт первое вхождение
 * id, поэтому карточка извлечения там навсегда остаётся такой, какой её впервые
 * запушили — `metrics: []`, `status: running`. При этом сервер всегда выглядит
 * свежее (хвост хода бампает updated_at уже после последней записи клиента).
 * Раньше здесь стояло `{...local, ...server}` — серверные messages накрывали
 * локальные целиком, и в хранилище вместо 75 строк оказывался ноль. Лента чата
 * этого не замечала (у неё своя копия в памяти хука), а зона «Документ» читает
 * хранилище и честно рисовала пустоту.
 *
 * Поэтому: скаляры берём у сервера (имя чата, папка, время), а ПЕРЕПИСКУ
 * сливаем так, чтобы при совпадении id побеждала ЛОКАЛЬНАЯ версия. Сервер
 * может только ДОБАВИТЬ сообщение, которого локально нет, — и не может ничего
 * обеднить. Обратная сторона осознанная: починить повреждённую локальную копию
 * из серверной больше нельзя; восстановление чата целиком (ради чего подтяжка и
 * делалась) не затронуто.
 */
export function mergeServerProjects(
  local: Project[],
  server: Project[],
  serverDeleted?: Record<string, number> | null,
): Project[] | null {
  if (!server.length) return null;
  // ⚠️ МЕТКИ УДАЛЕНИЯ — ЕДИНСТВЕННОЕ, ЧЕМ СЕРВЕР ВПРАВЕ УБАВИТЬ (09.08.2026).
  //
  // Правило «сервер добавляет, но не обедняет» защищает от потери чатов, когда
  // серверный снимок отстал или неполон: ОТСУТСТВИЕ чата на сервере ничего не
  // доказывает. Метка удаления — другое дело: это явное утверждение «его
  // удалили», с временем и сроком жизни (30 дней на сервере).
  //
  // Без этого удаление, сделанное где угодно кроме здешнего интерфейса, до
  // браузера не доходило ВООБЩЕ. Инцидент владельца: localStorage переполнился
  // на 168 чатах, загрузка файлов встала; на сервере 138 старых чатов пометили
  // удалёнными — браузер не заметил и остался при своих 168.
  const tombs = serverDeleted && typeof serverDeleted === "object" ? serverDeleted : null;
  const isDeleted = (id: string) => !!tombs && Object.hasOwn(tombs, id);
  const kept = tombs ? local.filter((p) => !isDeleted(p.id)) : local;
  const removed = local.length - kept.length;
  local = kept;

  const byId = new Map(local.map((p) => [p.id, p]));
  const missing = server.filter((p) => !byId.has(p.id) && !isDeleted(p.id));
  const fresher = server
    .filter((p) => {
      const lp = byId.get(p.id);
      return lp && (p.updated_at ?? 0) > (lp.updated_at ?? 0);
    })
    .map((p) => {
      const lp = byId.get(p.id)!;
      return {
        ...lp,
        ...p,
        messages: mergeMessages(p.messages ?? [], lp.messages ?? []),
      };
    });
  if (!missing.length && !fresher.length && !removed) return null;
  const freshIds = new Set(fresher.map((p) => p.id));
  return [...local.filter((p) => !freshIds.has(p.id)), ...fresher, ...missing];
}

/** Объединить накопительные списки (документы, артефакты), сохранив порядок.
 *  Ни то, ни другое из чата не удаляется — значит запись, не знающая о новом
 *  элементе, не имеет права его стереть. */
function unionBy<T>(stored: T[] | undefined, live: T[] | undefined, key: (x: T) => string): T[] {
  const out: T[] = [];
  const seen = new Set<string>();
  for (const x of [...(stored ?? []), ...(live ?? [])]) {
    const k = key(x);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(x);
  }
  return out;
}

/**
 * ЗАЩИТНЫЙ СЛОЙ ХРАНИЛИЩА. Любая запись списка проходит через слияние с тем,
 * что лежит на диске ПРЯМО СЕЙЧАС:
 *   - сообщения проекта объединяются по id — запись физически не может ужать
 *     переписку (главная причина «пропавших чатов»: несколько поверхностей
 *     писали каждый свой снимок целиком, последний затирал остальных);
 *   - проекты, которых нет в записываемом снимке, СОХРАНЯЮТСЯ — кроме явно
 *     удалённых (deleteProject ведёт реестр надгробий);
 *   - попытка записи с ужатой перепиской фиксируется в журнал клиента: если
 *     где-то остался ещё один затирающий писатель, мы его увидим, а не
 *     будем снова гадать.
 */
/**
 * ⚠️ ВОЗВРАЩАЕТ УСПЕХ ЗАПИСИ (с 11.08.2026). Раньше было `: void`, и отказ
 * записи глотался: `newChat` создавал чат, которого на диске нет, экран его
 * показывал, а прикрепление документа потом падало с «чат не найден». Именно
 * эту цепочку владелец видел как «документ не прикреплён» — три строки его
 * журнала (`active_chat_lost`, `doc_attach_no_project`, `dialogs_push_partial
 * missing=1`) порождены ей, а не путём надгробий.
 * Прежние вызывающие, которым результат не нужен, ничего не заметят.
 */
export function saveProjects(username: string, projects: Project[]): boolean {
  if (typeof window === "undefined" || !username) return false;
  const disk = loadProjects(username);
  const tombs = loadTombstones(username);
  const byId = new Map<string, Project>();
  for (const d of disk) byId.set(d.id, d);

  const result = new Map<string, Project>();
  for (const p of projects) {
    if (tombs.has(p.id)) continue; // удалён явно — не воскрешаем
    const stored = byId.get(p.id);
    if (!stored) {
      result.set(p.id, p);
      continue;
    }
    if ((stored.messages?.length ?? 0) > (p.messages?.length ?? 0)) {
      reportShrunkWrite(p.id, stored.messages.length, p.messages?.length ?? 0);
    }
    // Документы терялись именно здесь: слияние спасало только переписку, а
    // doc_ids брались у писателя целиком. Писатель со снимком «до загрузки»
    // (вторая вкладка, старый бандл, живая копия хука) стирал только что
    // приложенный документ — ход уходил без файла, а чип оставался в композере.
    const docs = unionBy(stored.doc_ids, p.doc_ids, (d) => d);
    if ((stored.doc_ids?.length ?? 0) > (p.doc_ids?.length ?? 0)) {
      reportShrunkWrite(p.id, stored.doc_ids.length, p.doc_ids?.length ?? 0, "doc_ids");
    }
    result.set(p.id, {
      ...p,
      messages: mergeMessages(stored.messages, p.messages ?? []),
      doc_ids: docs,
      artifacts: unionBy(stored.artifacts, p.artifacts, (a) => a.run_id),
    });
  }
  // Диск-only проекты (их не видел этот писатель) остаются жить.
  for (const d of disk) {
    if (!result.has(d.id) && !tombs.has(d.id)) result.set(d.id, d);
  }
  return writeProjects(username, Array.from(result.values()));
}

/**
 * Отсоединить документ от чата — пользователь передумал его отправлять.
 *
 * Пишем НАПРЯМУЮ, минуя объединяющий saveProjects: там doc_ids сливаются с
 * диском (чтобы чужая запись не могла стереть только что приложенный файл), и
 * отсоединённый документ немедленно вернулся бы. То же осознанное исключение,
 * что и у deleteProject для чатов: явное действие пользователя сильнее защиты
 * от случайной потери. Сам файл на сервере остаётся — удалять чужие данные по
 * снятию чипа мы не будем.
 */
export function detachDoc(username: string, projectId: string, docId: string): void {
  if (typeof window === "undefined" || !username) return;
  const list = loadProjects(username);
  const idx = list.findIndex((p) => p.id === projectId);
  if (idx < 0) return;
  const rest = (list[idx].doc_ids ?? []).filter((d) => d !== docId);
  if (rest.length === (list[idx].doc_ids ?? []).length) return;
  list[idx] = { ...list[idx], doc_ids: rest, updated_at: Date.now() };
  if (!writeProjects(username, list)) return;
  window.dispatchEvent(new Event("finengine:projects"));
}

/** Явное удаление чата: надгробие (30 дней) + запись без него. Только так
 *  проект исчезает по-настоящему — фильтрация списка больше не удаляет. */
export function deleteProject(username: string, id: string): void {
  if (typeof window === "undefined" || !username) return;
  const tombs = loadTombstones(username);
  tombs.set(id, Date.now());
  try {
    localStorage.setItem(
      tombKey(username),
      JSON.stringify(Object.fromEntries(tombs)),
    );
  } catch {
    /* no-op */
  }
  // Ключ, привязанный к ЭТОМУ чату, уходит вместе с ним. Штатно его снимает
  // тот, кто завёл (по завершении прогона), но брошенный прогон на удалённом
  // чате снять его уже некому: чата нет, экрана нет, повода зайти нет. Запись
  // остаётся навсегда и занимает место в тех же 5 МиБ, из-за которых зеркало
  // потом выбрасывает живые диалоги.
  //
  // ⚠️ ТОЛЬКО `ir_cal_run_`, и это проверено, а не предположено. Сценарный
  // ключ `aifa:v2:scenario-run:<id>` в первой редакции стоял здесь же — и был
  // мёртвой строкой: он адресуется id СЦЕНАРИЯ, а не чата (`scenarioId` в
  // v2/page.tsx — отдельное состояние рядом с `activeId`). Тест на него
  // проходил лишь потому, что ключ создавал сам тест. Сценарные ключи чистит
  // их владелец, scenario-view.tsx.
  try {
    localStorage.removeItem(`ir_cal_run_${id}`);
  } catch {
    /* no-op: недоступное хранилище не повод ронять удаление чата */
  }
  const rest = loadProjects(username).filter((p) => p.id !== id);
  writeProjects(username, rest);
}

/**
 * Снять ключи прогонов, чей чат больше не существует.
 *
 * Уборка при удалении лечит будущее, но не прошлое: ключи, осиротевшие до
 * появления той уборки, снять уже некому — их владелец удалён. Метём по одному
 * семейству `ir_cal_run_<id>`, потому что только его id можно сверить со
 * списком чатов.
 *
 * 🔴 СЦЕНАРНЫЕ КЛЮЧИ НЕ ТРОГАЕМ СОЗНАТЕЛЬНО. `aifa:v2:scenario-run:<id>`
 * адресуется id СЦЕНАРИЯ; сверить его со списком чатов нельзя, а подмести «по
 * маске» значило бы стереть указатель живого прогона. Чужое хранилище чистит
 * тот, кто знает, что там лежит.
 *
 * Возвращает число снятых ключей — чтобы уборку было видно, а не только
 * предполагать.
 */
export function sweepOrphanRunKeys(username: string): number {
  if (typeof window === "undefined" || !username) return 0;
  const живые = new Set(loadProjects(username).map((p) => p.id));
  const подсеч: string[] = [];
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith("ir_cal_run_")) continue;
      if (!живые.has(k.slice("ir_cal_run_".length))) подсеч.push(k);
    }
  } catch {
    return 0;
  }
  // Собрали список ДО удаления: `localStorage.key(i)` ходит по живому индексу,
  // и удаление внутри обхода сдвигает его — часть ключей была бы пропущена.
  for (const k of подсеч) {
    try {
      localStorage.removeItem(k);
    } catch {
      /* no-op */
    }
  }
  return подсеч.length;
}

const TOMB_TTL_MS = 30 * 24 * 3600 * 1000;

function tombKey(username: string): string {
  return `aifa:deleted-projects:${username}`;
}

/** Живые надгробия — для пуша на сервер (PUT /dialogs сливает состояния и
 *  удаляет только то, что явно объявлено удалённым). */
export function loadTombstoneIds(username: string): string[] {
  return Array.from(loadTombstones(username).keys());
}

/**
 * Применить удаления сервера: сначала УМЕНЬШИТЬ список чатов, потом записать
 * надгробия. Порядок здесь — лечение, а не стиль.
 *
 * ⚠️ ЗАМОК, ИЗ-ЗА КОТОРОГО ПРАВКА ОТ 11.08 НЕ СРАБОТАЛА С ПЕРВОГО РАЗА.
 * Хранилище владельца было переполнено (168 чатов, ~3,3 МБ). Усыновление
 * надгробий пишет НОВЫЙ ключ — при исчерпанной квоте setItem бросает
 * QuotaExceededError, метки не записываются, и saveProjects по правилу
 * «диск-only проекты остаются жить» возвращает все чаты с диска. Освободить
 * место нельзя, потому что для этого надо записать. Круг замкнут.
 *
 * Разрывается он одним свойством localStorage: перезапись СУЩЕСТВУЮЩЕГО ключа
 * МЕНЬШИМ значением проходит и при исчерпанной квоте — итоговый объём убывает.
 * Поэтому первым делом пишем укороченный список чатов (это и есть освобождение
 * места), и только потом — надгробия, которым теперь есть куда лечь.
 *
 * Если запись надгробий всё же не пройдёт, состояние остаётся корректным:
 * диск уже укорочен, а вернуть чаты обратно может только серверное пополнение,
 * которое mergeServerProjects отфильтрует по тем же меткам на следующем круге.
 */
export function applyServerDeletions(
  username: string,
  serverDeleted: Record<string, number> | null | undefined,
): { removed: number; adopted: number } {
  const none = { removed: 0, adopted: 0 };
  if (typeof window === "undefined" || !username) return none;
  if (!serverDeleted || typeof serverDeleted !== "object") return none;
  if (!Object.keys(serverDeleted).length) return none;

  const disk = loadProjects(username);
  const kept = disk.filter((p) => !Object.hasOwn(serverDeleted, p.id));
  const removed = disk.length - kept.length;
  // Порядок обязателен: сокращение — раньше надгробий (см. комментарий выше).
  if (removed) {
    let how = "replace";
    if (!writeProjects(username, kept)) {
      // Замена меньшим значением обязана проходить и при исчерпанной квоте —
      // объём убывает. Но полагаться на это как на единственный путь нельзя:
      // если браузер откажет и здесь, замок останется, а владелец останется
      // без загрузки файлов. Тогда — снять ключ (removeItem освобождает всё
      // и квотой не ограничен) и записать в пустое место.
      //
      // Окно потери: между removeItem и setItem. Данные при этом есть на
      // сервере (мы только что забрали снимок этим же ходом) и в `kept` в
      // памяти, так что окно закрывается следующей же строкой.
      how = "remove+set";
      try {
        localStorage.removeItem(projectsKey(username));
      } catch {
        /* нечего снимать */
      }
      writeProjects(username, kept);
    }
    reportStorage(
      "server_deletions_applied",
      `removed=${removed}; kept=${kept.length}; tombs=${Object.keys(serverDeleted).length}; how=${how}`,
    );
  }
  return { removed, adopted: adoptServerTombstones(username, serverDeleted) };
}

/**
 * Принять надгробия сервера в свой список удалённых.
 *
 * Без этого фильтр в mergeServerProjects бесполезен: saveProjects по правилу
 * «диск-only проекты остаются жить» вернёт отфильтрованные чаты обратно с
 * диска — проверку он ведёт по ЛОКАЛЬНЫМ надгробиям, а не по серверным.
 * Усыновление заодно чинит и обратный путь: усыновлённые метки уезжают в PUT
 * (loadTombstoneIds), так что сервер узнаёт, что клиент удаление принял.
 *
 * Сервер держит время в секундах, локальный список — в миллисекундах.
 * Возвращает, сколько меток добавилось (0 — писать нечего).
 *
 * Звать напрямую НЕ НАДО — только через applyServerDeletions, которая
 * соблюдает порядок «сначала освободить место, потом писать метки».
 */
export function adoptServerTombstones(
  username: string,
  serverDeleted: Record<string, number> | null | undefined,
): number {
  if (typeof window === "undefined" || !username) return 0;
  if (!serverDeleted || typeof serverDeleted !== "object") return 0;
  const tombs = loadTombstones(username);
  let added = 0;
  for (const [id, ts] of Object.entries(serverDeleted)) {
    if (tombs.has(id)) continue;
    const ms = typeof ts === "number" && ts > 0 ? ts * 1000 : Date.now();
    if (Date.now() - ms >= TOMB_TTL_MS) continue; // просроченное — не воскрешаем как метку
    tombs.set(id, ms);
    added += 1;
  }
  if (!added) return 0;
  try {
    localStorage.setItem(tombKey(username), JSON.stringify(Object.fromEntries(tombs)));
  } catch (e) {
    // ⚠️ ЭТОТ catch БЫЛ НЕМЫМ, и разбор 11.08 из-за этого встал. В журнале
    // было видно `projects_write_failed`, но НИ СЛОВА о надгробиях — отличить
    // «хранилище переполнено, метки не легли» от «сервер не прислал deleted»
    // по логам было нечем.
    const err = e as Error;
    reportStorage(
      "tombstones_write_failed",
      `${err?.name || "Error"}: ${err?.message || "нет описания"}; tombs=${tombs.size}; added=${added}`,
    );
    return 0; // не записали — значит и не приняли; фильтр отработает в следующий раз
  }
  return added;
}

function loadTombstones(username: string): Map<string, number> {
  try {
    const raw = localStorage.getItem(tombKey(username));
    const obj = raw ? (JSON.parse(raw) as Record<string, number>) : {};
    const now = Date.now();
    return new Map(Object.entries(obj).filter(([, ts]) => now - ts < TOMB_TTL_MS));
  } catch {
    return new Map();
  }
}

/** След в серверный журнал. Сбои хранилища в браузере иначе не видны никому. */
export function reportStorage(kind: string, detail: string, projectId = ""): void {
  try {
    void fetch("/api/client-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind,
        project_id: projectId,
        detail: `${detail}; url=${location.pathname}`,
        at: Date.now(),
        url: location.pathname,
      }),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    /* диагностика не должна ломать запись */
  }
}

/** Тревога: какой-то писатель принёс ужатую переписку. Данные мы не потеряли
 *  (слияние выше), но источник надо найти. */
function reportShrunkWrite(
  projectId: string,
  was: number,
  got: number,
  what: "messages" | "doc_ids" = "messages",
): void {
  reportStorage("thread_shrunk_write", `payload ${got} ${what} < disk ${was}`, projectId);
}

/**
 * Единственная точка записи списка проектов.
 *
 * Раньше `localStorage.setItem` стоял голым в трёх местах: исчерпание квоты
 * (в Safari она жёсткая) или приватный режим роняли ИСКЛЮЧЕНИЕ прямо посреди
 * обработчика — правка терялась, а пользователь видел лишь «всё пропало» без
 * единого следа. Теперь падение записи фиксируется и не рушит вызывающий код.
 */
/** Последняя неудача записи — чтобы вызывающий мог назвать пользователю ПРИЧИНУ.
 *
 * ЗАЧЕМ (09.08.2026, инцидент владельца). Хранилище переполнилось на 168 чатах:
 * `QuotaExceededError`, запись не проходит, новый чат не сохраняется — и загрузка
 * документа падает с сообщением «чат не найден в хранилище — обновите страницу».
 * Сообщение было формально верным и практически вредным: обновление страницы
 * места не освобождает, и пользователь жмёт его по кругу.
 *
 * Причина известна ровно здесь, а сообщение показывается тремя уровнями выше,
 * поэтому её приходится доносить полем модуля. Это некрасиво, но честнее, чем
 * гадать о причине в месте показа.
 */
let _lastWriteFailure: { quota: boolean; projects: number } | null = null;

export function lastStorageFailure(): { quota: boolean; projects: number } | null {
  return _lastWriteFailure;
}

/**
 * Потолок localStorage, ИЗМЕРЕННЫЙ на инциденте 11.08.2026, а не взятый из
 * головы. По снимку хранилища владельца: 167 чатов = 2 627 261 символ — влезло,
 * 168 чатов = 2 627 477 — упало. Окно 216 символов. Значит счёт идёт в UTF-16
 * (2 байта на символ) и предел ≈ 5 МиБ, то есть 2 621 440 символов.
 *
 * Порог предупреждения — 75%: остатка в четверть хватает на пару прогонов,
 * чтобы человек успел почистить, а не упёрся посреди работы.
 */
const STORAGE_CEILING_CHARS = 2_621_440;
const WARN_AT = 0.75;
const WARN_AGAIN_AT = 0.9;
let _warnedShare = 0;

/**
 * Предупредить ЗАРАНЕЕ. Раньше первым сигналом была невозможность приложить
 * документ: до этого момента всё выглядело исправным. Один тяжёлый прогон
 * (извлечение + анализ) весит 90–170 КБ, так что от «ещё нормально» до
 * «встало» — десяток прогонов, и заметить их без счётчика нечем.
 *
 * ⚠️ После введения `fitToBudget` сюда доходит РАЗМЕР УЖЕ ОБРЕЗАННОГО зеркала,
 * то есть порог не должен срабатывать вовсе. Предупреждение оставлено сторожем
 * на случай, если бюджет разъедется с реальным потолком браузера, — но текст
 * больше не велит человеку чистить руками: чистит код.
 */
function warnIfNearFull(chars: number, projects: number): void {
  const share = chars / STORAGE_CEILING_CHARS;
  const step = share >= WARN_AGAIN_AT ? WARN_AGAIN_AT : share >= WARN_AT ? WARN_AT : 0;
  if (!step || step <= _warnedShare) return;
  _warnedShare = step;
  const pct = Math.round(share * 100);
  reportStorage("projects_storage_pressure", `chars=${chars}; projects=${projects}; pct=${pct}`);
  notify(
    step >= WARN_AGAIN_AT ? "error" : "warn",
    `Хранилище браузера заполнено на ${pct}% даже после разгрузки — это сбой ` +
      `бюджета зеркала, сообщите разработчику.`,
  );
}

/** Доля потолка, которую зеркало имеет право занимать.
 *
 * Не 100%: под тем же потолком живут метки удаления, папки, настройки вида и
 * ключи чужих вкладок. Съесть всё до края значит уронить их запись, а метки
 * удаления при переполнении — единственное, чем чат вообще убирается.
 */
const MIRROR_BUDGET = 0.6;

/**
 * Обрезать зеркало до бюджета, начиная со САМЫХ СТАРЫХ чатов.
 *
 * ЗАЧЕМ (инцидент владельца 15.08: 75% на 44 чатах). localStorage хранил ВСЕ
 * чаты одним блоком, потолок ~5 МиБ, а один тяжёлый прогон весит 90–170 КБ.
 * Через полсотни прогонов запись переставала проходить, и первым это замечал
 * не человек, а загрузка документа: «чат не найден в хранилище». Пользователю
 * предлагалось чистить чаты руками — то есть система просила человека
 * обслуживать свою же поломку.
 *
 * Чистить руками не нужно, потому что localStorage — НЕ единственная копия.
 * Полный список лежит на сервере (`GET /dialogs`), а при загрузке страницы
 * `page.tsx` сливает серверный снимок с локальным по `updated_at`. Значит
 * выброшенный отсюда чат не потерян: он вернётся следующим заходом.
 *
 * 🔴 ПОРЯДОК ВЫБРОСА — ОТ СТАРЫХ К НОВЫМ, И ЭТО НЕ ВКУСОВЩИНА. Свежий чат
 * может быть ещё НЕ доехавшим до сервера (PUT идёт с задержкой); выброси его —
 * и он исчезнет навсегда. Старые к этому моменту на сервере заведомо есть.
 */
function fitToBudget(list: Project[]): { payload: string; kept: number; dropped: number } {
  const full = JSON.stringify(list);
  const budget = Math.floor(STORAGE_CEILING_CHARS * MIRROR_BUDGET);
  if (full.length <= budget || list.length <= 1) {
    return { payload: full, kept: list.length, dropped: 0 };
  }
  // Свежие — первыми, чтобы отрезать хвост, а не голову.
  const byFresh = [...list].sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));

  // 🔴 МЕРА СНИМАЕТСЯ ОДИН РАЗ С КАЖДОГО, А НЕ ЗАНОВО СО ВСЕГО СПИСКА (24.08).
  //
  // Здесь стоял цикл «убавить один и сериализовать заново». На профиле
  // владельца — 104 чата, зеркало 1,47 МБ — он звал JSON.stringify 66 раз и
  // держал главный поток 181 мс НА КАЖДУЮ ЗАПИСЬ. А запись идёт на каждой
  // подтяжке с сервера, и подтяжка бьёт на каждый возврат на вкладку: сервер
  // отдаёт 104, зеркало вмещает ~40, список сходится к тому же — и цикл
  // повторяется в том же объёме. Замер после правки: 8,4 мс, 21,7×.
  //
  // Длина JSON-массива считается точно и без сериализации: скобки (2) + сумма
  // элементов + запятые между ними (n−1). Размер префикса растёт монотонно,
  // поэтому накопление даёт ТОТ ЖЕ префикс, что и убавление, — равенство обеих
  // веток закреплено тестом против наивной реализации.
  let занято = 2;
  let keep = 0;
  for (const item of byFresh) {
    const вес = JSON.stringify(item).length + (keep ? 1 : 0);
    if (занято + вес > budget) break;
    занято += вес;
    keep += 1;
  }
  // Один чат остаётся всегда, даже если он один не влезает: пустое зеркало
  // хуже переполненного — по нему загрузка документа говорит «чат не найден».
  if (keep < 1) keep = 1;
  return {
    payload: JSON.stringify(byFresh.slice(0, keep)),
    kept: keep,
    dropped: list.length - keep,
  };
}

/** Наивная мера того же — только для теста равенства. Не для рантайма. */
export function __fitToBudgetНаивно(list: Project[], budget: number): Project[] {
  if (JSON.stringify(list).length <= budget || list.length <= 1) return [...list];
  const byFresh = [...list].sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
  let keep = byFresh.length;
  while (keep > 1) {
    keep -= 1;
    if (JSON.stringify(byFresh.slice(0, keep)).length <= budget) break;
  }
  return byFresh.slice(0, keep);
}

/** Тот же отбор, но параметризованный бюджетом — чтобы его можно было мерить. */
export function __fitToBudgetБыстро(list: Project[], budget: number): Project[] {
  if (JSON.stringify(list).length <= budget || list.length <= 1) return [...list];
  const byFresh = [...list].sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0));
  let занято = 2;
  let keep = 0;
  for (const item of byFresh) {
    const вес = JSON.stringify(item).length + (keep ? 1 : 0);
    if (занято + вес > budget) break;
    занято += вес;
    keep += 1;
  }
  return byFresh.slice(0, keep < 1 ? 1 : keep);
}

function writeProjects(username: string, list: Project[]): boolean {
  try {
    const { payload, kept, dropped } = fitToBudget(list);
    localStorage.setItem(projectsKey(username), payload);
    _lastWriteFailure = null;
    if (dropped > 0) reportStorage("projects_storage_trimmed",
      `chars=${payload.length}; kept=${kept}; dropped=${dropped}`);
    warnIfNearFull(payload.length, list.length);
    return true;
  } catch (e) {
    const err = e as Error;
    // Safari зовёт её QuotaExceededError, Firefox — NS_ERROR_DOM_QUOTA_REACHED;
    // ловим по имени И по тексту, чтобы не зависеть от браузера.
    const quota = /quota/i.test(`${err?.name || ""} ${err?.message || ""}`);
    _lastWriteFailure = { quota, projects: list.length };
    reportStorage(
      "projects_write_failed",
      `${err?.name || "Error"}: ${err?.message || "нет описания"}; projects=${list.length}`,
    );
    return false;
  }
}

export function loadLastProjectId(username: string): string | null {
  if (typeof window === "undefined" || !username) return null;
  return localStorage.getItem(lastProjectKey(username));
}

export function saveLastProjectId(username: string, id: string | null): void {
  if (typeof window === "undefined" || !username) return;
  if (id) localStorage.setItem(lastProjectKey(username), id);
  else localStorage.removeItem(lastProjectKey(username));
}

export function companyNameFromFilename(filename: string): string | null {
  const base = filename.replace(/\.[a-z]{2,5}$/i, "");
  const tokens = base.split(/[_\-\s.]+/).filter(Boolean);
  const cyrillic = tokens.filter((t) => /^[А-Яа-яЁё]{3,}$/.test(t));
  if (cyrillic.length > 0) {
    cyrillic.sort((a, b) => b.length - a.length);
    return cyrillic[0];
  }
  const latin = tokens.filter((t) => /^[A-Za-z]{3,}$/.test(t));
  if (latin.length > 0) {
    latin.sort((a, b) => b.length - a.length);
    return latin[0];
  }
  return null;
}

export function createProject(
  name: string = "Новый проект",
  project_type: ProjectType = "generic",
): Project {
  const ts = now();
  return {
    id: uid(),
    name,
    created_at: ts,
    updated_at: ts,
    project_type,
    doc_ids: [],
    messages: [],
    artifacts: [],
  };
}

// ─────────────────────────── Project-folders ───────────────────────────
// A Folder groups several chats (Project entities) with shared files +
// context, like a Claude Cowork project. Stored in its own localStorage key
// and synced in the /dialogs blob alongside `projects`.

export function loadFolders(username: string): Folder[] {
  if (typeof window === "undefined" || !username) return [];
  try {
    const raw = localStorage.getItem(foldersKey(username));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Folder[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveFolders(username: string, folders: Folder[]): void {
  if (typeof window === "undefined" || !username) return;
  try {
    localStorage.setItem(foldersKey(username), JSON.stringify(folders));
  } catch (e) {
    // Голый setItem бросал исключение прямо из обработчика при переполненной
    // квоте — то же, от чего в writeProjects защитились ещё 09.08, а здесь
    // осталось (найдено разбором 11.08). Папки не критичнее того кода, что
    // выполняется после них.
    const err = e as Error;
    reportStorage(
      "folders_write_failed",
      `${err?.name || "Error"}: ${err?.message || "нет описания"}; folders=${folders.length}`,
    );
  }
}

export function createFolder(name: string = "Новый проект"): Folder {
  const ts = now();
  return { id: uid(), name, instructions: "", doc_ids: [], created_at: ts, updated_at: ts };
}

/** Chats that belong to a folder, newest first. */
export function chatsInFolder(projects: Project[], folderId: string): Project[] {
  return projects
    .filter((p) => p.folder_id === folderId)
    .sort((a, b) => b.updated_at - a.updated_at);
}

/** Standalone chats (not in any folder) = the «Недавние чаты» list. Routine
 * projects (ir_calendar/ir_daily) are excluded — they live under Автоматизации. */
export function standaloneChats(projects: Project[]): Project[] {
  return projects
    .filter((p) => !p.folder_id && (!p.project_type || p.project_type === "generic"))
    .sort((a, b) => b.updated_at - a.updated_at);
}

/** Default display name suggested for a given template. The user can
 * still rename freely after creation. */
export function defaultProjectName(t: ProjectType): string {
  switch (t) {
    case "ir_calendar":
      return "IR Calendar";
    case "ir_daily":
      return "Daily News";
    default:
      return "Новый проект";
  }
}

export const PROJECT_TEMPLATES: ReadonlyArray<{
  type: ProjectType;
  title: string;
  blurb: string;
}> = [
  {
    type: "ir_calendar",
    title: "IR Calendar",
    blurb: "Календарь корпоративных событий по 72 компаниям из аналитического покрытия — отчётности, дни инвестора, дивидендные отсечки на 14 дней вперёд.",
  },
  {
    type: "ir_daily",
    title: "Daily News",
    blurb: "Ежедневный дайджест ключевых новостей по российскому рынку, рынкам США и Китая. Топ-10 событий с категорией и привязкой к watchlist.",
  },
  {
    type: "generic",
    title: "Пустой проект",
    blurb: "Чат с агентом + загрузка собственных документов. Без предустановленных скиллов.",
  },
];

export function touch(p: Project): Project {
  return { ...p, updated_at: now() };
}

export type MetricRow = MetricEvent & {
  doc_id: string;
  doc_label: string;
  period_current: string;
  period_prev: string;
  extracted_at: number;
};

/** Stable per-row identity for de-dup and React keys. Beyond the enum it folds
 * in segment/dimension and the source pages/text so the same enum appearing in
 * several segments or note pages doesn't collapse into one row. */
export function metricIdentityKey(
  metric: Pick<MetricEvent, "enum" | "segment" | "dimension" | "page" | "period_values"> & {
    doc_id?: string;
  },
): string {
  const pvPages = (metric.period_values ?? [])
    .map((pv) => pv.page)
    .filter((p) => typeof p === "number" && p > 0)
    .join(",");
  const pvSource = (metric.period_values ?? [])
    .map((pv) => pv.source_text?.slice(0, 80).trim() ?? "")
    .filter(Boolean)
    .join("|");
  return [
    metric.doc_id ?? "",
    metric.enum,
    metric.segment ?? "",
    metric.dimension ?? "",
    metric.page ?? "",
    pvPages,
    pvSource,
  ].join("::");
}

/**
 * Строка ленты — ИЗВЛЕКАЕМЫЙ показатель реестра, а не что-то ещё.
 *
 * Движок эмитит `{"type":"metric"}` из шести мест: извлекаемые показатели,
 * рассчитываемые коэффициенты, сегментные разрезы (операционные/география/
 * продукты), состав консолидированной группы и total-фолбэк сегментов. Все они
 * приходят одной лентой, и различить их можно только по этим меткам:
 *   `is_calculated` — посчитан кодом по формуле, из отчёта не читался;
 *   `dimension`/`segment` — строка разреза, а не показатель реестра.
 * Знаменатель прогресса (`total_extractable`) считает ТОЛЬКО извлекаемые —
 * значит и числитель обязан считать их же.
 */
export function isExtractableMetric(m: MetricEvent): boolean {
  if (m.is_calculated) return false;
  if ((m.dimension ?? "").trim()) return false;
  if ((m.segment ?? "").trim()) return false;
  return true;
}

/**
 * Дробь прогресса извлечения — ОДНА на весь фронт.
 *
 * ДЕФЕКТ (найден владельцем на живом прогоне): карточка воркера показывала
 * «156/77 ГОТОВО». Числитель брали как `metrics.length` — длину ленты ВСЕХ
 * событий `metric` (77 извлекаемых + коэффициенты + сегменты + группа), а
 * знаменатель — `total_extractable`, то есть только извлекаемые. Две разные
 * величины в одной дроби: числитель законно перерастал знаменатель, и человек
 * читал это как поломку счёта.
 *
 * Здесь числитель и знаменатель ходят по ОДНОМУ множеству. Считаем РАЗНЫЕ
 * enum'ы: движок добирает пропущенные показатели вторым проходом по буферу
 * (leftover), и одна и та же ячейка не должна засчитаться дважды.
 *
 * Дробь НЕ подрезается под знаменатель. Подрезка («не больше 77 из 77»)
 * спрятала бы ровно тот класс ошибки, ради которого это писалось: числитель,
 * посчитанный по другому множеству. Если она снова разъедется — пусть будет
 * видно, а не «готово» при незаконченной работе.
 */
export function extractionProgress(msg: {
  metrics: MetricEvent[];
  meta?: { total_extractable: number };
}): { done: number; total: number } {
  const seen = new Set<string>();
  for (const m of msg.metrics) {
    if (!isExtractableMetric(m)) continue;
    const id = String(m.enum ?? "").trim().toUpperCase();
    if (id) seen.add(id);
  }
  return { done: seen.size, total: msg.meta?.total_extractable ?? 0 };
}

export type TurnRunStatus = "running" | "done" | "error" | "aborted";

/** Линия работ хода: под-план одного исполнителя (`name` — имя тула). */
export type TurnLane = { name: string; label: string; steps: PlanStep[] };

/** Карточка исполнителя хода: фоновый воркер извлечения или тул этого чата. */
export type TurnWorker = {
  id: string;
  label: string;
  status: TurnRunStatus;
  found: number;
  total: number;
};

export type TurnProgress = { lanes: TurnLane[]; workers: TurnWorker[] };

/**
 * Согласовать шаги плана с фактическим состоянием исполнителя.
 *
 * ДЕФЕКТ (найден владельцем на живом прогоне): план и карточки исполнителей —
 * два НЕЗАВИСИМЫХ источника. Шаги приезжают из станции событиями plan/plan_step,
 * карточки — от бэкенда (прогоны и вызовы тулов). Синхронизации не было, и
 * рядом на одном экране висело «воркер: ГОТОВО» при «шаг: ВЫПОЛНЯЕТСЯ» и
 * «Финансовый анализ: РАБОТАЕТ» при «шаг: В ОЧЕРЕДИ».
 *
 * Кто главный: РАБОТА, а не рассказ о ней. Прогон/вызов знает факт «начал» и
 * «кончил», события плана знают только деталь — какой шаг внутри идёт сейчас.
 * Поэтому терминальное состояние линии задаёт исполнитель, а plan_step
 * уточняет её внутри. Так противоречие не может возникнуть: обе надписи
 * выводятся из одного факта.
 *
 * Прерванный и упавший прогон закрывают идущий шаг ошибкой: отдельного статуса
 * «не доделан» у шага нет, а вечно крутящийся шаг после остановки — то же
 * враньё, ради которого всё это и пишется. Невзятые шаги остаются «в очереди»
 * — они действительно не начинались.
 */
function reconcileSteps(steps: PlanStep[], owner: TurnRunStatus | null): PlanStep[] {
  if (!owner || !steps.length) return steps;
  if (owner === "done") {
    if (steps.every((s) => s.status === "done" || s.status === "error")) return steps;
    return steps.map((s) => (s.status === "error" ? s : { ...s, status: "done" as const }));
  }
  if (owner === "error" || owner === "aborted") {
    if (!steps.some((s) => s.status === "running")) return steps;
    return steps.map((s) => (s.status === "running" ? { ...s, status: "error" as const } : s));
  }
  // Исполнитель уже работает, а план весь «в очереди» — объявление шагов
  // приходит раньше первой отметки. Первый шаг идёт по факту.
  if (steps.some((s) => s.status !== "pending")) return steps;
  return steps.map((s, i) => (i === 0 ? { ...s, status: "running" as const } : s));
}

/**
 * Состояние ТЕКУЩЕГО хода одним куском: линии плана + карточки исполнителей.
 *
 * Обе половины острова «Прогресс» выводятся ЗДЕСЬ, из одного разбора истории.
 * Раньше страница считала их двумя независимыми memo, и рассинхрон между ними
 * был вопросом времени. Дробь карточки берётся из `extractionProgress` — той
 * же, что рисует чат и панель показателей.
 */
export function turnProgress(project: Project | null | undefined): TurnProgress {
  const msgs = project?.messages ?? [];

  // План: ход, который ИДЁТ, иначе последний объявивший шаги — завершённый
  // план остаётся на экране с зелёными галочками, а не пропадает.
  const runningId = project?.activeChatRun?.assistant_id;
  const runningMsg = runningId ? msgs.find((m) => m.id === runningId) : undefined;
  const hasNodeRuns = (m: ChatMessage): m is TextMessage =>
    m.role === "assistant" && (
      executionGraphsOf(m as TextMessage).some((graph) => Object.keys(graph.nodeRuns).length > 0)
      || Boolean(Object.keys((m as TextMessage).nodeRuns ?? {}).length)
    );
  let graphMsg: TextMessage | null =
    runningMsg && hasNodeRuns(runningMsg) ? runningMsg : null;
  if (!graphMsg) {
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (hasNodeRuns(msgs[i])) {
        graphMsg = msgs[i] as TextMessage;
        break;
      }
    }
  }
  const hasPlan = (m: ChatMessage): m is TextMessage =>
    m.role === "assistant" && Boolean((m as TextMessage).plans?.length);
  let planMsg: TextMessage | null =
    runningMsg && hasPlan(runningMsg) ? runningMsg : null;
  if (!planMsg) {
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (hasPlan(m)) {
        planMsg = m;
        break;
      }
    }
  }

  // Ход = всё после последней реплики пользователя.
  let since = 0;
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i].role === "user") {
      since = msgs[i].created_at;
      break;
    }
  }

  // «Кто ещё работает» перевешивает «кто уже закончил»: один и тот же тул может
  // прийти и карточкой прогона, и вызовом в чате.
  const RANK: Record<TurnRunStatus, number> = { done: 0, aborted: 1, error: 2, running: 3 };
  const owners = new Map<string, TurnRunStatus>();
  const noteOwner = (tool: string, st: TurnRunStatus) => {
    const prev = owners.get(tool);
    if (!prev || RANK[st] > RANK[prev]) owners.set(tool, st);
  };

  const workers: TurnWorker[] = [];
  for (const m of msgs) {
    if (m.created_at < since) continue;
    if (m.role === "extraction") {
      const e = m as ExtractionMessage;
      const { done, total } = extractionProgress(e);
      workers.push({
        id: e.id,
        label: "Извлечение · фоновый воркер",
        status: e.status,
        found: done,
        total,
      });
      noteOwner("fe_extract_metrics", e.status);
    } else if (m.role === "assistant") {
      for (const t of (m as TextMessage).toolCalls ?? []) {
        const st: TurnRunStatus =
          t.status === "error" ? "error" : t.status === "done" ? "done" : "running";
        noteOwner(t.name, st);
        // Извлечение уже показано карточкой выше — не задваиваем.
        if (t.name === "fe_extract_metrics") continue;
        workers.push({
          id: `${m.id}:${t.name}`,
          label: `${toolLabel(t.name).step} · в этом чате`,
          status: st,
          found: 0,
          total: 0,
        });
      }
    }
  }

  // Once NodeRun events exist they are the sole progress contract. Model plan
  // prose remains a compatibility display for old runs only.
  const progressGraph = graphMsg ? latestExecutionGraph(executionGraphsOf(graphMsg)) : null;
  const graphSteps: PlanStep[] = Object.values(progressGraph?.nodeRuns ?? graphMsg?.nodeRuns ?? {}).map((run) => ({
    title: run.node_key.atom_name,
    status: nodeStepStatus(run, Boolean(progressGraph?.terminal)),
    note: run.error ?? nodeStatusLabel(run, Boolean(progressGraph?.terminal)),
    startedAt: run.started_at ? Date.parse(run.started_at) : undefined,
    endedAt: run.finished_at ? Date.parse(run.finished_at) : undefined,
  }));
  const lanes: TurnLane[] = graphSteps.length
    ? [{ name: "plan_graph", label: "Граф выполнения", steps: graphSteps }]
    : (planMsg?.plans ?? []).map((l) => ({
        name: l.name,
        label: toolLabel(l.name).step,
        steps: reconcileSteps(l.steps, owners.get(l.name) ?? null),
      }));

  return { lanes, workers };
}

/**
 * Build a finished ExtractionMessage from a frozen demo snapshot. Same shape
 * as a live SSE run that has fully completed — UI renders it identically.
 */
export function extractionMessageFromDemo(opts: {
  doc_id: string;
  doc_label: string;
  metrics: MetricEvent[];
  period_current: string;
  period_prev: string;
  unit: string;
  company_type?: string;
  total_extractable?: number;
}): ExtractionMessage {
  // `total_extractable` обязан значить то же, что и в живом прогоне, — иначе
  // счётчик демо-карточки покажет «77 из 103»: числитель по извлекаемым,
  // знаменатель по всей ленте. Раньше сюда клали длину ленты целиком.
  const extractableCount = new Set(
    opts.metrics.filter(isExtractableMetric).map((m) => String(m.enum ?? "").trim().toUpperCase()),
  ).size;
  const total = opts.total_extractable ?? extractableCount;
  return {
    id: `demo-${opts.doc_id}`,
    role: "extraction",
    doc_id: opts.doc_id,
    doc_label: opts.doc_label,
    company_type: opts.company_type ?? "unknown",
    created_at: Date.now(),
    started_at: Date.now(),
    elapsed_ms: 0,
    status: "done",
    meta: {
      total_metrics: opts.metrics.length,
      total_extractable: total,
      company_type: opts.company_type ?? "unknown",
      unit: opts.unit,
      period_current: opts.period_current,
      period_prev: opts.period_prev,
    },
    metrics: opts.metrics,
    event_count: opts.metrics.length,
  };
}

export function collectMetrics(
  project: Project | null,
  docId: string | null,
): MetricRow[] {
  if (!project) return [];
  const out = new Map<string, MetricRow>();
  for (const m of project.messages) {
    if (m.role !== "extraction") continue;
    if (docId && m.doc_id !== docId) continue;
    for (const metric of m.metrics) {
      const row: MetricRow = {
        ...metric,
        doc_id: m.doc_id,
        doc_label: m.doc_label,
        period_current: m.meta?.period_current ?? "",
        period_prev: m.meta?.period_prev ?? "",
        extracted_at: m.created_at,
      };
      const key = metricIdentityKey(row);
      const existing = out.get(key);
      if (!existing || existing.extracted_at <= row.extracted_at) {
        out.set(key, row);
      }
    }
  }
  return Array.from(out.values());
}

/** Return all extraction messages for a doc, newest-first. Each entry is
 * its own run (different arch / model / time) — UI renders them as
 * separate cards so the user can compare and never loses history. */
export function extractionsForDoc(
  project: Project | null,
  docId: string | null,
): ExtractionMessage[] {
  if (!project) return [];
  const out: ExtractionMessage[] = [];
  for (const m of project.messages) {
    if (m.role !== "extraction") continue;
    if (docId && m.doc_id !== docId) continue;
    out.push(m);
  }
  return out.sort((a, b) => b.created_at - a.created_at);
}

/** Build MetricRow[] from a single ExtractionMessage. Used by per-extraction
 * cards so each card shows exactly the metrics from its own run. */
export function rowsFromExtraction(m: ExtractionMessage): MetricRow[] {
  return m.metrics.map((metric) => ({
    ...metric,
    doc_id: m.doc_id,
    doc_label: m.doc_label,
    period_current: m.meta?.period_current ?? "",
    period_prev: m.meta?.period_prev ?? "",
    extracted_at: m.created_at,
  }));
}

export function findRunningExtraction(
  project: Project | null,
  docId: string | null,
): ExtractionMessage | null {
  if (!project) return null;
  for (let i = project.messages.length - 1; i >= 0; i--) {
    const m = project.messages[i];
    if (m.role !== "extraction") continue;
    if (docId && m.doc_id !== docId) continue;
    if (m.status === "running") return m;
  }
  return null;
}

/** Проза хода — в заметку ленты, ответ обнуляется. Одно правило демоции на
 * оба вызывающих места: ветку `tool_call` редьюсера и старт извлечения в
 * `use-chat-run` (перед `extract_start` модель пишет «создам файл и запущу
 * расчёт» — это хроника, не ответ; прогон 9d2dad93, 31.08.2026). */
export function demoteProseToNote<M extends { content: string; beats?: Beat[] }>(m: M): M {
  const note = (m.content || "").trim();
  if (!note) return m;
  return {
    ...m,
    beats: [...(m.beats ?? []), { kind: "note" as const, text: note }],
    content: "",
  };
}

/** Карточка для РАБОЧЕЙ ОБЛАСТИ. Точечный заказ live в область не идёт
 * (решение владельца 31.08.2026): его таблица живёт в чате, а область
 * обновится сохранённым итогом после завершения — потому фильтруется
 * ровно сочетание «narrow_order и running». Завершённый точечный прогон
 * область показывает: это уже итог, а не стрим. */
export function areaExtractionOf(runs: ExtractionMessage[]): ExtractionMessage | null {
  return runs.find((r) => !(r.narrow_order && r.status === "running")) ?? null;
}

/** Шапка прогона, как её присылает бэкенд событием `extract_start` и как она
 *  же лежит в сохранённом экране (`screen.start`). */
export type ExtractStartBody = {
  doc_id?: string;
  company_name?: string;
  company_type?: string;
  total_extractable?: number;
  /** Есть только у реплей-стрима фонового воркера; у события хода и в
   *  сохранённом экране его нет — там знаменатель один. */
  total_metrics?: number;
  period_current?: string;
  period_prev?: string;
  unit?: string;
  /** Точечный заказ — см. одноимённое поле ExtractionMessage. */
  narrow_order?: boolean;
};

/**
 * Пустая карточка извлечения по шапке прогона.
 *
 * ОДНА НА ДВА ВХОДА: живой поток (`use-chat-run`, событие `extract_start`) и
 * сохранённый экран прогона (`extractionFromStoredRun`, #276). Второй копией
 * эти два разъехались бы на первом же новом поле шапки, и разошлись бы молча:
 * обе карточки выглядят правдоподобно, а различие видно только сверкой.
 */
export function extractionFromStart(
  start: ExtractStartBody,
  opts: {
    id: string;
    doc_id: string;
    doc_label: string;
    at: number;
    arch?: string;
    run_id?: string;
  },
): ExtractionMessage {
  return {
    id: opts.id,
    role: "extraction",
    doc_id: opts.doc_id,
    doc_label: opts.doc_label,
    company_type: start.company_type || "unknown",
    created_at: opts.at,
    started_at: opts.at,
    status: "running",
    metrics: [],
    event_count: 0,
    arch: opts.arch,
    run_id: opts.run_id,
    narrow_order: Boolean(start.narrow_order),
    meta: {
      total_metrics: start.total_metrics ?? start.total_extractable ?? 0,
      total_extractable: start.total_extractable ?? 0,
      company_type: start.company_type || "",
      unit: start.unit || "",
      period_current: start.period_current || "",
      period_prev: start.period_prev || "",
      company_name: start.company_name || undefined,
    },
  };
}

/**
 * Карточка извлечения из прогона, ПРОЧИТАННОГО С СЕРВЕРА (#276).
 *
 * ЗАЧЕМ. Таблица прогона жила в браузере: закрыл вкладку раньше конца прогона
 * — час работы движка терялся, и 18.08 так пропали ДОМ.РФ и «Самолёт».
 * Бэкенд теперь кладёт собранный экран в строку прогона; здесь он
 * разворачивается обратно.
 *
 * ⚠️ РАЗВОРАЧИВАЕМ ТЕМ ЖЕ РЕДЬЮСЕРОМ, ЧТО И ЖИВОЙ ПОТОК. `applyExtractEvent`
 * — единственное место, знающее, как поля события ложатся в карточку (роль,
 * белый список раскладки, периоды, `formatted`). Разобрать сохранённый экран
 * своим кодом значило бы завести ВТОРУЮ редакцию карточки — ровно тот дефект,
 * на котором сгорели два рисовальщика таблицы.
 *
 * `null` — «показывать нечего»: экран не сохранён (прогон шёл до этой правки)
 * либо в нём нет строк. Пустая карточка была бы хуже отсутствия: она означает
 * «прогон прошёл и не нашёл ничего».
 */
export function extractionFromStoredRun(
  stored: {
    nonce: string;
    doc_id: string;
    finished_at: number;
    status?: string;
    error?: string | null;
    screen: {
      start?: ExtractStartBody;
      rows?: unknown[];
      done?: Record<string, unknown> | null;
    } | null;
  },
  docLabel: string,
): ExtractionMessage | null {
  const rows = stored.screen?.rows ?? [];
  if (!rows.length) return null;
  const start = stored.screen?.start ?? {};
  // `finished_at` — секунды сервера; у карточки время в миллисекундах.
  const at = stored.finished_at > 0 ? stored.finished_at * 1000 : Date.now();
  const base = extractionFromStart(start, {
    // Нонс в id намеренно: карточка одного прогона не должна ни размножаться
    // при повторном чтении, ни путаться с локальной от того же документа.
    id: `run-${stored.nonce}`,
    doc_id: start.doc_id || stored.doc_id || "",
    doc_label: docLabel,
    at,
    arch: "aifa_station",
  });
  const filled = (rows as ExtractStreamEvent[]).reduce(
    (m, row) => applyExtractEvent(m, { ...row, type: "metric" } as ExtractStreamEvent),
    base,
  );
  const done = stored.screen?.done;
  if (done) {
    return applyExtractEvent(filled, { ...done, type: "done" } as ExtractStreamEvent);
  }
  // 🔴 ИТОГА НЕТ — И КАРТОЧКА ОБЯЗАНА ЭТО СКАЗАТЬ. Прогон оборвался, строки
  // приехать успели. Пометить его «готово» значило бы соврать дважды: роли и
  // раскладки у него нет (их собирает терминал), а неполнота набора выглядела
  // бы результатом извлечения, а не обрывом. Строки при этом остаются на
  // экране: 19.08 по Банку «Санкт-Петербург» их было 133 из 133.
  return applyExtractEvent(filled, {
    type: "error",
    message: stored.error || "прогон не завершился — показано то, что успело приехать",
  } as ExtractStreamEvent);
}

export function applyExtractEvent(
  msg: ExtractionMessage,
  ev: ExtractStreamEvent,
): ExtractionMessage {
  msg = { ...msg, event_count: (msg.event_count ?? 0) + 1 };
  if (ev.type === "start") {
    return {
      ...msg,
      meta: {
        total_metrics: ev.total_metrics,
        total_extractable: ev.total_extractable,
        company_type: ev.company_type,
        unit: ev.unit,
        period_current: ev.period_current,
        period_prev: ev.period_prev,
        periods: ev.periods,
        company_name: ev.company_name,
        role: ev.role ?? msg.meta?.role,
        role_label: ev.role_label ?? msg.meta?.role_label,
        role_format: ev.role_format ?? msg.meta?.role_format,
      },
      extraction_role: ev.role ?? msg.extraction_role,
      role_label: ev.role_label ?? msg.role_label,
      role_format: ev.role_format ?? msg.role_format,
      table_role: msg.table_role ?? ev.role ?? msg.extraction_role,
      table_role_label: msg.table_role_label ?? ev.role_label ?? msg.role_label,
      table_role_format: msg.table_role_format ?? ev.role_format ?? msg.role_format,
      narrow_order:
        (ev as { narrow_order?: boolean }).narrow_order ?? msg.narrow_order,
    };
  }
  if (ev.type === "progress") {
    return { ...msg, current: { enum: ev.enum, name_ru: ev.name_ru } };
  }
  if (ev.type === "metric") {
    return { ...msg, metrics: [...msg.metrics, ev], current: undefined };
  }
  if (ev.type === "done") {
    return {
      ...msg,
      status: "done",
      current: undefined,
      meta: msg.meta && {
        ...msg.meta,
        tokens_in: ev.tokens_in ?? msg.meta.tokens_in,
        tokens_out: ev.tokens_out ?? msg.meta.tokens_out,
        tokens_cached: ev.tokens_cached ?? msg.meta.tokens_cached,
        cost_usd: ev.cost_usd ?? msg.meta.cost_usd,
        periods: ev.periods ?? msg.meta.periods,
        period_current: ev.period_current ?? msg.meta.period_current,
        period_prev: ev.period_prev ?? msg.meta.period_prev,
        unit: ev.unit ?? msg.meta.unit,
        company_type: ev.company_type ?? msg.meta.company_type,
        company_name: ev.company_name ?? msg.meta.company_name,
        role: ev.role ?? msg.meta.role,
        role_label: ev.role_label ?? msg.meta.role_label,
        role_format: ev.role_format ?? msg.meta.role_format,
      },
      extraction_role: ev.role ?? msg.extraction_role,
      role_label: ev.role_label ?? msg.role_label,
      role_format: ev.role_format ?? msg.role_format,
      table_role: msg.table_role ?? ev.role ?? msg.extraction_role,
      table_role_label: msg.table_role_label ?? ev.role_label ?? msg.role_label,
      table_role_format: msg.table_role_format ?? ev.role_format ?? msg.role_format,
      // #141: белый список раскладки роли приезжал в событии, но редьюсер его
      // не переносил — фильтр таблицы включался только после РУЧНОЙ смены
      // раскладки. Приоритет `msg.` обязателен: пользователь мог сменить
      // раскладку до прихода done, и затирать его выбор нельзя.
      table_output_enums: msg.table_output_enums ?? ev.table_output_enums ?? undefined,
      role_not_applied: ev.role_not_applied ?? msg.role_not_applied,
      role_notice: ev.role_notice ?? msg.role_notice,
      // ⚠️ НЕ ЗАСЕВАТЬ КОПИЕЙ (11.08.2026). Здесь стояло
      // `msg.table_formatted ?? ev.formatted ?? msg.formatted`, и на done поле
      // получало ПОБАЙТОВУЮ копию formatted: в чате владельца это 35 368 байт
      // дважды в одной карточке — 34 КБ из 150 КБ на каждый прогон, при пределе
      // localStorage в единицы мегабайт (замер 11.08 по серверному блобу).
      //
      // table_formatted означает «пользователь СМЕНИЛ раскладку» и больше
      // ничего: его ставил только обработчик смены роли старого экрана
      // (page.tsx, снесён в Р20; у /v2 смена роли живёт в extracted-pane и
      // кладёт поле сама). Читатели берут `table_formatted ?? formatted`,
      // поэтому отсутствие поля = раскладку не меняли, и это ровно та же
      // таблица, а не потеря.
      table_formatted: msg.table_formatted,
      formatted: ev.formatted ?? msg.formatted,
    };
  }
  if (ev.type === "error") {
    return { ...msg, status: "error", error: ev.message, current: undefined };
  }
  return msg;
}

/** Apply one streaming chat/run event to an assistant TextMessage. Shared by
 * the chat send loop and the scenario-run-as-chat consumer. Pure reducer —
 * message-level fields only (content / thinking / toolCalls); the call site
 * handles project-level effects (session_id, usage, done). */
/** Сброс живых полей ответа перед РЕПЛЕЕМ журнала.
 *
 * 🔴 ДОКАЗАННЫЙ ДЕФЕКТ (31.08.2026, прогон владельца). Сброс жил инлайном в
 * `use-chat-run` и забывал `beats` и `segBreak`. Обрыв связи → реплей журнала
 * заново прогоняет ВСЕ события — и шаги дописывались к уже существующим:
 * «Список источников, Классификация отчёта» × 2 на экране, при том что модель
 * звала каждый по разу. Чем больше обрывов, тем длиннее ложная лента.
 *
 * Правило одно: реплей строит сообщение С НУЛЯ, значит сбрасывается ВСЁ, что
 * события наращивают. Поле забыто здесь — оно задвоится при первом же обрыве.
 */
export function resetForReplay(m: TextMessage): TextMessage {
  return {
    ...m,
    content: "",
    thinking: undefined,
    toolCalls: [],
    beats: [],
    segBreak: false,
    plan: undefined,
    plans: undefined,
    question: undefined,
    files: [],
    analysisCard: undefined,
    typedResults: [],
    nodeRuns: {},
    planGraph: undefined,
    planGraphHash: undefined,
    planGraphs: undefined,
    dynamicTrace: undefined,
    typedErrors: [],
  };
}

export function applyChatEvent(msg: TextMessage, ev: ScenarioRunEvent): TextMessage {
  switch (ev.type) {
    case "thinking":
    case "progress":
      return { ...msg, thinking: ("text" in ev && ev.text) || msg.thinking };
    case "tool_progress":
      // Строка состояния — и ТОЛЬКО она. Демоция контента отсюда снята
      // (31.08.2026): прогресс станции приходит другим каналом, чем текст, и
      // резал предложение модели посередине («…займёт несколько | период.»),
      // а финальный ответ уезжал в заметки. Границы текста теперь ставит
      // `text_segment_end` — он идёт В ПОТОКЕ и подтверждён живым логом.
      return { ...msg, thinking: ev.text };
    case "plan": {
      const src = ev.name || "ход";
      const lanes = msg.plans ?? [];
      const idx = lanes.findIndex((l) => l.name === src);
      // Идемпотентность: план на те же шаги приходит ДВАЖДЫ (инлайн-пуш тула и
      // релей той же декларации из станции). Слепая замена сбрасывала уже
      // проставленные статусы в «в очереди», пока работа шла полным ходом.
      const prev = idx >= 0 ? lanes[idx].steps : msg.plan;
      const same =
        prev && prev.length === ev.steps.length && prev.every((st, i) => st.title === ev.steps[i]);
      const steps = same
        ? prev!
        : ev.steps.map((t) => ({ title: t, status: "pending" as const }));
      const plans =
        idx >= 0
          ? lanes.map((l, i) => (i === idx ? { ...l, steps } : l))
          : [...lanes, { name: src, steps }];
      // msg.plan остаётся зеркалом ПЕРВОЙ линии — обратная совместимость со
      // старым экраном и сохранёнными ходами.
      return { ...msg, plans, plan: plans[0]?.steps ?? msg.plan };
    }
    case "plan_step": {
      const src = ev.name || "ход";
      const lanes = msg.plans ?? (msg.plan ? [{ name: src, steps: msg.plan }] : []);
      // Отметка адресована СВОЕЙ линии; для легаси-событий без имени — первой.
      const li = Math.max(0, lanes.findIndex((l) => l.name === src));
      // Индекс < 0 = «последний шаг». Состав плана знает только его автор
      // (у банка на шаг больше, чем у нефинансовой), и закрыть финальный шаг
      // из другого места можно лишь так — иначе пришлось бы дублировать
      // логику состава плана на бэкенде и ловить рассинхрон.
      const target = ev.index < 0 ? (lanes[li]?.steps.length ?? 0) - 1 : ev.index;
      if (!lanes[li]?.steps[target]) return msg;
      const plans = lanes.map((l, i) =>
        i !== li
          ? l
          : {
              ...l,
              steps: l.steps.map((s, j) => {
                if (j !== target) return s;
                const now = Date.now();
                return {
                  ...s,
                  status: ev.status,
                  note: ev.note ?? s.note,
                  // Начало ставим один раз: повторное `running` (переотметка,
                  // догон потока после разрыва) не должно обнулять отсчёт.
                  startedAt: ev.status === "running" ? (s.startedAt ?? now) : s.startedAt,
                  endedAt: ev.status === "running" ? undefined : now,
                };
              }),
            },
      );
      return { ...msg, plans, plan: plans[0]?.steps ?? msg.plan };
    }
    case "text_segment_end":
      // 🔴 КОНЕЦ ITEM ≠ ГРАНИЦА «ПРОЗА → ОТВЕТ» (живой прогон f0494f77,
      // 31.08.2026). Модель закрыла message-item ПОСРЕДИ фразы: «…с
      // округлением до» / segment_end / «копеек).» — и демоция по этому
      // событию оторвала начало ответа в заметку, ответом остался хвост.
      // А в соседнем прогоне (9d2dad93) segment_end не пришёл НИ РАЗУ за
      // 117 событий — на прокси-тулах модель item вообще не закрывает.
      // Событие ненадёжно с обеих сторон; текст оно больше не режет.
      // Прозу от ответа отделяет ФАКТ РАБОТЫ — ветка `tool_call`.
      return msg;
    case "text_delta": {
      // 🔴 ХВОСТ ОБОРВАННОЙ ФРАЗЫ (прогоны 9d2dad93/f5a8cd62, 31.08–01.09).
      // Модель рвёт фразу вокруг инструмента и присылает хвост СКЛЕЕННЫМ с
      // началом новой в одной дельте: «…смотрю формат входа для» + тул +
      // «среднимОба коэффициента…». После демоции хвост «средним» становился
      // началом ОТВЕТА. Стык «строчная кириллица → заглавная» в настоящем
      // тексте невозможен — по нему хвост детерминированно доклеивается в
      // последнюю заметку ленты, а контент начинается с настоящего начала.
      if (!msg.content && ev.text && /^[а-яё]/.test(ev.text)) {
        const beats = msg.beats ?? [];
        const li = beats.map((b) => b.kind).lastIndexOf("note");
        if (li >= 0) {
          const m = ev.text.match(/^[а-яё][^А-ЯЁ]*/);
          const хвост = (m?.[0] ?? ev.text).trimEnd();
          const остаток = ev.text.slice(m?.[0]?.length ?? ev.text.length);
          const note = beats[li] as { kind: "note"; text: string };
          const patched = beats.map((b, i) =>
            i === li ? { ...note, text: `${note.text}${note.text.endsWith(" ") ? "" : " "}${хвост}` } : b,
          );
          return { ...msg, beats: patched, content: остаток, thinking: undefined };
        }
      }
      return { ...msg, content: msg.content + ev.text, thinking: undefined };
    }
    case "clarify":
      return {
        ...msg,
        question: { prompt: ev.question, options: ev.options },
        thinking: undefined,
      };
    case "file":
      return {
        ...msg,
        files: [
          ...(msg.files ?? []),
          { file_id: ev.file_id, filename: ev.filename, kind: ev.kind },
        ],
      };
    case "analysis_card":
      return { ...msg, analysisCard: ev.card };
    case "typed_result": {
      const resultId = typedResultId(ev.result);
      const prior = msg.typedResults ?? [];
      return {
        ...msg,
        typedResults: [...prior.filter((value) => {
          const id = typedResultId(value);
          return id !== resultId;
        }), ev.result],
        thinking: undefined,
      };
    }
    case "node_run": {
      const run = ev.node_run;
      const key = `${run.node_key.atom_name}|${run.node_key.atom_version}|${run.node_key.args_fingerprint}`;
      const known = msg.planGraphs ?? executionGraphsOf(msg);
      const planGraphs = applyExecutionNodeRun(known, ev.plan_graph_hash, run);
      // NodeRun без своего plan_graph (графа ещё нет или он вытеснен потолком) не пропадает:
      // лента чата строит шаги по nodeRuns, пока граф не пришёл.
      const orphan = planGraphs === known;
      return {
        ...msg,
        planGraphs: orphan ? msg.planGraphs : planGraphs,
        nodeRuns: orphan ? { ...(msg.nodeRuns ?? {}), [key]: run } : msg.nodeRuns,
        planGraphHash: ev.plan_graph_hash,
      };
    }
    case "plan_graph": {
      // Топология фиксирована один раз (Срез 29, Этап 7) — реплей/переподключение
      // читают её отсюда же, статусы узлов обновляет case "node_run" выше.
      const graph: PlanGraphContract = {
        graph_hash: ev.graph_hash, planner_version: ev.planner_version, root: ev.root,
        nodes: ev.nodes, scenarios: ev.scenarios,
      };
      const planGraphs = addExecutionGraph(msg.planGraphs, graph, msg.dynamicTrace, Date.now());
      return { ...msg, planGraphs, planGraphHash: latestExecutionGraph(planGraphs)!.graph.graph_hash };
    }
    case "dynamic_move": {
      const dynamicTrace = applyDynamicMove(msg.dynamicTrace, ev);
      const planGraphs = ev.status === "done"
        ? closeExecutionMove(msg.planGraphs, ev.move, "succeeded", Date.now())
        : msg.planGraphs;
      return { ...msg, dynamicTrace, planGraphs };
    }
    case "dynamic_plan":
      return { ...msg, dynamicTrace: applyDynamicPlan(msg.dynamicTrace, ev) };
    case "author_attempt":
      return { ...msg, dynamicTrace: applyAuthorAttempt(msg.dynamicTrace, ev) };
    case "typed_error":
      return {
        ...msg,
        planGraphs: closeAllExecutionGraphs(
          msg.planGraphs ?? executionGraphsOf(msg), "failed", Date.now(),
        ),
        typedErrors: [...(msg.typedErrors ?? []), ev.error],
        typedResults: [
          ...(msg.typedResults ?? []),
          ...ev.partial_results.filter((candidate) => {
            const id = typedResultId(candidate);
            return !(msg.typedResults ?? []).some((value) =>
              typedResultId(value) === id,
            );
          }),
        ],
        thinking: undefined,
      };
    case "tool_call": {
      const name = ev.name;
      const lastArgs = ev.args_summary || "";
      const existing = msg.toolCalls ?? [];
      const idx = existing.findIndex((t) => t.name === name);
      const toolCalls =
        idx >= 0
          ? existing.map((t, i) =>
              i === idx
                ? { ...t, count: t.count + 1, status: "running" as const, lastArgs: lastArgs || t.lastArgs }
                : t,
            )
          : [...existing, { name, count: 1, status: "running" as const, lastArgs }];
      // Хроника — ЕДИНАЯ для прокси и инлайна (31.08.2026, прогон 9d2dad93):
      // всё, что агент написал ДО вызова инструмента, — проза о ходе работы,
      // она уходит заметкой в ленту и вырезается из ответа. Текст после
      // ПОСЛЕДНЕГО инструмента никто не забирает — он и есть ответ.
      // Прокси-ветка «без демоции» (надежда на text_segment_end) снята:
      // segment_end на прокси-тулах не приходит вовсе, и вся проза оставалась
      // в ответе. Стык, разорванный самой моделью («…и запущу » + тул +
      // «расчёт.…»), рвётся некрасиво в ЗАМЕТКЕ — это цена, ответ важнее.
      const note = msg.content.trim();
      const beats: Beat[] = [
        ...(msg.beats ?? []),
        ...(note ? [{ kind: "note" as const, text: note }] : []),
        { kind: "tool" as const, name, status: "running" as const },
      ];
      // Строка состояния идёт за РЕАЛЬНЫМ занятием агента: взялся за инструмент
      // — так и пишем. Без этого пользователь всё время видел «Думаю…», хотя
      // агент уже распознавал документ или считал показатели.
      return { ...msg, toolCalls, beats, content: "", thinking: toolLabel(name).doing };
    }
    case "tool_output": {
      const name = ev.name;
      const existing = msg.toolCalls ?? [];
      // В хронике закрываем ПОСЛЕДНИЙ такт с этим инструментом: один и тот же
      // инструмент за ход могут взять дважды, и закрывать надо свежий.
      const beats = [...(msg.beats ?? [])];
      for (let i = beats.length - 1; i >= 0; i--) {
        const b = beats[i];
        if (b.kind === "tool" && b.name === name && b.status === "running") {
          beats[i] = {
            ...b,
            status: ev.ok ? "done" : "error",
            ...(ev.reason ? { reason: ev.reason } : {}),
          };
          break;
        }
      }
      return {
        ...msg,
        toolCalls: existing.map((t) =>
          t.name === name ? { ...t, status: ev.ok ? ("done" as const) : ("error" as const) } : t,
        ),
        beats,
        // Инструмент отработал — пока агент решает, что дальше, честно пишем
        // именно это, а не название уже завершённого шага.
        thinking: msg.content ? msg.thinking : "Собираю ответ",
      };
    }
    case "error":
      return {
        ...msg,
        planGraphs: closeAllExecutionGraphs(
          msg.planGraphs ?? executionGraphsOf(msg), "failed", Date.now(),
        ),
        content: msg.content + (msg.content ? "\n\n" : "") + `⚠️ ${ev.error}`,
        thinking: undefined,
      };
    case "done": {
      // Terminal status closes the frontend projection without forging backend
      // NodeRun transitions: planned nodes become «не выполнялся», a running
      // node becomes «не завершён», and the immutable executor status remains.
      const status = (ev.status ?? "succeeded") as GraphTerminalStatus;
      const planGraphs = closeAllExecutionGraphs(
        msg.planGraphs ?? executionGraphsOf(msg), status, Date.now(),
      );
      return { ...msg, planGraphs, thinking: undefined };
    }
    default:
      // started / artifact / session_id / usage / sources — no message change.
      return msg;
  }
}
