"use client";

import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarDays,
  ExternalLink,
  Loader2,
  Newspaper,
  Play,
  RefreshCw,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { runNewsDigest } from "@/lib/api";
import type { IRCalendarEvent, IRNewsItem } from "@/lib/api";
import type { ArtifactRef, ProjectType } from "@/lib/projects";

const DIRECT_API =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const EVENT_GROUP_LABEL: Record<string, string> = {
  reporting: "Отчётность",
  investor_day: "День инвестора",
  other: "Прочее",
};
const EVENT_TYPE_LABEL: Record<string, string> = {
  earnings: "отчётность",
  investor_day: "день инвестора",
  agm: "собрание",
  dividend: "дивиденды",
  press_release: "пресс-релиз",
  other: "событие",
};
const CATEGORY_LABEL: Record<string, string> = {
  macro: "макро",
  corporate: "корпоративные",
  regulatory: "регуляторные",
  other: "прочее",
};

function fmtDate(iso: string): string {
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso;
  return `${m[3]}.${m[2]}.${m[1]}`;
}

function fmtWeekday(iso: string): string {
  if (!/^\d{4}-\d{2}-\d{2}/.test(iso)) return "";
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("ru-RU", { weekday: "long" });
}

function fmtRelTime(unixSeconds: number): string {
  const diff = Date.now() / 1000 - unixSeconds;
  if (diff < 60) return "только что";
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  return `${Math.floor(diff / 86400)} д назад`;
}

type Props = {
  projectId: string;
  projectType: ProjectType;
  artifacts: ArtifactRef[];
  onArtifactsChanged: (next: ArtifactRef[]) => void;
  /** Fire the run once on mount — used by RoutineView's run mode: clicking
   * «Запустить» on the routine detail screen lands here and runs immediately. */
  autoRun?: boolean;
};

/** Inline action bar + artifact list rendered at the top of an IR
 * project workspace. The Run button kicks off the SSE skill, shows
 * progress, and refreshes the artifact list on completion. */
