"use client";

import { memo, useEffect, useMemo, useState } from "react";
import type { PlanGraphContract } from "@/lib/api";
import { atomLabel } from "@/lib/atom-labels";
import {
  atomCategory,
  CATEGORY_LABEL,
  edgeState,
  formatDuration,
  graphHeadline,
  isDynamicPlan,
  NODE_STATUS_LABEL,
  nodeDisplayStatus,
  nodeToken,
  showsProgressBar,
  statusOf,
  summarizeProgress,
  type EdgeState,
  type GraphTerminal,
  type NodeStatus,
  type StoredNodeRun,
} from "@/lib/plan-graph-progress";
import { cn } from "@/lib/utils";

/**
 * Срез 29a §4 → Срез 31 (П7): план исполнения в правой «Рабочей области».
 *
 * План появляется СРАЗУ после события `plan_graph` (бэкенд отдаёт события по
 * мере работы, а не после неё), а каждый узел живьём меняет вид по `node_run`:
 * в очереди → выполняется (спиннер, бегущая полоса) → готово (галочка, мягкая
 * зелёная подсветка) или ошибка. Ребро загорается зелёным, когда завершён его
 * источник, и «течёт», пока приёмник выполняется. Шапка показывает прогресс
 * «n из N», время и признак плана, собранного агентом-планировщиком.
 *
 * Чистая проекция сохранённой пары событий (`plan_graph`/`node_run`):
 * топология фиксирована (`layoutGraph` зависит только от
 * графа), от статусов меняется лишь оформление. Расчёты прогресса — в
 * `lib/plan-graph-progress.ts` (проверяются без DOM). Сторонняя библиотека
 * графов не нужна.
 */

type Size = { w: number; h: number; colGap: number; rowGap: number; compact: boolean };
const FULL: Size = { w: 200, h: 60, colGap: 84, rowGap: 14, compact: false };
const COMPACT: Size = { w: 156, h: 34, colGap: 56, rowGap: 8, compact: true };
/** Полный анализ на реальном отчёте — сотни узлов: карточки становятся компактными. */
const COMPACT_THRESHOLD = 48;
/** Заполнение формы — 500–600 узлов и ~1000 рёбер: включается «лёгкий» режим (без анимаций,
 * стрелок и скрытых тултипов в каждой карточке), иначе интерфейс лагает. */
const LARGE_THRESHOLD = 150;
const PAD = 28;
/** Расстояние между слоями в вертикальной раскладке — место под стрелку-связь. */
const LAYER_GAP_VERTICAL = 30;

type Orientation = "horizontal" | "vertical";

type LayoutNode = {
  token: string;
  node: PlanGraphContract["nodes"][number];
  x: number;
  y: number;
};

