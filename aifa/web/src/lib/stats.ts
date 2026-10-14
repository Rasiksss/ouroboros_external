import type { Project } from "./projects";

export type Stats = {
  projects: number;
  chats: number;
  userMessages: number;
  assistantMessages: number;
  extractions: number;
  extractedMetrics: number;
  foundMetrics: number;
  totalElapsedMs: number;
  avgMetricMs: number | null;
  documents: number;
  lastActivity: number | null;
};

export function computeStats(projects: Project[]): Stats {
  let userMessages = 0;
  let assistantMessages = 0;
  let extractions = 0;
  let extractedMetrics = 0;
  let foundMetrics = 0;
  let totalElapsedMs = 0;
  let chats = 0;
  const docIds = new Set<string>();
  let lastActivity: number | null = null;

  for (const p of projects) {
    if (p.messages.length > 0) chats++;
    for (const id of p.doc_ids) docIds.add(id);
    if (!lastActivity || p.updated_at > lastActivity) lastActivity = p.updated_at;
    for (const m of p.messages) {
      if (m.role === "user") userMessages++;
      else if (m.role === "assistant") assistantMessages++;
      else if (m.role === "extraction") {
        extractions++;
        extractedMetrics += m.metrics.length;
        foundMetrics += m.metrics.filter((x) => x.found).length;
        if (m.elapsed_ms) totalElapsedMs += m.elapsed_ms;
      }
    }
  }

  const avgMetricMs = extractedMetrics > 0 ? totalElapsedMs / extractedMetrics : null;

  return {
    projects: projects.length,
    chats,
    userMessages,
    assistantMessages,
    extractions,
    extractedMetrics,
    foundMetrics,
    totalElapsedMs,
    avgMetricMs,
    documents: docIds.size,
    lastActivity,
  };
}

export function fmtDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} мс`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)} с`;
  const m = s / 60;
  if (m < 60) return `${m.toFixed(1)} мин`;
  return `${(m / 60).toFixed(1)} ч`;
}