export function IRActions({ projectId, projectType, artifacts, onArtifactsChanged, autoRun }: Props) {
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [digestMd, setDigestMd] = useState<string | null>(null);
  const [digestMeta, setDigestMeta] = useState<{
    from_cache?: boolean;
    age_hours?: number;
    items_kept?: number;
  } | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const stopRun = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStatus("Остановлено");
    setRunning(false);
  }, []);

  // Stable ref to onArtifactsChanged so the effect below doesn't re-fire
  // every parent render (parent rebuilds the callback on each setProjects).
  const onArtifactsChangedRef = useRef(onArtifactsChanged);
  useEffect(() => {
    onArtifactsChangedRef.current = onArtifactsChanged;
  }, [onArtifactsChanged]);

  const refreshArtifacts = useCallback(async () => {
    try {
      const r = await fetch(`${DIRECT_API}/projects/${projectId}/artifacts`, {
        credentials: "include",
        cache: "no-store",
      });
      if (!r.ok) return;
      const data: { artifacts: ArtifactRef[] } = await r.json();
      onArtifactsChangedRef.current(data.artifacts);
    } catch {
      /* ignore */
    }
  }, [projectId]);

  // Fetch the artifact list once per project switch — never on parent
  // re-renders. Without this, every setProjects() on the page caused the
  // bar to refire its GET, flooding the backend.
  useEffect(() => {
    refreshArtifacts();
  }, [projectId, refreshArtifacts]);

  // Shared SSE consumer — used by the initial POST and by reattach-after-reload.
  // The backend run is a run_orchestrator background task that survives client
  // disconnect, so we persist its run_id (to re-subscribe) and the worker
  // finishes + saves the artifact regardless of whether a client is attached.
  const consumeCalendarSSE = useCallback(
    async (res: Response, opts: { reattach?: boolean } = {}) => {
      if (!res.ok || !res.body) {
        throw new Error(`${res.status}: ${await res.text().catch(() => res.statusText)}`);
      }
      const runKey = `ir_cal_run_${projectId}`;
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let companyDone = 0;
      let companyTotal = 0;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) !== -1) {
          const raw = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const lines = raw.split("\n").filter((l) => l.startsWith("data: "));
          if (!lines.length) continue;
          const payload = lines.map((l) => l.slice(6)).join("\n");
          let ev: { type: string; [k: string]: unknown };
          try {
            ev = JSON.parse(payload);
          } catch {
            continue;
          }
          if (ev.type === "run_id") {
            try { localStorage.setItem(runKey, String(ev.run_id)); } catch { /* ignore */ }
          } else if (ev.type === "started") {
            companyTotal = (ev.companies as number) ?? 0;
            const wb = (ev.window_back as number | undefined) ?? 14;
            const wf = (ev.window_fwd as number | undefined) ?? 14;
            setStatus(`Обхожу ${companyTotal} компаний (−${wb}/+${wf} дней)…`);
          } else if (ev.type === "source_done") {
            setStatus(`Источник ${ev.source}: +${ev.events as number} событий…`);
          } else if (ev.type === "events") {
            companyDone++;
            const cnt = Array.isArray(ev.events) ? (ev.events as unknown[]).length : 0;
            setStatus(`Обработано ${companyDone}${companyTotal ? `/${companyTotal}` : ""}${cnt ? ` · +${cnt} событий` : ""}`);
          } else if (ev.type === "done") {
            const ms = (ev.multi_source as number | undefined) ?? 0;
            const failN = (ev.fetch_failed as number | undefined) ?? 0;
            try { localStorage.removeItem(runKey); } catch { /* ignore */ }
            setStatus(
              `Готово: ${ev.total_events as number} событий` +
                (ms ? ` · ${ms} подтв. ≥2 ист.` : "") +
                (failN ? ` · не удалось ${failN}` : ""),
            );
          } else if (ev.type === "error") {
            try { localStorage.removeItem(runKey); } catch { /* ignore */ }
            if (!opts.reattach) toast.error(String(ev.message));
            setStatus(opts.reattach ? "" : `Ошибка: ${ev.message}`);
          } else if (ev.type === "artifact") {
            try { localStorage.removeItem(runKey); } catch { /* ignore */ }
            await refreshArtifacts();
            setOpenRunId(String(ev.run_id));
            toast.success("Календарь сохранён");
          }
        }
      }
    },
    [projectId, refreshArtifacts],
  );

  const runCalendarPersisted = useCallback(async () => {
    setRunning(true);
    setStatus("Запускаю сбор календаря…");
    const ctl = new AbortController();
    abortRef.current = ctl;
    try {
      const res = await fetch(`${DIRECT_API}/ir/calendar/stream`, {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        signal: ctl.signal,
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ project_id: projectId }),
      });
      await consumeCalendarSSE(res);
    } catch (e: unknown) {
      // AbortError is fine — we already showed "Остановлено" in stopRun. The
      // backend run keeps going regardless; reattach picks it up on reload.
      if ((e as Error)?.name !== "AbortError") {
        toast.error(`Ошибка: ${e}`);
      }
    } finally {
      abortRef.current = null;
      setRunning(false);
    }
  }, [projectId, consumeCalendarSSE]);

  // Reattach after a page reload: the calendar run keeps going on the backend
  // (run_orchestrator), so if a run_id is stored for this project, re-subscribe
  // to stream the rest + pick up the saved artifact. A finished run just
  // replays its journal and clears the stored id.
  useEffect(() => {
    if (projectType !== "ir_calendar") return;
    let stored: string | null = null;
    try { stored = localStorage.getItem(`ir_cal_run_${projectId}`); } catch { /* ignore */ }
    if (!stored) return;
    const ctl = new AbortController();
    abortRef.current = ctl;
    (async () => {
      setRunning(true);
      setStatus("Переподключаюсь к запущенному сбору…");
      try {
        const res = await fetch(`${DIRECT_API}/ir/calendar/runs/${stored}/stream`, {
          credentials: "include",
          cache: "no-store",
          signal: ctl.signal,
          headers: { Accept: "text/event-stream" },
        });
        await consumeCalendarSSE(res, { reattach: true });
      } catch (e: unknown) {
        if ((e as Error)?.name !== "AbortError") {
          try { localStorage.removeItem(`ir_cal_run_${projectId}`); } catch { /* ignore */ }
        }
      } finally {
        if (abortRef.current === ctl) abortRef.current = null;
        setRunning(false);
      }
    })();
    return () => ctl.abort();
  }, [projectId, projectType, consumeCalendarSSE]);

  // Daily News now runs the scripted backend ETL pipeline
  // (/skills/news_digest/run) instead of the old Tavily SSE skill. The
  // pipeline returns a rendered markdown digest (cached <24h), so there's
  // no per-item stream to follow — one request, render the markdown.
  const runDailyPersisted = useCallback(async (force = false) => {
    setRunning(true);
    setStatus(force ? "Собираю свежий дайджест…" : "Собираю дайджест…");
    setDigestMd(null);
    try {
      const res = await runNewsDigest({ fresh: force });
      if (res.error) {
        toast.error(String(res.error));
        setStatus(`Ошибка: ${res.error}`);
        return;
      }
      setDigestMd(res.markdown || "");
      setDigestMeta({
        from_cache: res.from_cache,
        age_hours: res.age_hours,
        items_kept: res.items_kept,
      });
      setStatus(
        res.from_cache
          ? `Из кэша${res.age_hours ? ` · ${Math.round(res.age_hours)}ч` : ""}`
          : `Готово · ${res.items_kept ?? 0} новостей`,
      );
    } catch (e: unknown) {
      toast.error(`Ошибка: ${e}`);
      setStatus(`Ошибка: ${e}`);
    } finally {
      setRunning(false);
    }
  }, []);

  const isCalendar = projectType === "ir_calendar";
  const Icon = isCalendar ? CalendarDays : Newspaper;
  const runFn = isCalendar ? runCalendarPersisted : runDailyPersisted;
  const ctaLabel = "Запустить";

  const autoRunFiredRef = useRef(false);
  useEffect(() => {
    if (autoRun && !autoRunFiredRef.current) {
      autoRunFiredRef.current = true;
      void runFn();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoRun]);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Icon className="size-5 text-primary shrink-0" />
        {running ? (
          <>
            <button
              type="button"
              disabled
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm opacity-70"
            >
              <Loader2 className="size-4 animate-spin" />
              Идёт сбор…
            </button>
            <button
              type="button"
              onClick={stopRun}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md border border-border/40 bg-background hover:bg-muted text-sm"
              title="Остановить запущенный сбор"
            >
              <X className="size-4" />
              Стоп
            </button>
          </>
        ) : (
          <>
            <button
              type="button"
              onClick={() => runFn()}
              className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-primary text-primary-foreground text-sm"
            >
              <Play className="size-4" />
              {ctaLabel}
            </button>
            {artifacts.length > 0 && (
              <button
                type="button"
                onClick={refreshArtifacts}
                className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
                title="Обновить список"
              >
                <RefreshCw className="size-3.5" />
              </button>
            )}
          </>
        )}
        {status && (
          <span className="text-xs text-muted-foreground truncate ml-auto">{status}</span>
        )}
      </div>

      {artifacts.length > 0 && (
        <div className="grid gap-1.5">
          {artifacts.slice(0, 5).map((a) => (
            <button
              key={a.run_id}
              type="button"
              onClick={() => setOpenRunId(a.run_id)}
              className={`text-left text-sm rounded-md border border-border/40 px-3 py-2 hover:border-primary hover:bg-accent transition flex items-center gap-2 ${
                openRunId === a.run_id ? "border-primary bg-accent" : "bg-background"
              }`}
            >
              <span className="font-medium truncate">{a.title}</span>
              <span className="text-xs text-muted-foreground ml-auto shrink-0">
                {fmtRelTime(a.created_at)}
              </span>
            </button>
          ))}
        </div>
      )}

      {openRunId && (
        <ArtifactPanel
          projectId={projectId}
          runId={openRunId}
          onClose={() => setOpenRunId(null)}
        />
      )}

      {!isCalendar && digestMd && (
        <div className="mt-4">
          <div className="flex items-center mb-2">
            <span className="text-sm font-medium">IR Daily Digest</span>
            {digestMeta?.from_cache && (
              <span className="text-xs text-muted-foreground ml-2">
                из кэша
                {digestMeta.age_hours != null
                  ? digestMeta.age_hours < 1
                    ? " · менее часа"
                    : ` · ${Math.round(digestMeta.age_hours)} ч`
                  : ""}
                <button
                  type="button"
                  onClick={() => runDailyPersisted(true)}
                  disabled={running}
                  className="ml-1.5 text-primary hover:underline disabled:opacity-50"
                  title="Пересобрать свежий дайджест (игнорируя 12ч-кэш)"
                >
                  обновить
                </button>
              </span>
            )}
          </div>
          <div className="prose prose-sm max-w-none text-sm">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{digestMd}</ReactMarkdown>
          </div>
        </div>
      )}
    </div>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  ir_site: "сайт компании",
  finnhub: "Finnhub",
  edisclosure: "e-disclosure",
  smartlab: "smart-lab",
  fmp: "FMP",
};