export function layoutGraph(
  graph: PlanGraphContract,
): { nodes: LayoutNode[]; width: number; height: number; size: Size; orientation: Orientation } {
  const size = graph.nodes.length > COMPACT_THRESHOLD ? COMPACT : FULL;
  const byToken = new Map(graph.nodes.map((n) => [nodeToken(n.node_key), n]));
  const depth = new Map<string, number>();

  // Слой = длина самой длинной цепочки зависимостей до узла. `visiting` —
  // защита ТОЛЬКО от бага отрисовки: планировщик уже проверяет граф на циклы
  // до исполнения (A1.11), настоящего цикла здесь быть не может.
  function resolve(token: string, visiting: Set<string>): number {
    const cached = depth.get(token);
    if (cached !== undefined) return cached;
    if (visiting.has(token)) return 0;
    const node = byToken.get(token);
    if (!node || node.requires.length === 0) {
      depth.set(token, 0);
      return 0;
    }
    visiting.add(token);
    const d = 1 + Math.max(...node.requires.map((r) => resolve(nodeToken(r), visiting)));
    depth.set(token, d);
    return d;
  }
  for (const n of graph.nodes) resolve(nodeToken(n.node_key), new Set());

  const byLayer = new Map<number, PlanGraphContract["nodes"]>();
  for (const n of graph.nodes) {
    const d = depth.get(nodeToken(n.node_key)) ?? 0;
    (byLayer.get(d) ?? byLayer.set(d, []).get(d)!).push(n);
  }
  const layers = [...byLayer.keys()].sort((a, b) => a - b);
  const maxRows = Math.max(0, ...layers.map((l) => byLayer.get(l)!.length));
  // Ориентация — по числу колонок, не по измерению DOM (работает и при SSR, и в
  // тестах). Правая панель узкая: цепочка из 7–17 узлов «слева направо» уходит за
  // край на ~2000 px, а сверху вниз занимает две-три колонки и листается вниз.
  // Широкий полный анализ (сотни узлов, слоёв мало, строк много) остаётся
  // горизонтальным — в вертикали у него колонок было бы больше.
  const orientation: Orientation = maxRows < layers.length ? "vertical" : "horizontal";
  const nodes: LayoutNode[] = [];
  layers.forEach((layerIdx, colPos) => {
    const layerNodes = byLayer.get(layerIdx)!;
    // Вертикаль: слой с меньшим числом узлов центрируется по самому широкому слою.
    const centering = ((maxRows - layerNodes.length) * (size.w + size.rowGap)) / 2;
    layerNodes.forEach((node, row) => {
      nodes.push({
        token: nodeToken(node.node_key),
        node,
        x:
          orientation === "vertical"
            ? PAD + centering + row * (size.w + size.rowGap)
            : PAD + colPos * (size.w + size.colGap),
        y:
          orientation === "vertical"
            ? PAD + colPos * (size.h + LAYER_GAP_VERTICAL)
            : PAD + row * (size.h + size.rowGap),
      });
    });
  });
  const acrossLayers = (step: number, extent: number) =>
    PAD * 2 + layers.length * extent + Math.max(0, layers.length - 1) * step;
  const withinLayer = (step: number, extent: number) =>
    PAD * 2 + maxRows * extent + Math.max(0, maxRows - 1) * step;
  const width =
    orientation === "vertical" ? withinLayer(size.rowGap, size.w) : acrossLayers(size.colGap, size.w);
  const height =
    orientation === "vertical" ? acrossLayers(LAYER_GAP_VERTICAL, size.h) : withinLayer(size.rowGap, size.h);
  return { nodes, width, height, size, orientation };
}

