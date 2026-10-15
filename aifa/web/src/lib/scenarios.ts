// Client for the Scenarios («Сценарии») API — user-defined scheduled tasks.
// Uses DIRECT_API (= "/api" in the deployed build, localhost:8000 in dev) +
// credentials:"include", matching the convention in components/ir-actions.tsx.

const DIRECT_API =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export type ScenarioKind = "ir_calendar" | "ir_daily" | "agent_prompt";
export type ScenarioFreq = "manual" | "hourly" | "daily" | "weekly";
export type ScenarioStatus = "" | "ok" | "error" | "running";

export type Schedule = {
  freq: ScenarioFreq;
  /** "HH:MM" in Europe/Moscow. */
  at_time: string;
  /** Weekdays for freq="weekly": 0=Пн … 6=Вс. */
  dows: number[];
};

export type Scenario = {
  id: string;
  user_id: number;
  name: string;
  kind: ScenarioKind;
  instructions: string;
  description: string;
  project_id: string;
  model: string;
  schedule: Schedule;
  /** JSON-настройки запуска per-kind (#87): календарь — back_days/fwd_days. */
  params: Record<string, unknown>;
  paused: boolean;
  created_at: number; // unix seconds
  last_run_at: number | null;
  last_status: ScenarioStatus;
  last_run_id: string;
  last_error: string;
};

/** A saved run output (artifact) under a scenario's id namespace. Same shape
 * as ArtifactRef in lib/projects.ts. */
export type ScenarioRunRef = {
  run_id: string;
  kind: string;
  created_at: number;
  title: string;
  summary?: string;
  counts?: Record<string, number>;
};

export type CreateScenarioInput = {
  name: string;
  kind: ScenarioKind;
  instructions?: string;
  description?: string;
  project_id?: string;
  model?: string;
  schedule: Schedule;
  paused?: boolean;
};

export type PatchScenarioInput = {
  name?: string;
  instructions?: string;
  description?: string;
  project_id?: string;
  model?: string;
  schedule?: Schedule;
  paused?: boolean;
  /** Частичный мердж на бэке; null-значение снимает настройку (→ дефолт). */
  params?: Record<string, unknown>;
};

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${DIRECT_API}${path}`, {
    credentials: "include",
    cache: "no-store",
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  return r.json() as Promise<T>;
}

async function jsend<T>(
  method: "POST" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const r = await fetch(`${DIRECT_API}${path}`, {
    method,
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status}: ${await r.text().catch(() => r.statusText)}`);
  return r.json().catch(() => ({})) as Promise<T>;
}

export async function listScenarios(): Promise<Scenario[]> {
  const d = await jget<{ scenarios: Scenario[] }>("/scenarios");
  return d.scenarios ?? [];
}

export function getScenario(id: string): Promise<Scenario> {
  return jget<Scenario>(`/scenarios/${encodeURIComponent(id)}`);
}

export function createScenario(input: CreateScenarioInput): Promise<Scenario> {
  return jsend<Scenario>("POST", "/scenarios", input);
}

export function patchScenario(id: string, input: PatchScenarioInput): Promise<Scenario> {
  return jsend<Scenario>("PATCH", `/scenarios/${encodeURIComponent(id)}`, input);
}

export function deleteScenario(id: string): Promise<unknown> {
  return jsend("DELETE", `/scenarios/${encodeURIComponent(id)}`);
}

export function runScenarioNow(id: string): Promise<{ run_id: string }> {
  return jsend<{ run_id: string }>("POST", `/scenarios/${encodeURIComponent(id)}/run`);
}

export async function listScenarioRuns(id: string): Promise<ScenarioRunRef[]> {
  const d = await jget<{ runs: ScenarioRunRef[] }>(`/scenarios/${encodeURIComponent(id)}/runs`);
  return d.runs ?? [];
}

/** SSE URL for live progress of a scenario run (or journal replay). */
export function scenarioRunStreamUrl(id: string, runId: string): string {
  return `${DIRECT_API}/scenarios/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}/stream`;
}

export function scenarioRunUrl(id: string, runId: string): string {
  return `${DIRECT_API}/scenarios/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`;
}

// ─── «Создать с AifClaw» wizard (Phase B2) ───────────────────────────────

export type AssistantTurn = { role: "user" | "assistant"; content: string };

/** A scenario the wizard proposes, ready to prefill the create form. */
export type ScenarioDraft = {
  name: string;
  description: string;
  instructions: string;
  freq: ScenarioFreq;
  at_time: string;
  dows: number[];
};

/** A clarifying question the wizard asks, rendered as selectable option cards. */
export type WizardQuestion = { prompt: string; options: string[] };

export function scenarioAssistant(
  messages: AssistantTurn[],
): Promise<{ reply: string; question: WizardQuestion | null; draft: ScenarioDraft | null }> {
  return jsend<{ reply: string; question: WizardQuestion | null; draft: ScenarioDraft | null }>(
    "POST",
    "/scenarios/assistant",
    { messages },
  );
}

// ─── Presentation helpers (shared by overview + detail) ──────────────────

export const KIND_LABEL: Record<ScenarioKind, string> = {
  ir_calendar: "IR Calendar",
  ir_daily: "Daily News",
  agent_prompt: "Свой промпт",
};

export const DOW_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];

/** Human label for a schedule: «Вручную» / «Ежедневно 18:00» / «По будням
 * 09:00» / «Пн, Ср, Пт 09:00». */
export function scheduleLabel(s: Schedule): string {
  if (s.freq === "manual") return "Вручную";
  if (s.freq === "hourly") return "Каждый час";
  if (s.freq === "daily") return `Ежедневно ${s.at_time}`;
  const dows = [...(s.dows ?? [])].sort((a, b) => a - b);
  if (dows.length === 0) return `Еженедельно ${s.at_time}`;
  if (dows.length === 5 && dows.every((d, i) => d === i)) return `По будням ${s.at_time}`;
  if (dows.length === 7) return `Ежедневно ${s.at_time}`;
  return `${dows.map((d) => DOW_LABELS[d]).join(", ")} ${s.at_time}`;
}
