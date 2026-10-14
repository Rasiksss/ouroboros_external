/**
 * Лента гибридного планировщика: ходы planner и попытки автора кода.
 *
 * Источник данных — два события backend (`dynamic_move`, `author_attempt`), которые цикл отдаёт
 * в тот же поток, что `plan_graph`/`node_run`. Здесь только чистые функции слияния: компонент
 * лишь рисует. Оба слияния идемпотентны — реплей журнала не дублирует карточки.
 */

export type TraceNewAtom = {
  name: string;
  kind: "download" | "code";
  purpose: string;
  output_type: string;
  url: string | null;
};

// Часть разобранной цели, исполняемая по ссылке `goal_ref`; описание читаемое («Рассчитать EBITDA (все периоды)»).
export type TraceGoal = { goal_ref: string; description: string };

export type TraceStep = { atom: string | null; concept: string | null; period_ref: string | null };

export type MoveStatus = "running" | "done" | "rejected";
// "entry" — не ход планировщика, а причина входа в цикл (номер 0): стандартный граф собрался и упал.
export type MoveAction = "execute" | "finalize" | "give_up" | "rejected" | "entry";
export type AttemptStatus = "ok" | "error" | "bad_format" | "no_response" | "station_fault";

export type DynamicMoveEvent = {
  type: "dynamic_move";
  move: number;
  status: MoveStatus;
  action: MoveAction;
  objective: string;
  reason: string;
  scenarios: string[];
  // Записи журнала до части 18 поля не имеют: читается как «частей нет».
  goals?: TraceGoal[];
  new_atoms: TraceNewAtom[];
  steps: TraceStep[];
  outcome: string;
  details: string;
};

export type AuthorAttemptEvent = {
  type: "author_attempt";
  move: number;
  atom: string;
  attempt: number;
  status: AttemptStatus;
  error: string | null;
  code: string;
};

export type AuthorAttempt = {
  atom: string;
  attempt: number;
  status: AttemptStatus;
  error: string | null;
  code: string;
};

export type TraceMove = {
  move: number;
  status: MoveStatus;
  action: MoveAction;
  objective: string;
  reason: string;
  scenarios: string[];
  goals: TraceGoal[];
  newAtoms: TraceNewAtom[];
  steps: TraceStep[];
  outcome: string;
  details: string;
  attempts: AuthorAttempt[];
};

// План цикла целиком: приходит до первого хода (фаза планирования) и заново, если планировщик его переписал.
export type DynamicPlanEvent = { type: "dynamic_plan"; plan: string[]; files: string[] };

export type DynamicTrace = { moves: TraceMove[]; plan?: string[]; files?: string[] };

export type TraceSummary = {
  moves: number;
  rejected: number;
  authorAttempts: number;
  state: "empty" | "running" | "finished";
};

function emptyMove(move: number): TraceMove {
  return {
    move,
    status: "running",
    action: "execute",
    objective: "",
    reason: "",
    scenarios: [],
    goals: [],
    newAtoms: [],
    steps: [],
    outcome: "",
    details: "",
    attempts: [],
  };
}

function withMove(trace: DynamicTrace | undefined, next: TraceMove): DynamicTrace {
  const rest = (trace?.moves ?? []).filter((item) => item.move !== next.move);
  return { ...trace, moves: [...rest, next].sort((a, b) => a.move - b.move) };
}

export function applyDynamicPlan(trace: DynamicTrace | undefined, event: DynamicPlanEvent): DynamicTrace {
  return { moves: trace?.moves ?? [], plan: event.plan, files: event.files };
}

export function applyDynamicMove(trace: DynamicTrace | undefined, event: DynamicMoveEvent): DynamicTrace {
  const current = trace?.moves.find((item) => item.move === event.move) ?? emptyMove(event.move);
  return withMove(trace, {
    ...current,
    status: event.status,
    action: event.action,
    objective: event.objective,
    reason: event.reason,
    scenarios: event.scenarios,
    goals: event.goals ?? [],
    newAtoms: event.new_atoms,
    steps: event.steps,
    outcome: event.outcome,
    details: event.details,
  });
}

export function applyAuthorAttempt(trace: DynamicTrace | undefined, event: AuthorAttemptEvent): DynamicTrace {
  const current = trace?.moves.find((item) => item.move === event.move) ?? emptyMove(event.move);
  const attempt: AuthorAttempt = {
    atom: event.atom,
    attempt: event.attempt,
    status: event.status,
    error: event.error,
    code: event.code,
  };
  const rest = current.attempts.filter((item) => !(item.atom === event.atom && item.attempt === event.attempt));
  const attempts = [...rest, attempt].sort((a, b) => a.attempt - b.attempt);
  return withMove(trace, { ...current, attempts });
}

export function summarizeTrace(trace: DynamicTrace | undefined): TraceSummary {
  const moves = trace?.moves ?? [];
  if (!moves.length) {
    return { moves: 0, rejected: 0, authorAttempts: 0, state: trace?.plan?.length ? "running" : "empty" };
  }
  const last = moves[moves.length - 1];
  const terminal = last.status === "done" && (last.action === "finalize" || last.action === "give_up");
  return {
    moves: moves.filter((item) => item.action !== "entry").length,
    rejected: moves.filter((item) => item.status === "rejected").length,
    authorAttempts: moves.reduce((sum, item) => sum + item.attempts.length, 0),
    state: terminal ? "finished" : "running",
  };
}