/** «Сейчас» для живого таймера; тикает только пока идёт работа. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    // Первый тик — через 500 мс; до него `now` может отставать, поэтому время в
    // шапке обрезается снизу нулём (см. `elapsedSeconds`).
    const id = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

function StatusIcon({ status }: { status: NodeStatus }) {
  if (status === "succeeded") {
    return (
      <svg aria-hidden viewBox="0 0 16 16" className="aifa-pg-icon aifa-pg-pop" data-icon="check">
        <circle cx="8" cy="8" r="8" fill="var(--pg-success)" />
        <path d="M4.6 8.3l2.4 2.4 4.6-5" fill="none" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (status === "failed" || status === "interrupted") {
    return (
      <svg aria-hidden viewBox="0 0 16 16" className="aifa-pg-icon aifa-pg-pop" data-icon="cross">
        <circle cx="8" cy="8" r="8" fill="var(--pg-fail)" />
        <path d="M5.4 5.4l5.2 5.2M10.6 5.4l-5.2 5.2" stroke="#fff" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    );
  }
  if (status === "running") return <span aria-hidden className="aifa-pg-icon aifa-pg-spin" data-icon="spinner" />;
  if (status === "blocked" || status === "skipped" || status === "empty" || status === "not_run") {
    return (
      <svg aria-hidden viewBox="0 0 16 16" className="aifa-pg-icon" data-icon="skip">
        <circle cx="8" cy="8" r="7" fill="none" stroke="var(--pg-idle)" strokeWidth="1.4" strokeDasharray="2.5 2.5" />
        <path d="M5 8h6" stroke="var(--pg-idle)" strokeWidth="1.6" strokeLinecap="round" />
      </svg>
    );
  }
  return (
    <svg aria-hidden viewBox="0 0 16 16" className="aifa-pg-icon" data-icon={status === "ready" ? "ready" : "queued"}>
      <circle
        cx="8" cy="8" r="7" fill="none" stroke={status === "ready" ? "var(--pg-run)" : "var(--pg-idle)"}
        strokeWidth="1.5" strokeDasharray={status === "ready" ? undefined : "3 2.5"}
      />
      {status === "ready" && <circle cx="8" cy="8" r="2.4" fill="var(--pg-run)" />}
    </svg>
  );
}

const STYLES = `
.aifa-pg {
  --pg-success: #2f9e5c;
  --pg-run: #3b7de0;
  --pg-fail: var(--destructive);
  --pg-idle: var(--color-neutral-400);
}
:root[data-theme="terminal"] .aifa-pg { --pg-success: #3ecb7e; --pg-run: #5b9dff; }

.aifa-pg-node {
  transition: background-color .35s ease, border-color .35s ease, box-shadow .35s ease, opacity .35s ease;
  background: var(--color-surface);
  border-color: var(--color-divider);
  overflow: hidden;
}
.aifa-pg-node[data-node-status="pending"], .aifa-pg-node[data-node-status="planned"] { opacity: .78; }
.aifa-pg-node[data-node-status="ready"] { border-color: var(--pg-run); }
.aifa-pg-node[data-node-status="running"] {
  border-color: var(--pg-run);
  background: color-mix(in srgb, var(--pg-run) 7%, var(--color-surface));
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--pg-run) 16%, transparent);
}
.aifa-pg-node[data-node-status="succeeded"] {
  border-color: color-mix(in srgb, var(--pg-success) 60%, var(--color-divider));
  background: color-mix(in srgb, var(--pg-success) 10%, var(--color-surface));
}
.aifa-pg-node[data-node-status="failed"] {
  border-color: var(--pg-fail);
  background: color-mix(in srgb, var(--pg-fail) 9%, var(--color-surface));
}
.aifa-pg-node[data-node-status="interrupted"] {
  border-color: var(--pg-fail); border-style: dashed;
  background: color-mix(in srgb, var(--pg-fail) 5%, var(--color-surface));
}
.aifa-pg-node[data-node-status="blocked"], .aifa-pg-node[data-node-status="skipped"] {
  border-style: dashed; opacity: .7;
}
.aifa-pg-node[data-node-status="empty"], .aifa-pg-node[data-node-status="not_run"] {
  border-style: dashed; opacity: .72;
}
.aifa-pg-node[data-node-root] { box-shadow: 0 0 0 1px var(--color-neutral-500) inset; }

.aifa-pg-icon { width: 16px; height: 16px; flex: none; }
.aifa-pg-spin {
  display: inline-block; border-radius: 9999px; box-sizing: border-box;
  border: 2px solid color-mix(in srgb, var(--pg-run) 25%, transparent); border-top-color: var(--pg-run);
}
.aifa-pg-tip {
  opacity: 0; pointer-events: none; transform: translateY(-4px);
  transition: opacity .12s ease, transform .12s ease;
}
.aifa-pg-node:hover .aifa-pg-tip,
.aifa-pg-node:focus-visible .aifa-pg-tip,
.aifa-pg-node:focus-within .aifa-pg-tip { opacity: 1; pointer-events: auto; transform: translateY(0); }
.aifa-pg-node:hover, .aifa-pg-node:focus-within { overflow: visible; z-index: 20; }

.aifa-pg-edge { fill: none; stroke-width: 1.6; transition: stroke .35s ease, opacity .35s ease; }
.aifa-pg-edge[data-edge-state="idle"] { stroke: var(--color-neutral-300); }
.aifa-pg-edge[data-edge-state="done"] { stroke: var(--pg-success); }
.aifa-pg-edge[data-edge-state="active"] { stroke: var(--pg-run); stroke-dasharray: 5 4; }
.aifa-pg-edge[data-edge-state="failed"] { stroke: var(--pg-fail); }

.aifa-pg-bar { background: var(--color-neutral-200); }
.aifa-pg-bar-fill { background: var(--pg-success); transition: width .45s ease; }
.aifa-pg[data-plan-state="failed"] .aifa-pg-bar-fill { background: var(--pg-fail); }

@media (prefers-reduced-motion: no-preference) {
  @keyframes aifa-pg-spin { to { transform: rotate(360deg); } }
  @keyframes aifa-pg-sweep { from { background-position: -60% 0; } to { background-position: 160% 0; } }
  @keyframes aifa-pg-flow { to { stroke-dashoffset: -18; } }
  @keyframes aifa-pg-pop { 0% { transform: scale(.4); opacity: 0; } 60% { transform: scale(1.18); opacity: 1; } 100% { transform: scale(1); } }
  @keyframes aifa-pg-stripe { to { background-position: 28px 0; } }
  .aifa-pg-spin { animation: aifa-pg-spin .8s linear infinite; }
  .aifa-pg-pop { animation: aifa-pg-pop .32s ease-out; }
  .aifa-pg-node[data-node-status="running"]::after {
    content: ""; position: absolute; left: 0; bottom: 0; width: 100%; height: 2px;
    background: linear-gradient(90deg, transparent, var(--pg-run), transparent) no-repeat;
    background-size: 45% 100%; animation: aifa-pg-sweep 1.3s linear infinite;
  }
  .aifa-pg-edge[data-edge-state="active"] { animation: aifa-pg-flow .7s linear infinite; }
  .aifa-pg[data-plan-state="running"] .aifa-pg-bar-fill {
    background-image: linear-gradient(135deg, rgba(255,255,255,.28) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.28) 50%, rgba(255,255,255,.28) 75%, transparent 75%);
    background-size: 28px 28px; animation: aifa-pg-stripe .9s linear infinite;
  }
}

/* Большой план: статус виден по цвету и значку, а не по движению. Специфичность выше правил
   анимаций выше, поэтому порядок в файле не важен. */
