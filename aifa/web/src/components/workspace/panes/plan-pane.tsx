"use client";

import { useMemo } from "react";
import { latestRootPlanRevisions } from "@/lib/root-plan";
import type { Project } from "@/lib/projects";
import { RootPlanView } from "./root-plan-view";

/**
 * Колонка «План» на месте колонки исходного документа (решение владельца 23.10.2026): план, который
 * предложил планировщик, виден весь ход, а не только пока рабочая область не сменилась лентой
 * «Планировщик и автор». Ревизии и раскрытие шагов — в `RootPlanView`.
 */
export function PlanPane({ project }: { project: Project | null }) {
  const revisions = useMemo(() => latestRootPlanRevisions(project?.messages ?? []), [project]);
  return (
    <div className="h-full overflow-y-auto">
      <RootPlanView
        revisions={revisions}
        defaultOpen
        emptyText="План появится, когда планировщик его предложит."
      />
    </div>
  );
}
