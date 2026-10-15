import type { Project, Folder } from "@/lib/projects";

const DIRECT_API =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

/** Метка сборки в заголовке пуша. Сервер журналирует пуши, потерявшие чаты, —
 * пуш БЕЗ метки означает вкладку со старым бандлом (главный подозреваемый в
 * пропажах).
 *
 * ⚠️ Здесь была РУЧНАЯ строка «2026-07-28-server-truth», и она врала: не
 * менялась две недели и полсотни выкаток. 11.08 по журналу нельзя было
 * отличить «в браузере старый бандл» от «новый бандл, но код не сработал» —
 * разбор упёрся ровно в это. Теперь уезжает отпечаток сборки (git sha,
 * ARG NEXT_PUBLIC_BUILD_ID), тот же, что сверяет полоса «версия устарела». */
const BUILD_TAG = `${process.env.NEXT_PUBLIC_BUILD_ID ?? "dev"}/sync-2`;

export type ServerDialogState = {
  projects: Project[];
  folders?: Folder[];
  last_project_id: string | null;
  saved_at?: string | null;
  /** Явно удалённые чаты (надгробия клиента) — только так сервер удаляет. */
  deleted_project_ids?: string[];
  /**
   * Надгробия, как их ХРАНИТ сервер: id → время удаления (сек), срок жизни
   * 30 дней. Приходят в GET; клиент обязан их применить, иначе удаление,
   * сделанное на другом устройстве или на сервере, до браузера не доедет.
   */
  deleted?: Record<string, number>;
};

/**
 * Backend keeps one JSON blob per user containing all of their projects
 * (whatever localStorage holds today). On login we GET to bring chat
 * history across browsers / devices; on every change we debounce-PUT
 * back. Errors are silent — localStorage stays the canonical source on
 * the client and the server is "best-effort durable backup".
 */
export async function fetchDialogs(
  signal?: AbortSignal,
): Promise<ServerDialogState | null> {
  try {
    const res = await fetch(`${DIRECT_API}/dialogs`, {
      credentials: "include",
      cache: "no-store",
      signal,
    });
    if (!res.ok) return null;
    return (await res.json()) as ServerDialogState;
  } catch {
    return null;
  }
}

let _saveTimer: ReturnType<typeof setTimeout> | null = null;
let _lastSerialised = "";

/**
 * Debounced PUT. Multiple rapid mutations (typing into a message,
 * stream events arriving every 100 ms) collapse to one network write.
 *
 * Skips the PUT if the serialised payload is byte-identical to the
 * last one we sent — important when re-render hooks fire without
 * actually changing state.
 */
export function syncDialogsDebounced(
  state: {
    projects: Project[];
    folders?: Folder[];
    last_project_id: string | null;
    deleted_project_ids?: string[];
  },
  delayMs: number = 1500,
): void {
  if (typeof window === "undefined") return;
  // Drop the streaming "thinking" indicator before persisting — it's a
  // transient UI hint, not part of the saved message. Otherwise every
  // delta would dirty the payload and trigger a PUT.
  const sanitised = {
    projects: state.projects.map((p) => ({
      ...p,
      messages: p.messages.map((m) => {
        if (m.role !== "assistant") return m;
        const { thinking: _t, ...rest } = m as { thinking?: unknown } & typeof m;
        return rest;
      }),
    })),
    folders: state.folders ?? [],
    last_project_id: state.last_project_id,
    deleted_project_ids: state.deleted_project_ids ?? [],
  };
  const payload = JSON.stringify(sanitised);
  if (payload === _lastSerialised) return;
  _lastSerialised = payload;

  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    fetch(`${DIRECT_API}/dialogs`, {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-AIFA-Build": BUILD_TAG },
      body: payload,
    }).catch(() => {
      /* swallow — localStorage remains the source of truth */
    });
  }, delayMs);
}

/**
 * Force a flush — used when the page is about to unload and we don't
 * want to lose the last 1.5 s of typing. `keepalive` lets the request
 * outlive the page.
 */
export function flushDialogsNow(state: {
  projects: Project[];
  folders?: Folder[];
  last_project_id: string | null;
  deleted_project_ids?: string[];
}): void {
  if (typeof window === "undefined") return;
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = null;
  const payload = JSON.stringify({
    projects: state.projects,
    folders: state.folders ?? [],
    last_project_id: state.last_project_id,
    deleted_project_ids: state.deleted_project_ids ?? [],
  });
  if (payload === _lastSerialised) return;
  _lastSerialised = payload;
  try {
    fetch(`${DIRECT_API}/dialogs`, {
      method: "PUT",
      credentials: "include",
      headers: { "Content-Type": "application/json", "X-AIFA-Build": BUILD_TAG },
      body: payload,
      keepalive: true,
    }).catch(() => {});
  } catch {
    /* navigator may already be detached */
  }
}
