/**
 * План, который предложил автор плана (событие `root_plan`, ADR-0074).
 *
 * Служба видит только предложения: принял ли предложение корень, знает ouroboros. Поэтому каждое
 * событие — ревизия хода, последняя — текущая, а прежние на экране помечены «заменён». Статусов
 * шагов здесь нет: их не из чего честно вывести.
 */

export type RootPlanStep = {
  id: string;
  objective: string;
  depends_on: string[];
  execution: string;
  outputs: string[];
  criteria: { id: string; description: string; verification: string }[];
};

export type RootPlanEvent = {
  type: "root_plan";
  task_id: string;
  requirements: { id: string; description: string; source: string }[];
  assumptions: { id: string; description: string; reason: string }[];
  questions: string[];
  steps: RootPlanStep[];
};

/** Что хранит сообщение: событие без `type`, чтобы не путать с потоком. */
export type RootPlanProposal = Omit<RootPlanEvent, "type">;

export type RootPlanRevision = RootPlanProposal & {
  taskId: string;
  number: number;
  current: boolean;
};

/** Добавить предложение; то же предложение (повтор журнала) второй раз не добавляется. */
export function addRootPlan(
  proposals: RootPlanProposal[] | undefined,
  event: RootPlanEvent,
): RootPlanProposal[] {
  const known = proposals ?? [];
  if (known.some((p) => p.task_id === event.task_id)) return known;
  return [
    ...known,
    {
      task_id: event.task_id,
      requirements: event.requirements,
      assumptions: event.assumptions,
      questions: event.questions,
      steps: event.steps,
    },
  ];
}

export function rootPlanRevisions(msg: { rootPlans?: RootPlanProposal[] }): RootPlanRevision[] {
  const proposals = msg.rootPlans ?? [];
  return proposals.map((p, i) => ({
    ...p,
    taskId: p.task_id,
    number: i + 1,
    current: i === proposals.length - 1,
  }));
}

/** Ревизии последнего хода, где автор плана что-то предложил. */
export function latestRootPlanRevisions(
  messages: readonly { role: string; rootPlans?: RootPlanProposal[] }[],
): RootPlanRevision[] {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role === "assistant" && m.rootPlans?.length) return rootPlanRevisions(m);
  }
  return [];
}
