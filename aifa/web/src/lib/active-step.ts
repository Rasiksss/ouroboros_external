import type { PlanStep } from "@/lib/projects";

/**
 * Где ход находится ПРЯМО СЕЙЧАС — один шаг плана.
 *
 * ЖАЛОБА (Nate, 19.08, п.5): «план показал красный шаг и ЗАМЕР: не видно
 * текущего шага и того, как агент исправляется». Разбор: строка «где мы»
 * искала шаг `running`, а не нашла — `pending`. Упавший шаг не бывает ни тем,
 * ни другим, и когда падает последний, строка исчезала совсем — экран
 * замирал на красной точке без единого слова о том, что происходит дальше.
 *
 * Порядок выбора: идущий шаг → следующий в очереди → последний упавший.
 * Последний, а не первый: агент мог споткнуться дважды, и человеку нужно то
 * место, где он стоит, а не то, где спотыкался в начале.
 */
export type ActiveStep = { index: number; title: string; status: PlanStep["status"] };

export function activeStepOf(plan: PlanStep[] | null | undefined): ActiveStep | null {
  const steps = plan ?? [];
  const running = steps.findIndex((s) => s.status === "running");
  const queued = steps.findIndex((s) => s.status === "pending");
  const failed = steps.reduce((last, s, i) => (s.status === "error" ? i : last), -1);
  const idx = running >= 0 ? running : queued >= 0 ? queued : failed;
  if (idx < 0) return null;
  return { index: idx, title: steps[idx].title, status: steps[idx].status };
}