.aifa-pg[data-plan-scale="large"] .aifa-pg-node { transition: none; }
.aifa-pg[data-plan-scale="large"] .aifa-pg-node[data-node-status="running"]::after { content: none; animation: none; }
.aifa-pg[data-plan-scale="large"] .aifa-pg-pop { animation: none; }
.aifa-pg[data-plan-scale="large"] .aifa-pg-edge { transition: none; }
.aifa-pg[data-plan-scale="large"] .aifa-pg-edge[data-edge-state="active"] { animation: none; stroke-dasharray: none; }
`;

const MARKERS: [EdgeState, string][] = [
  ["idle", "var(--color-neutral-300)"],
  ["done", "var(--pg-success)"],
  ["active", "var(--pg-run)"],
  ["failed", "var(--pg-fail)"],
];

/**
 * Ребро и карточка — `memo`: на каждое `node_run` и каждый тик таймера перерисовывается родитель,
 * а у 600 узлов это тысячи элементов. Пропсы — примитивы и объекты с неизменной ссылкой
 * (`LayoutNode` из `useMemo`, `NodeRun`, который редьюсер не пересоздаёт, если он не менялся),
 * поэтому перерисовывается только то, у чего поменялся статус.
 */
const GraphEdge = memo(function GraphEdge({
  source, target, state, orientation, size, arrow,
}: {
  source: LayoutNode;
  target: LayoutNode;
  state: EdgeState;
  orientation: Orientation;
  size: Size;
  arrow: boolean;
}) {
  const vertical = orientation === "vertical";
  // Горизонталь: правая грань источника → левая грань цели. Вертикаль:
  // нижняя грань источника → верхняя грань цели.
  const x1 = vertical ? source.x + size.w / 2 : source.x + size.w;
  const y1 = vertical ? source.y + size.h : source.y + size.h / 2;
  const x2 = vertical ? target.x + size.w / 2 : target.x;
  const y2 = vertical ? target.y : target.y + size.h / 2;
  const mid = vertical ? (y1 + y2) / 2 : (x1 + x2) / 2;
  return (
    <path
      className="aifa-pg-edge"
      data-edge-state={state}
      d={
        vertical
          ? `M ${x1} ${y1} C ${x1} ${mid}, ${x2} ${mid}, ${x2} ${y2}`
          : `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`
      }
      markerEnd={arrow ? `url(#aifa-pg-arrow-${state})` : undefined}
    />
  );
});

