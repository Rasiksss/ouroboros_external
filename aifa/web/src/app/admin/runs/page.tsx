"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowLeft, ChevronDown, ChevronRight, Download, ExternalLink } from "lucide-react";
import { toast } from "sonner";
import { useAuth } from "@/components/auth-context";
import {
  listExtractRuns,
  adminIdpOcrUrl,
  adminExtractRunsJsonlUrl,
  type ExtractRun,
} from "@/lib/auth";

const ARCH_LABEL: Record<string, string> = {
  finengine: "Finengine Baseline",
  finengine_baseline: "Finengine Baseline",
  finengine_rag: "Finengine RAG",
  finengine_dev: "Finengine DEV",
  aifa: "AIFA DEV",
  aifa_dev: "AIFA DEV",
  aifa_baseline: "AIFA baseline",
};

const MODEL_LABEL: Record<string, string> = {
  "qwen/qwen3.6-plus": "Базовая",
  "moonshotai/kimi-k2.5": "Kimi K2.5",
  "z-ai/glm-5.1": "GLM 5.1",
};

function fmtTs(ts: string): string {
  if (!ts) return "—";
  // ISO 'YYYY-MM-DDTHH:MM:SS' (UTC). Render as local-time HH:MM, dd.MM.
  const d = new Date(ts.endsWith("Z") ? ts : ts + "Z");
  if (Number.isNaN(d.getTime())) return ts;
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export default function RunsAdminPage() {
  const { user, loading } = useAuth();
  const [runs, setRuns] = useState<ExtractRun[] | null>(null);
  const [total, setTotal] = useState(0);
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const [filterBroken, setFilterBroken] = useState<"all" | "ok" | "broken">("all");
  const [filterArch, setFilterArch] = useState<string>("all");

  const refresh = useCallback(async () => {
    try {
      const { runs, total } = await listExtractRuns(500);
      setRuns(runs);
      setTotal(total);
    } catch (e) {
      toast.error((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!loading && user?.is_admin) void refresh();
  }, [loading, user, refresh]);

  const filtered = useMemo(() => {
    if (!runs) return [];
    return runs.filter((r) => {
      if (filterBroken === "ok" && r.broken) return false;
      if (filterBroken === "broken" && !r.broken) return false;
      if (filterArch !== "all" && r.arch !== filterArch) return false;
      return true;
    });
  }, [runs, filterBroken, filterArch]);

  if (loading) return null;
  if (!user) return null;
  if (!user.is_admin) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
        Доступ только для администратора.
      </div>
    );
  }

  return (
    <div className="h-full overflow-auto bg-background">
      <div className="max-w-6xl mx-auto px-6 py-8 space-y-5">
        <Link
          href="/"
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" /> К чату
        </Link>

        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight">Прогоны извлечения</h1>
            <p className="text-sm text-muted-foreground mt-1">
              Каждый запуск extract из чата пишется в общий лог. Кликни строку — увидишь diag,
              стадии, samples. Сломанные строки выделены красным и не идут в агрегаты.
            </p>
          </div>
          <a
            href={adminExtractRunsJsonlUrl()}
            className="inline-flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded border bg-card hover:bg-muted"
            download
          >
            <Download className="size-3.5" /> Скачать .jsonl
          </a>
        </div>

        {/* Filters */}
        <div className="flex flex-wrap gap-3 text-sm">
          <div className="inline-flex rounded-md border overflow-hidden">
            {(["all", "ok", "broken"] as const).map((v) => (
              <button
                key={v}
                onClick={() => setFilterBroken(v)}
                className={`px-3 py-1.5 ${
                  filterBroken === v ? "bg-primary text-primary-foreground" : "bg-card hover:bg-muted"
                }`}
              >
                {v === "all" ? "Все" : v === "ok" ? "Только OK" : "Только сломанные"}
              </button>
            ))}
          </div>
          <select
            value={filterArch}
            onChange={(e) => setFilterArch(e.target.value)}
            className="rounded-md border bg-background px-2 py-1.5"
          >
            <option value="all">Все архитектуры</option>
            <option value="finengine">Finengine</option>
            <option value="aifa_baseline">AIFA baseline</option>
            <option value="aifa_dev">AIFA DEV</option>
          </select>
          <span className="text-xs text-muted-foreground self-center">
            Показано: {filtered.length} из {runs?.length ?? "…"} (всего в логе: {total})
          </span>
        </div>

        {/* List */}
        {!runs && <p className="text-sm text-muted-foreground">Загружаю…</p>}
        {runs && filtered.length === 0 && (
          <p className="text-sm text-muted-foreground">Нет прогонов под фильтр.</p>
        )}
        <div className="space-y-1.5">
          {filtered.map((r, i) => {
            const isOpen = !!expanded[i];
            return (
              <div
                key={`${r.ts}-${i}`}
                className={`rounded-md border ${r.broken ? "border-destructive/40 bg-destructive/5" : "bg-card"}`}
              >
                <button
                  onClick={() => setExpanded((s) => ({ ...s, [i]: !s[i] }))}
                  className="w-full px-3 py-2 flex items-center gap-3 text-left text-sm hover:bg-muted/50"
                >
                  {isOpen ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
                  <span className="font-mono text-xs text-muted-foreground w-24 shrink-0">{fmtTs(r.ts)}</span>
                  <span className={`text-xs px-1.5 py-0.5 rounded shrink-0 ${
                    r.broken ? "bg-destructive/20 text-destructive" : "bg-emerald-500/15 text-emerald-700"
                  }`}>
                    {r.broken ? "BROKEN" : "OK"}
                  </span>
                  <span className="text-xs px-1.5 py-0.5 rounded bg-muted shrink-0">
                    {ARCH_LABEL[r.arch] ?? r.arch}
                  </span>
                  <span className="text-xs text-muted-foreground shrink-0">
                    {MODEL_LABEL[r.model_id] ?? r.model_id}
                  </span>
                  {(r.git_ver || r.git_sha) && (
                    <span
                      className="text-xs font-mono px-1.5 py-0.5 rounded bg-muted text-muted-foreground shrink-0"
                      title={r.git_sha ? `sha: ${r.git_sha}` : "версия деплоя"}
                    >
                      {r.git_ver || r.git_sha}
                    </span>
                  )}
                  <span className="font-medium truncate flex-1 min-w-0">{r.company_name || r.filename}</span>
                  <span className="text-xs text-muted-foreground shrink-0 tabular-nums">
                    {r.found}/{r.total}
                  </span>
                  <span className="text-xs text-muted-foreground shrink-0 tabular-nums w-14 text-right">
                    {r.elapsed_min} мин
                  </span>
                </button>

                {isOpen && (
                  <div className="px-3 pb-3 pt-1 border-t bg-background/50 text-xs space-y-2">
                    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-muted-foreground">
                      <div><span className="text-foreground">Файл:</span> <span className="font-mono">{r.filename}</span></div>
                      <div><span className="text-foreground">doc_id:</span> <span className="font-mono">{r.doc_id}</span></div>
                      <div><span className="text-foreground">Тип:</span> {r.company_type}</div>
                      <div><span className="text-foreground">Причина:</span> {r.broken_reason}</div>
                    </div>

                    {r.error && (
                      <div className="rounded bg-destructive/10 text-destructive px-2 py-1.5">
                        <span className="font-medium">error:</span> {r.error}
                      </div>
                    )}

                    {/* Diag dump — render top-level fields readable, full json collapsed */}
                    <DiagPanel diag={r.diag} />

                    <div className="flex flex-wrap gap-2 pt-1">
                      <a
                        href={adminIdpOcrUrl(r.doc_id)}
                        target="_blank"
                        rel="noreferrer"
                        className={`inline-flex items-center gap-1 px-2 py-1 rounded border ${
                          r.idpocr_available
                            ? "bg-card hover:bg-muted"
                            : "bg-muted text-muted-foreground cursor-not-allowed pointer-events-none"
                        }`}
                      >
                        <ExternalLink className="size-3" />
                        OCR JSON {r.idpocr_available ? "" : "(нет)"}
                      </a>
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function DiagPanel({ diag }: { diag: Record<string, unknown> }) {
  const [showRaw, setShowRaw] = useState(false);
  if (!diag || Object.keys(diag).length === 0) {
    return <div className="text-muted-foreground italic">no diag</div>;
  }
  // Surface common fields top-level
  const known = [
    ["ocr_s", "OCR, с"],
    ["scoring_total_s", "Реранкер, с"],
    ["llm_total_s", "LLM, с"],
    ["calc_s", "Calc, с"],
    ["metric_phase_s", "Per-metric фаза, с"],
    ["tables_total", "Таблиц"],
    ["parallel", "Параллелизм"],
  ] as const;
  const ocr = (diag.ocr ?? {}) as Record<string, unknown>;
  const usage = (diag.usage_sum ?? diag.usage ?? {}) as Record<string, unknown>;
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1">
        {known.map(([k, label]) =>
          k in diag ? (
            <div key={k}>
              <span className="text-muted-foreground">{label}:</span>{" "}
              <span className="tabular-nums">{String(diag[k as keyof typeof diag])}</span>
            </div>
          ) : null,
        )}
      </div>
      {Object.keys(ocr).length > 0 && (
        <div className="rounded border bg-card p-2">
          <div className="text-muted-foreground mb-1">OCR per-page</div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1">
            {[
              ["pages_total", "Всего"],
              ["pages_success", "OK"],
              ["pages_failed", "Ошибки"],
              ["ocr_total_s", "OCR время, с"],
            ].map(([k, label]) =>
              k in ocr ? (
                <div key={k}>
                  <span className="text-muted-foreground">{label}:</span>{" "}
                  <span className="tabular-nums">{String(ocr[k])}</span>
                </div>
              ) : null,
            )}
          </div>
        </div>
      )}
      {Object.keys(usage).length > 0 && (
        <div className="rounded border bg-card p-2">
          <div className="text-muted-foreground mb-1">Usage</div>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-3 gap-y-1">
            {Object.entries(usage).map(([k, v]) => (
              <div key={k}>
                <span className="text-muted-foreground">{k}:</span>{" "}
                <span className="tabular-nums">{String(v)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
      <button
        onClick={() => setShowRaw((v) => !v)}
        className="text-xs text-muted-foreground underline"
      >
        {showRaw ? "Скрыть raw diag" : "Показать raw diag"}
      </button>
      {showRaw && (
        <pre className="rounded border bg-card p-2 text-[11px] overflow-x-auto max-h-80">
          {JSON.stringify(diag, null, 2)}
        </pre>
      )}
    </div>
  );
}