/** One calendar section (Предстоящие / Недавние): events grouped under a
 * date + weekday header row, with a ✓✓ confidence mark when ≥2 sources agree.
 * Exported so the Scenario detail screen can render calendar artifacts too. */
/** #89: выпадающий мультивыбор-фильтр колонки таблицы календаря. Пустой выбор
 * = «все значения». Клик мимо панели закрывает её. */
function ColumnFilter({
  label,
  values,
  selected,
  onChange,
}: {
  label: string;
  values: { value: string; label: string }[];
  selected: Set<string>;
  onChange: (next: Set<string>) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (ev: MouseEvent) => {
      if (ref.current && !ref.current.contains(ev.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  const active = selected.size > 0;
  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs ${
          active
            ? "border-primary/50 bg-primary/10 text-foreground"
            : "border-border/60 text-muted-foreground hover:text-foreground"
        }`}
      >
        {label}
        {active && <span className="font-medium">· {selected.size}</span>}
        <span aria-hidden>▾</span>
      </button>
      {open && (
        <div className="absolute z-20 mt-1 max-h-64 w-56 overflow-y-auto rounded-md border border-border bg-popover p-1.5 shadow-md">
          {active && (
            <button
              type="button"
              onClick={() => onChange(new Set())}
              className="mb-1 w-full rounded px-2 py-1 text-left text-xs text-muted-foreground hover:bg-muted"
            >
              Сбросить фильтр
            </button>
          )}
          {values.map((v) => (
            <label
              key={v.value}
              className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 text-xs hover:bg-muted"
            >
              <input
                type="checkbox"
                checked={selected.has(v.value)}
                onChange={(e) => {
                  const next = new Set(selected);
                  if (e.target.checked) next.add(v.value);
                  else next.delete(v.value);
                  onChange(next);
                }}
              />
              <span className="truncate">{v.label}</span>
            </label>
          ))}
        </div>
      )}
    </div>
  );
}

export function CalendarSection({
  title,
  icon,
  events,
  desc,
}: {
  title: string;
  icon: string;
  events: IRCalendarEvent[];
  desc?: boolean;
}) {
  // #89: клиентские фильтры по колонкам (пустой набор = все).
  const [fGroup, setFGroup] = useState<Set<string>>(new Set());
  const [fType, setFType] = useState<Set<string>>(new Set());
  const [fCompany, setFCompany] = useState<Set<string>>(new Set());
  const [fRegion, setFRegion] = useState<Set<string>>(new Set());
  const [fSource, setFSource] = useState<Set<string>>(new Set());
  const uniq = useMemo(() => {
    const groups = new Map<string, string>();
    const types = new Map<string, string>();
    const companies = new Set<string>();
    const regions = new Set<string>();
    const sources = new Map<string, string>();
    for (const e of events) {
      const gr = e.group || "other";
      groups.set(gr, EVENT_GROUP_LABEL[gr] ?? gr);
      types.set(e.event_type, EVENT_TYPE_LABEL[e.event_type] ?? e.event_type);
      if (e.company) companies.add(e.company);
      if (e.region) regions.add(e.region);
      for (const s of e.sources ?? []) sources.set(s, SOURCE_LABEL[s] ?? s);
    }
    const toArr = (m: Map<string, string>) =>
      [...m.entries()].map(([value, label]) => ({ value, label }))
        .sort((a, b) => a.label.localeCompare(b.label, "ru"));
    const toPlain = (s: Set<string>) =>
      [...s].sort((a, b) => a.localeCompare(b, "ru")).map((v) => ({ value: v, label: v }));
    return { groups: toArr(groups), types: toArr(types), companies: toPlain(companies), regions: toPlain(regions), sources: toArr(sources) };
  }, [events]);
  const filtered = useMemo(
    () =>
      events.filter(
        (e) =>
          (!fGroup.size || fGroup.has(e.group || "other")) &&
          (!fType.size || fType.has(e.event_type)) &&
          (!fCompany.size || fCompany.has(e.company)) &&
          (!fRegion.size || fRegion.has(e.region)) &&
          (!fSource.size || (e.sources ?? []).some((s) => fSource.has(s))),
      ),
    [events, fGroup, fType, fCompany, fRegion, fSource],
  );
  const anyFilter = fGroup.size + fType.size + fCompany.size + fRegion.size + fSource.size > 0;

  if (!events.length) return null;
  const sorted = [...filtered].sort((a, b) =>
    desc ? b.date.localeCompare(a.date) : a.date.localeCompare(b.date),
  );
  const byDate = new Map<string, IRCalendarEvent[]>();
  for (const e of sorted) {
    const arr = byDate.get(e.date);
    if (arr) arr.push(e);
    else byDate.set(e.date, [e]);
  }
  return (
    <div className="mb-4">
      <div className="text-sm font-medium mb-1.5">
        {icon} {title}{" "}
        <span className="text-xs font-normal text-muted-foreground">
          ({anyFilter ? `${filtered.length} из ${events.length}` : events.length})
        </span>
      </div>
      {/* #89: фильтры по колонкам */}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <ColumnFilter label="Группа" values={uniq.groups} selected={fGroup} onChange={setFGroup} />
        <ColumnFilter label="Тип" values={uniq.types} selected={fType} onChange={setFType} />
        <ColumnFilter label="Компания" values={uniq.companies} selected={fCompany} onChange={setFCompany} />
        <ColumnFilter label="Регион" values={uniq.regions} selected={fRegion} onChange={setFRegion} />
        <ColumnFilter label="Источник" values={uniq.sources} selected={fSource} onChange={setFSource} />
        {anyFilter && (
          <button
            type="button"
            onClick={() => {
              setFGroup(new Set());
              setFType(new Set());
              setFCompany(new Set());
              setFRegion(new Set());
              setFSource(new Set());
            }}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            Сбросить всё
          </button>
        )}
      </div>
      {anyFilter && filtered.length === 0 && (
        <p className="mb-2 text-xs text-muted-foreground">Под выбранные фильтры не попало ни одного события.</p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="bg-muted">
            <tr>
              <th className="px-3 py-2 text-left font-medium">Тип</th>
              <th className="px-3 py-2 text-left font-medium">Компания</th>
              <th className="px-3 py-2 text-left font-medium">Регион</th>
              <th className="px-3 py-2 text-left font-medium">Описание</th>
              <th className="px-3 py-2 text-left font-medium">Источник</th>
            </tr>
          </thead>
          <tbody>
            {[...byDate.entries()].map(([date, dayEvents]) => (
              <Fragment key={date}>
                <tr className="border-t border-border/40 bg-muted/40">
                  <td colSpan={5} className="px-3 py-1.5 text-xs font-semibold whitespace-nowrap">
                    {fmtDate(date)}
                    <span className="ml-2 font-normal text-muted-foreground capitalize">
                      {fmtWeekday(date)}
                    </span>
                  </td>
                </tr>
                {dayEvents.map((e, i) => {
                  const srcLabels = (e.sources ?? []).map((s) => SOURCE_LABEL[s] ?? s);
                  return (
                    <tr key={`${e.company}-${date}-${i}`} className="border-t border-border/40">
                      <td className="px-3 py-2">
                        <span className="rounded bg-muted px-2 py-0.5 text-xs">
                          {EVENT_TYPE_LABEL[e.event_type] ?? e.event_type}
                        </span>
                      </td>
                      <td className="px-3 py-2 font-medium whitespace-nowrap">
                        {e.company}
                        {srcLabels.length >= 2 && (
                          <span
                            className="ml-1 text-emerald-600 dark:text-emerald-500"
                            title={`Подтверждено: ${srcLabels.join(", ")}`}
                          >
                            ✓✓
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 text-muted-foreground">{e.region}</td>
                      <td className="px-3 py-2 text-muted-foreground">{e.raw_excerpt}</td>
                      <td className="px-3 py-2">
                        <a
                          href={e.source_url}
                          target="_blank"
                          rel="noreferrer"
                          title={srcLabels.join(", ")}
                          className="inline-flex items-center gap-1 text-primary hover:underline"
                        >
                          <ExternalLink className="size-3" />
                        </a>
                      </td>
                    </tr>
                  );
                })}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ArtifactPanel({
  projectId,
  runId,
  onClose,
}: {
  projectId: string;
  runId: string;
  onClose: () => void;
}) {
  const [data, setData] = useState<{
    meta: ArtifactRef & { kind: string; tokens?: Record<string, number> };
    payload: { events?: IRCalendarEvent[]; items?: IRNewsItem[]; lookahead_days?: number };
  } | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    fetch(`${DIRECT_API}/projects/${projectId}/artifacts/${runId}`, {
      credentials: "include",
      cache: "no-store",
    })
      .then((r) => r.json())
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch(() => { /* ignore */ })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, runId]);

  const events = useMemo(() => {
    const evs = data?.payload?.events ?? [];
    return [...evs].sort((a, b) => a.date.localeCompare(b.date));
  }, [data]);

  // Split into the two ТЗ sections: upcoming (announced ahead — US/EU calendars)
  // and recent (already-published — so the agent doesn't miss RU press-release
  // events that appear with no advance notice).
  const upcoming = useMemo(() => events.filter((e) => e.when !== "recent"), [events]);
  const recent = useMemo(() => events.filter((e) => e.when === "recent"), [events]);

  const items = data?.payload?.items ?? [];
  const kind = data?.meta?.kind;

  return (
    <div className="rounded-lg border border-border/40 bg-background p-3 mt-2">
      <div className="flex items-center mb-2">
        <span className="text-sm font-medium">{data?.meta?.title ?? "Загрузка…"}</span>
        <button
          type="button"
          onClick={onClose}
          className="ml-auto text-xs text-muted-foreground hover:text-foreground"
        >
          закрыть
        </button>
      </div>

      {loading && <div className="text-sm text-muted-foreground">Загрузка…</div>}

      {!loading && kind === "ir_calendar" && events.length > 0 && (
        <div>
          <CalendarSection title="Предстоящие" icon="🔜" events={upcoming} />
          <CalendarSection title="Недавние (по факту)" icon="🆕" events={recent} desc />
        </div>
      )}

      {!loading && kind === "ir_daily" && items.length > 0 && (
        <ul className="space-y-2">
          {items.map((it, i) => (
            <li
              key={it.url || i}
              className="rounded-md border border-border/40 bg-card p-3"
            >
              <div className="flex items-baseline gap-2 mb-1">
                <span className="rounded bg-muted px-2 py-0.5 text-xs">
                  {CATEGORY_LABEL[it.category] ?? it.category}
                </span>
                {it.matched_topic && (
                  <span className="text-xs text-muted-foreground">→ {it.matched_topic}</span>
                )}
                <span className="ml-auto text-xs text-muted-foreground">
                  {(it.score ?? 0).toFixed(2)}
                </span>
              </div>
              <a
                href={it.url}
                target="_blank"
                rel="noreferrer"
                className="font-medium hover:underline text-sm"
              >
                {it.title}
              </a>
              <p className="text-sm text-muted-foreground mt-1">{it.summary}</p>
            </li>
          ))}
        </ul>
      )}

      {!loading && (
        (kind === "ir_calendar" && events.length === 0) ||
        (kind === "ir_daily" && items.length === 0)
      ) && (
        <div className="text-sm text-muted-foreground">Нет данных в этом запуске.</div>
      )}
    </div>
  );
}