const GraphNodeCard = memo(function GraphNodeCard({
  ln, run, terminal, size, isRoot, scenarioRef, large,
}: {
  ln: LayoutNode;
  run: StoredNodeRun | undefined;
  terminal: boolean;
  size: Size;
  isRoot: boolean;
  scenarioRef: string | undefined;
  large: boolean;
}) {
  const status: NodeStatus = nodeDisplayStatus(run, terminal);
  const label = atomLabel(ln.node.node_key.atom_name);
  const category = CATEGORY_LABEL[atomCategory(ln.node.node_key.atom_name)];
  const produced = ln.node.produces.join(", ") || ln.node.node_key.atom_name;
  const duration = run?.started_at && run?.finished_at
    ? Math.max(0, (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000)
    : null;
  // В большом плане скрытый тултип в каждой карточке (~7 элементов × сотни узлов) заменён
  // нативным `title`: то же содержание, ноль лишних элементов DOM.
  const title = large
    ? [
        `${label.name}: ${NODE_STATUS_LABEL[status]}${duration !== null ? ` · ${formatDuration(duration)}` : ""}`,
        label.description,
        `Даёт: ${produced}`,
        run?.error,
      ].filter(Boolean).join("\n")
    : undefined;
  return (
    <div
      tabIndex={0}
      aria-label={`${label.name}: ${NODE_STATUS_LABEL[status]}. ${label.description}`}
      title={title}
      data-node-status={status}
      data-node-root={isRoot || undefined}
      className={cn(
        "aifa-pg-node absolute flex items-center gap-2 rounded-lg border px-2.5",
        "text-[11.5px] text-[var(--color-text)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)]",
      )}
      style={{ left: ln.x, top: ln.y, width: size.w, height: size.h }}
    >
      <StatusIcon status={status} />
      <span className="flex min-w-0 flex-1 flex-col justify-center">
        <span className="truncate font-medium leading-tight">{label.name}</span>
        {!size.compact && (
          <span className="truncate text-[10px] leading-tight text-[var(--color-neutral-600)]">
            {category} · {NODE_STATUS_LABEL[status]}
            {duration !== null && ` · ${formatDuration(duration)}`}
          </span>
        )}
      </span>
      {isRoot && (
        <span className="shrink-0 rounded bg-[var(--color-neutral-800)] px-1 py-[1px] text-[9px] uppercase tracking-wide text-[var(--color-bg)]">
          итог
        </span>
      )}
      {scenarioRef && !isRoot && (
        <span className="shrink-0 rounded bg-[var(--color-neutral-100)] px-1 py-[1px] text-[9px] text-[var(--color-neutral-600)]">
          {scenarioRef}
        </span>
      )}
      {size.compact && <span className="sr-only">{NODE_STATUS_LABEL[status]}</span>}

      {!large && (
        <div
          role="tooltip"
          className="aifa-pg-tip absolute left-0 top-full z-10 mt-1 w-60 rounded-md border border-[var(--color-neutral-300)] bg-[var(--color-bg)] p-2 text-[11px] text-[var(--color-text)] shadow-lg"
        >
          <div className="font-medium">{label.name}</div>
          <div className="mt-0.5 text-[var(--color-neutral-600)]">{label.description}</div>
          <div className="mt-1 text-[10px] text-[var(--color-neutral-500)]">
            Статус: {NODE_STATUS_LABEL[status]}
            {duration !== null && ` · ${formatDuration(duration)}`}
            {run && run.attempt > 1 && ` · попытка ${run.attempt}`}
          </div>
          <div className="mt-0.5 text-[10px] text-[var(--color-neutral-500)]">Даёт: {produced}</div>
          {run?.error && <div className="mt-1 text-[10px] text-[var(--destructive)]">{run.error}</div>}
          <div className="mt-1 text-[9.5px] text-[var(--color-neutral-400)]">{ln.node.node_key.atom_name}</div>
        </div>
      )}
    </div>
  );
});

export function PlanGraphWorkspace({
  graph,
  nodeRuns,
  terminal,
}: {
  graph: PlanGraphContract;
  nodeRuns?: Record<string, StoredNodeRun>;
  terminal?: GraphTerminal;
}) {
  const { nodes, width, height, size, orientation } = useMemo(() => layoutGraph(graph), [graph]);
  const byToken = useMemo(() => new Map(nodes.map((n) => [n.token, n])), [nodes]);
  const summary = useMemo(() => summarizeProgress(graph, nodeRuns, terminal), [graph, nodeRuns, terminal]);
  const now = useNow(summary.state === "running");
  const rootToken = nodeToken(graph.root);
  const scenarioByRoot = useMemo(
    () => new Map((graph.scenarios ?? []).map((scenario) => [nodeToken(scenario.root), scenario])),
    [graph.scenarios],
  );

  if (!nodes.length) return null;

  const dynamic = isDynamicPlan(graph);
  const large = graph.nodes.length > LARGE_THRESHOLD;
  const elapsedSeconds =
    summary.startedAtMs === null
      ? null
      : Math.max(0, ((summary.finishedAtMs ?? now) - summary.startedAtMs) / 1000);
  const headline = graphHeadline(summary);

  return (
    <div
      className="aifa-pg flex h-full min-h-0 flex-col"
      data-plan-graph-hash={graph.graph_hash}
      data-plan-state={summary.state}
      data-plan-kind={dynamic ? "dynamic" : "deterministic"}
      data-plan-orientation={orientation}
      data-plan-scale={large ? "large" : "normal"}
    >
      <style>{STYLES}</style>

      <div className="border-b border-[var(--color-divider)] px-4 pb-2.5 pt-2">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-500)]">План исполнения</span>
          <span
            className={cn(
              "rounded-full border px-2 py-[1px] text-[10px]",
              dynamic
                ? "border-[var(--pg-run)] text-[var(--pg-run)]"
                : "border-[var(--color-divider)] text-[var(--color-neutral-600)]",
            )}
            title={
              dynamic
                ? "Детерминированный планировщик не справился — план собрал агент-планировщик из существующих атомов"
                : "План построен детерминированным планировщиком"
            }
          >
            {dynamic ? "собран агентом" : "автоплан"}
          </span>
          <span
            className={cn(
              "rounded-full px-2 py-[1px] text-[10.5px] font-medium",
              summary.state === "failed" && "bg-[color-mix(in_srgb,var(--pg-fail)_14%,transparent)] text-[var(--pg-fail)]",
              summary.state === "succeeded" && "bg-[color-mix(in_srgb,var(--pg-success)_16%,transparent)] text-[var(--pg-success)]",
              summary.state === "running" && "bg-[color-mix(in_srgb,var(--pg-run)_14%,transparent)] text-[var(--pg-run)]",
              summary.state === "idle" && "bg-[var(--color-neutral-100)] text-[var(--color-neutral-600)]",
            )}
            data-plan-headline
          >
            {headline}
          </span>
          {elapsedSeconds !== null && (
            <span className="ml-auto text-[10.5px] tabular-nums text-[var(--color-neutral-500)]">
              {formatDuration(elapsedSeconds)}
            </span>
          )}
        </div>
        {showsProgressBar(summary.state) && (
          <div
            className="aifa-pg-bar mt-2 h-[5px] overflow-hidden rounded-full"
            role="progressbar"
            aria-label="Выполнение плана"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={summary.percent}
          >
            <div className="aifa-pg-bar-fill h-full rounded-full" style={{ width: `${summary.percent}%` }} />
          </div>
        )}
        <div className="mt-1 flex flex-wrap gap-x-3 text-[10px] text-[var(--color-neutral-500)]">
          <span>{summary.done} готово</span>
          {summary.empty > 0 && <span>{summary.empty} без результата</span>}
          {summary.notRun > 0 && <span>{summary.notRun} не выполнялось</span>}
          {summary.running > 0 && <span>{summary.running} выполняется</span>}
          {summary.failed > 0 && <span className="text-[var(--pg-fail)]">{summary.failed} с ошибкой</span>}
          <span>{summary.pending} в очереди</span>
          <span className="ml-auto">всего узлов: {summary.total}</span>
        </div>
        {(graph.scenarios?.length ?? 0) > 1 && (
          <div className="mt-2 flex flex-wrap gap-1.5" data-compound-plan>
            {graph.scenarios!.map((scenario) => (
              <span key={scenario.goal_ref} className="rounded border border-[var(--color-divider)] px-1.5 py-0.5 text-[9.5px] text-[var(--color-neutral-600)]">
                {scenario.goal_ref}: {scenario.goal_type}
                {scenario.depends_on.length > 0 && ` ← ${scenario.depends_on.join(", ")}`}
              </span>
            ))}
          </div>
        )}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-4" role="group" aria-label="Построенный план исполнения">
        <div className="relative" style={{ width, height }}>
          <svg className="pointer-events-none absolute inset-0" width={width} height={height} aria-hidden="true">
            {!large && (
              <defs>
                {MARKERS.map(([state, fill]) => (
                  <marker
                    key={state} id={`aifa-pg-arrow-${state}`} viewBox="0 0 8 8" refX="7" refY="4"
                    markerWidth="7" markerHeight="7" orient="auto-start-reverse"
                  >
                    <path d="M0,0 L8,4 L0,8 z" fill={fill} />
                  </marker>
                ))}
              </defs>
            )}
            {nodes.flatMap((target) =>
              target.node.requires.map((req) => {
                const source = byToken.get(nodeToken(req));
                if (!source) return null;
                const state = edgeState(
                  statusOf(nodeRuns, source.token, Boolean(terminal)),
                  statusOf(nodeRuns, target.token, Boolean(terminal)),
                );
                return (
                  <GraphEdge
                    key={`${source.token}->${target.token}`}
                    source={source}
                    target={target}
                    state={state}
                    orientation={orientation}
                    size={size}
                    arrow={!large}
                  />
                );
              }),
            )}
          </svg>
          {nodes.map((ln) => (
            <GraphNodeCard
              key={ln.token}
              ln={ln}
              run={nodeRuns?.[ln.token]}
              terminal={Boolean(terminal)}
              size={size}
              isRoot={ln.token === rootToken}
              scenarioRef={scenarioByRoot.get(ln.token)?.goal_ref}
              large={large}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
