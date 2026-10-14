"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight, Loader2, Play } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";
import { CalendarSection } from "@/components/ir-actions";
import {
  getIRCalendarCompanies,
  listSources,
  scenarioRunStream,
  type IRCalendarCompany,
  type IRCalendarEvent,
  type PersonalSource,
} from "@/lib/api";
import {
  DOW_LABELS,
  getScenario,
  KIND_LABEL,
  listScenarioRuns,
  patchScenario,
  runScenarioNow,
  scenarioRunUrl,
  scheduleLabel,
  type Scenario,
  type ScenarioKind,
  type ScenarioRunRef,
  type Schedule,
} from "@/lib/scenarios";
import { cn } from "@/lib/utils";

/**
 * Страница сценария автоматизации (эталон: README, «Страница сценария
 * автоматизации»). Накрывает рабочую строку, рельс остаётся слева.
 *
 * ГРАНИЦА ЧЕСТНОСТИ. Из пяти вкладок эталона бэкенд обеспечивает данными
 * полторы: параметры (schema.py) читаются и пишутся, журнал прогонов и их
 * артефакты отдаются ручками. Всё остальное, чего в API нет — состав разделов
 * выпуска, пошаговая воронка сборки, статистика по источникам за прогон,
 * отметка сбоя в календаре — НЕ рисуется: вместо этого стоит надпись, чего
 * именно не хватает. Никаких выдуманных чисел.
 */

// ─────────────────────────── схема ручек ───────────────────────────
// Зеркало src/finengine/skills/scenarios/schema.py (PARAM_SCHEMAS). Единый
// источник правды — там; ручку добавили на бэке → добавить сюда, иначе она
// просто не появится на странице (валидацию всё равно делает сервер).

type IntSpec = {
  kind: "int";
  key: string;
  label: string;
  desc: string;
  min: number;
  max: number;
  def: number;
  /** Как значение читается в предложении: «48 часов». */
  unit: (n: number) => string;
  presets: number[];
};
type EnumSpec = {
  kind: "enum";
  key: string;
  label: string;
  desc: string;
  def: string;
  options: { value: string; label: string; note: string }[];
};
type FlagsSpec = {
  kind: "flags";
  key: string;
  label: string;
  desc: string;
  keys: { key: string; label: string }[];
};
type ParamSpec = IntSpec | EnumSpec | FlagsSpec;

const PARAMS: Partial<Record<ScenarioKind, ParamSpec[]>> = {
  ir_daily: [
    {
      kind: "int",
      key: "since_hours",
      label: "Период сбора, часов назад",
      desc: "За сколько часов назад собирать новости из источников.",
      min: 6,
      max: 336,
      def: 48,
      unit: (n) => `${n} ч`,
      presets: [6, 12, 24, 48, 72],
    },
    {
      kind: "flags",
      key: "source_groups",
      label: "Группы источников",
      desc: "Какие группы источников опрашивать. Выключенная группа не собирается вовсе.",
      keys: [
        { key: "official", label: "Официальные" },
        { key: "media", label: "СМИ" },
        { key: "telegram", label: "Telegram" },
        { key: "aggregator", label: "Агрегаторы" },
      ],
    },
    {
      kind: "enum",
      key: "macro_mode",
      label: "Макропоказатели",
      desc: "Какой блок экономики попадает в выпуск.",
      def: "both",
      options: [
        { value: "both", label: "обновления недели + полный дашборд", note: "по умолчанию" },
        { value: "week_only", label: "только обновившиеся на этой неделе", note: "короче выпуск" },
        { value: "full", label: "только полный дашборд", note: "без пометки новизны" },
      ],
    },
  ],
  ir_calendar: [
    {
      kind: "int",
      key: "back_days",
      label: "Окно назад, дней",
      desc: "Сколько дней назад искать уже прошедшие события.",
      min: 1,
      max: 120,
      def: 14,
      unit: (n) => `${n} дн.`,
      presets: [7, 14, 30, 60],
    },
    {
      kind: "int",
      key: "fwd_days",
      label: "Окно вперёд, дней",
      desc: "На сколько дней вперёд искать анонсированные события.",
      min: 1,
      max: 120,
      def: 14,
      unit: (n) => `${n} дн.`,
      presets: [7, 14, 30, 60],
    },
    {
      kind: "flags",
      key: "event_groups",
      label: "Группы событий",
      desc: "Какие группы событий показывать в собранном календаре.",
      keys: [
        { key: "reporting", label: "Отчётность" },
        { key: "investor_day", label: "День инвестора" },
        { key: "other", label: "Прочее" },
      ],
    },
  ],
};

/** Предложение «что делает сценарий»: текст вперемешку с щёлкаемыми ручками. */
type Seg = { t: "text"; v: string } | { t: "param"; key: string };

const SENTENCE: Partial<Record<ScenarioKind, Seg[]>> = {
  ir_daily: [
    { t: "text", v: "Собираю новости, опубликованные за последние " },
    { t: "param", key: "since_hours" },
    { t: "text", v: ", из групп источников: " },
    { t: "param", key: "source_groups" },
    { t: "text", v: ". В блоке экономики показываю " },
    { t: "param", key: "macro_mode" },
    { t: "text", v: ". Готовый выпуск сохраняю в журнал прогонов." },
  ],
  ir_calendar: [
    { t: "text", v: "Собираю события IR-календаря: прошедшие за " },
    { t: "param", key: "back_days" },
    { t: "text", v: " и анонсированные на " },
    { t: "param", key: "fwd_days" },
    { t: "text", v: " вперёд. В выпуск попадают группы: " },
    { t: "param", key: "event_groups" },
    { t: "text", v: ". Таблица сохраняется в журнал прогонов." },
  ],
};

// ─────────────────────────── значения ручек ───────────────────────────

function intOf(params: Record<string, unknown>, s: IntSpec): number {
  const n = Number(params[s.key]);
  return Number.isFinite(n) ? Math.max(s.min, Math.min(s.max, Math.trunc(n))) : s.def;
}

function enumOf(params: Record<string, unknown>, s: EnumSpec): string {
  const v = String(params[s.key] ?? "");
  return s.options.some((o) => o.value === v) ? v : s.def;
}

/** Пустой/отсутствующий ключ = все группы включены (дефолт движка). */
function flagsOf(params: Record<string, unknown>, s: FlagsSpec): Record<string, boolean> {
  const raw = params[s.key];
  const cur = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  const out: Record<string, boolean> = {};
  for (const k of s.keys) out[k.key] = cur[k.key] === undefined ? true : Boolean(cur[k.key]);
  return out;
}

function valueLabel(params: Record<string, unknown>, s: ParamSpec): string {
  if (s.kind === "int") return s.unit(intOf(params, s));
  if (s.kind === "enum") {
    const v = enumOf(params, s);
    return s.options.find((o) => o.value === v)?.label ?? v;
  }
  const f = flagsOf(params, s);
  const on = s.keys.filter((k) => f[k.key]).map((k) => k.label);
  return on.length ? on.join(", ") : "ни одной группы";
}

/** Отличается ли ручка от значения по умолчанию (для счётчика «изменено»). */
function isDirty(params: Record<string, unknown>, s: ParamSpec): boolean {
  if (s.kind === "int") return intOf(params, s) !== s.def;
  if (s.kind === "enum") return enumOf(params, s) !== s.def;
  const f = flagsOf(params, s);
  return s.keys.some((k) => !f[k.key]);
}

// ─────────────────────────── мелкие утилиты ───────────────────────────

function fmtWhen(sec: number): string {
  return new Date(sec * 1000).toLocaleString("ru-RU", {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function dayKey(sec: number): string {
  const d = new Date(sec * 1000);
  return `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
}

function fmtCost(v: number): string {
  return v >= 0.01 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;
}

function fmtNum(v: number): string {
  return v.toLocaleString("ru-RU");
}

/** Отчёт компилятора инструкции приходит ОДНОЙ строкой (params._compiled_note),
 * склеенной в scenarios/compiler.py из фиксированных зачинов. Режем по ним,
 * чтобы «применил» шло золотой линией, а отказы — серой (эталон, вкладка
 * «Агент»). Зачин не нашёлся — показываем строку целиком, ничего не выдумывая. */
const NOTE_HEADS: { head: string; tone: "gold" | "gray" }[] = [
  { head: "Применил: ", tone: "gold" },
  { head: "Не применимо: ", tone: "gray" },
  { head: "Требует доработки навыка (передам редактору): ", tone: "gray" },
  { head: "Отклонено валидацией: ", tone: "gray" },
  { head: "Изменений настроек не потребовалось.", tone: "gray" },
];

function parseNote(note: string): { tone: "gold" | "gray"; text: string }[] {
  const hits = NOTE_HEADS.map((h) => ({ ...h, at: note.indexOf(h.head) }))
    .filter((h) => h.at >= 0)
    .sort((a, b) => a.at - b.at);
  if (!hits.length) return note.trim() ? [{ tone: "gray", text: note.trim() }] : [];
  return hits.map((h, i) => ({
    tone: h.tone,
    text: note.slice(h.at, i + 1 < hits.length ? hits[i + 1].at : undefined).trim(),
  }));
}

// ─────────────────────────── тело артефакта ───────────────────────────

type RunBody = {
  meta?: {
    kind?: string;
    title?: string;
    created_at?: number;
    counts?: Record<string, number> | null;
    tokens?: { in?: number; out?: number; cost_usd?: number } | null;
  };
  payload?: {
    markdown?: string;
    events?: IRCalendarEvent[];
    /** ir_daily: концы воронки, которые движок реально сохраняет. */
    meta?: { items_raw?: number; items_kept?: number; cost_usd?: number };
  };
};

// ─────────────────────────── страница ───────────────────────────

type Tab = "agent" | "issue" | "cal" | "runs" | "src";

const TABS: [Tab, string][] = [
  ["agent", "Агент"],
  ["issue", "Выпуск"],
  ["cal", "Календарь"],
  ["runs", "Сборки"],
  ["src", "Источники"],
];

/** Незавершённый прогон переживает F5: id хода лежит рядом с id сценария. */
const runKey = (id: string) => `aifa:v2:scenario-run:${id}`;

export function ScenarioView({
  scenarioId,
  onBack,
  onChanged,
}: {
  scenarioId: string;
  onBack: () => void;
  /** Сценарий изменился (пауза/расписание/имя) — рельс перечитывает список. */
  onChanged?: () => void;
}) {
  // Ответ помечен id сценария, которому принадлежит: пока ответа на ТЕКУЩИЙ нет
  // — это «загружаю», и при переключении сценария не мелькает чужая карточка.
  // scn === null внутри свежего ответа = ручка отказала.
  const [box, setBox] = useState<{ id: string; scn: Scenario | null } | null>(null);
  const [runs, setRuns] = useState<ScenarioRunRef[]>([]);
  const [tab, setTab] = useState<Tab>("agent");
  const [busy, setBusy] = useState(false);
  const [openParam, setOpenParam] = useState<string>("");
  const [fineOpen, setFineOpen] = useState(false);

  const [selectedRunId, setSelectedRunId] = useState<string>("");
  const [body, setBody] = useState<{ runId: string; data: RunBody | null } | null>(null);

  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);

  const fresh = box && box.id === scenarioId ? box : null;
  const scn = fresh?.scn ?? null;
  const failed = !!fresh && !fresh.scn;
  const putScn = useCallback(
    (s: Scenario) => setBox({ id: scenarioId, scn: s }),
    [scenarioId],
  );

  const params = useMemo(
    () => (scn?.params ?? {}) as Record<string, unknown>,
    [scn],
  );
  const specs = useMemo(() => (scn ? (PARAMS[scn.kind] ?? []) : []), [scn]);

  const refreshRuns = useCallback(
    () =>
      listScenarioRuns(scenarioId)
        .then((r) => {
          setRuns(r);
          // Свежий прогон становится показанным выпуском — иначе после сборки
          // слева остался бы вчерашний.
          setSelectedRunId((cur) => (r.some((x) => x.run_id === cur) ? cur : (r[0]?.run_id ?? "")));
        })
        .catch(() => undefined),
    [scenarioId],
  );

  useEffect(() => {
    let cancelled = false;
    getScenario(scenarioId)
      .then((s) => {
        if (!cancelled) setBox({ id: scenarioId, scn: s });
      })
      .catch(() => {
        if (!cancelled) setBox({ id: scenarioId, scn: null });
      });
    refreshRuns();
    return () => {
      cancelled = true;
    };
  }, [scenarioId, refreshRuns]);

  // Тело выбранного прогона грузим один раз на всю страницу: его читают и
  // предпросмотр слева, и вкладка «Сборки» справа.
  useEffect(() => {
    if (!selectedRunId) return;
    let cancelled = false;
    fetch(scenarioRunUrl(scenarioId, selectedRunId), {
      credentials: "include",
      cache: "no-store",
    })
      .then((r) => (r.ok ? (r.json() as Promise<RunBody>) : Promise.reject(new Error(String(r.status)))))
      .then((d) => {
        if (!cancelled) setBody({ runId: selectedRunId, data: d });
      })
      .catch(() => {
        if (!cancelled) setBody({ runId: selectedRunId, data: null });
      });
    return () => {
      cancelled = true;
    };
  }, [scenarioId, selectedRunId]);

  const follow = useCallback(
    async (runId: string) => {
      abortRef.current?.abort();
      const ac = new AbortController();
      abortRef.current = ac;
      setRunning(true);
      setProgress([]);
      window.localStorage.setItem(runKey(scenarioId), runId);
      try {
        for await (const raw of scenarioRunStream(scenarioId, runId, ac.signal)) {
          // ScenarioRunEvent в lib/api не описывает done/error, которые шлёт
          // runner.py — читаем поток как размеченные записи, не сужая union.
          const ev = raw as { type: string; text?: string; run_id?: string; message?: string };
          if (ev.type === "progress" && ev.text) setProgress((p) => [...p, ev.text!]);
          else if (ev.type === "artifact" && ev.run_id) setSelectedRunId(ev.run_id);
          else if (ev.type === "error") {
            setProgress((p) => [...p, `Сбой: ${ev.message ?? "без объяснения"}`]);
            break;
          } else if (ev.type === "done") break;
        }
      } catch (e) {
        if (!ac.signal.aborted) setProgress((p) => [...p, `Поток оборвался: ${String(e)}`]);
      } finally {
        if (!ac.signal.aborted) {
          setRunning(false);
          window.localStorage.removeItem(runKey(scenarioId));
          refreshRuns();
          // Прогон переписал last_status/last_error в строке сценария — иначе
          // вкладка «Сборки» показывала бы позапрошлый сбой.
          getScenario(scenarioId).then(putScn).catch(() => undefined);
        }
      }
    },
    [scenarioId, refreshRuns, putScn],
  );

  // Реаттач: страницу перезагрузили посреди 3–5-минутной сборки — подписываемся
  // заново, поток реплеит журнал с начала.
  useEffect(() => {
    const saved = window.localStorage.getItem(runKey(scenarioId));
    if (!saved) return () => abortRef.current?.abort();
    // Подписку откладываем на тик: follow ставит состояние синхронно, а прямо
    // в теле эффекта это каскадный рендер (react-hooks/set-state-in-effect).
    const t = window.setTimeout(() => void follow(saved), 0);
    return () => {
      window.clearTimeout(t);
      abortRef.current?.abort();
    };
  }, [scenarioId, follow]);

  const patch = useCallback(
    async (input: Parameters<typeof patchScenario>[1], okText?: string) => {
      setBusy(true);
      try {
        putScn(await patchScenario(scenarioId, input));
        if (okText) toast.success(okText);
        onChanged?.();
      } catch (e) {
        toast.error(String(e));
      } finally {
        setBusy(false);
      }
    },
    [scenarioId, onChanged, putScn],
  );

  const start = useCallback(async () => {
    try {
      const { run_id } = await runScenarioNow(scenarioId);
      void follow(run_id);
    } catch (e) {
      toast.error(`Запуск не прошёл: ${e}`);
    }
  }, [scenarioId, follow]);

  if (failed) {
    return (
      <div className="flex h-full items-start justify-center bg-[var(--color-bg)] p-8">
        <Honest title="Автоматизация не открылась">
          Ручка <code className="text-[11.5px]">GET /scenarios/{scenarioId}</code> ответила
          ошибкой. Показывать нечего — вернитесь назад и попробуйте ещё раз.
        </Honest>
      </div>
    );
  }

  if (!scn) {
    return (
      <div className="flex h-full items-center justify-center gap-2 bg-[var(--color-bg)] text-[12.5px] text-[var(--color-neutral-500)]">
        <Loader2 className="size-3.5 animate-spin" />
        Загружаю автоматизацию…
      </div>
    );
  }

  const dirtyCount = specs.filter((s) => isDirty(params, s)).length;

  return (
    <div className="flex h-full min-w-0 bg-[var(--color-neutral-200)]">
      {/* ── левая колонка: что делает сценарий + предпросмотр выпуска ── */}
      <div className="flex min-w-0 flex-1 flex-col border-r border-[var(--color-divider)] bg-[var(--color-bg)]">
        <div className="flex shrink-0 items-center gap-3 border-b border-[var(--color-divider)] px-4 py-3">
          <button
            type="button"
            onClick={onBack}
            title="Вернуться к рабочему месту"
            className="flex shrink-0 items-center gap-1.5 text-[11px] uppercase tracking-[0.1em] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
          >
            <ChevronLeft className="size-3.5" />
            к работе
          </button>
          <span className="h-[18px] w-px shrink-0 bg-[var(--color-divider)]" />
          <span className="font-heading min-w-0 truncate text-[19px] leading-tight text-[var(--color-text)]">
            {scn.name}
          </span>
          <span className="shrink-0 text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
            {KIND_LABEL[scn.kind]} · {scheduleLabel(scn.schedule)}
          </span>

          <span className="ml-auto flex shrink-0 items-center gap-3">
            {scn.schedule.freq !== "manual" && (
              <Switch
                on={!scn.paused}
                busy={busy}
                label={scn.paused ? "на паузе" : "включено"}
                title="Включить или выключить запуск по расписанию"
                onToggle={() =>
                  patch(
                    { paused: !scn.paused },
                    scn.paused ? "Запуск по расписанию включён" : "Запуск по расписанию на паузе",
                  )
                }
              />
            )}
            <button
              type="button"
              onClick={start}
              disabled={running}
              className="flex items-center gap-1.5 rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] px-3 py-1.5 text-[12.5px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)] disabled:cursor-not-allowed disabled:opacity-60"
            >
              {running ? <Loader2 className="size-3.5 animate-spin" /> : <Play className="size-3.5" />}
              {running ? "Собираю…" : "Собрать сейчас"}
            </button>
          </span>
        </div>

        <div className="flex min-h-0 flex-1 flex-col gap-6 overflow-y-auto px-6 py-4">
          {/* ── что делает сценарий ── */}
          <section className="flex flex-col gap-3">
            <Rule
              title="Что делает сценарий"
              hint={
                specs.length
                  ? "подчёркнутое меняется щелчком — или напишите агенту справа"
                  : undefined
              }
              right={
                specs.length ? (
                  <span
                    className="text-[10px] uppercase tracking-[0.1em] [font-variant-numeric:tabular-nums]"
                    style={{
                      color: dirtyCount
                        ? "var(--color-accent-700)"
                        : "var(--color-neutral-500)",
                    }}
                  >
                    {dirtyCount ? `изменено: ${dirtyCount}` : "всё по умолчанию"}
                  </span>
                ) : undefined
              }
            />

            <p className="text-[14.5px] leading-[2] text-[var(--color-neutral-800)]">
              <span>Запуск: </span>
              <button
                type="button"
                onClick={() => setTab("cal")}
                title="Расписание меняется во вкладке «Календарь»"
                className="border-b border-dashed border-[var(--color-neutral-400)] text-[var(--color-text)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent-700)] [font-variant-numeric:tabular-nums]"
              >
                {scheduleLabel(scn.schedule)}
              </button>
              <span>{scn.paused && scn.schedule.freq !== "manual" ? " (сейчас на паузе). " : ". "}</span>

              {(SENTENCE[scn.kind] ?? []).map((seg, i) => {
                if (seg.t === "text") return <span key={i}>{seg.v}</span>;
                const s = specs.find((x) => x.key === seg.key);
                if (!s) return null;
                const dirty = isDirty(params, s);
                return (
                  <button
                    key={i}
                    type="button"
                    title={s.desc}
                    onClick={() => setOpenParam(openParam === s.key ? "" : s.key)}
                    className={cn(
                      "border-b px-1 transition-colors [font-variant-numeric:tabular-nums]",
                      openParam === s.key
                        ? "border-[var(--color-accent)] bg-[var(--color-accent-100)] text-[var(--color-accent-700)]"
                        : dirty
                          ? "border-[var(--color-accent)] text-[var(--color-accent-700)]"
                          : "border-dashed border-[var(--color-neutral-400)] text-[var(--color-text)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent-700)]",
                    )}
                  >
                    {valueLabel(params, s)}
                  </button>
                );
              })}
            </p>

            {!specs.length && (
              <Honest title="Типизированных ручек у этого вида нет">
                Автоматизация вида «{KIND_LABEL[scn.kind]}» настраивается только текстом
                инструкции — вкладка «Агент» справа. Схема параметров
                (<code className="text-[11.5px]">scenarios/schema.py</code>) описывает ручки
                лишь для IR Daily и IR Календаря.
              </Honest>
            )}

            {specs.map((s) =>
              openParam === s.key ? (
                <ParamEditor
                  key={s.key}
                  spec={s}
                  params={params}
                  busy={busy}
                  onClose={() => setOpenParam("")}
                  onSave={(value) =>
                    patch(
                      { params: { [s.key]: value } },
                      "Сохранено — применится при следующем запуске",
                    )
                  }
                />
              ) : null,
            )}

            <div className="flex flex-col gap-2">
              <button
                type="button"
                onClick={() => setFineOpen((v) => !v)}
                className="flex items-center gap-1.5 self-start text-[11px] uppercase tracking-[0.1em] text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
              >
                <ChevronDown className={cn("size-3 transition-transform", !fineOpen && "-rotate-90")} />
                Тонкая настройка отбора
              </button>
              {fineOpen && (
                <div className="border-l border-[var(--color-divider)] pl-3">
                  <p className="text-[12.5px] leading-relaxed text-[var(--color-neutral-600)]">
                    Эталон держит здесь ещё четыре ручки: кандидатов на разбор, долю
                    официальных источников, скорость старения новости и окно проверки
                    повторов. В движке это константы — схема параметров их не содержит,
                    менять нечего. Появятся в схеме — появятся здесь.
                  </p>
                </div>
              )}
            </div>
          </section>

          {/* ── живой прогон ── */}
          {(running || progress.length > 0) && (
            <section className="flex flex-col gap-2">
              <Rule
                title={running ? "Идёт сборка" : "Последняя сборка в этом окне"}
                right={
                  running ? (
                    <Loader2 className="size-3.5 animate-spin text-[var(--color-accent)]" />
                  ) : undefined
                }
              />
              <div className="flex flex-col gap-1 border-l-2 border-[var(--color-accent)] pl-3">
                {progress.map((line, i) => (
                  <span
                    key={i}
                    className="text-[12px] leading-relaxed text-[var(--color-neutral-700)] [font-variant-numeric:tabular-nums]"
                  >
                    {line}
                  </span>
                ))}
                {running && !progress.length && (
                  <span className="text-[12px] text-[var(--color-neutral-500)]">
                    Прогон начался, первых строк ещё нет…
                  </span>
                )}
              </div>
            </section>
          )}

          {/* ── предпросмотр выпуска ── */}
          <IssuePreview
            kind={scn.kind}
            runs={runs}
            selectedRunId={selectedRunId}
            body={body}
          />
        </div>
      </div>

      {/* ── правая колонка 452px ── */}
      <div className="flex w-[452px] min-w-0 shrink-0 flex-col bg-[var(--color-bg)]">
        <div className="flex shrink-0 items-stretch border-b border-[var(--color-divider)]">
          {TABS.map(([id, label]) => (
            <button
              key={id}
              type="button"
              onClick={() => setTab(id)}
              className={cn(
                "flex-1 border-b-2 px-0.5 py-3 text-[11.5px] transition-colors",
                tab === id
                  ? "border-[var(--color-accent)] text-[var(--color-text)]"
                  : "border-transparent text-[var(--color-neutral-600)] hover:text-[var(--color-accent-700)]",
              )}
            >
              {label}
            </button>
          ))}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
          {tab === "agent" && <AgentTab scn={scn} busy={busy} onPatch={patch} />}
          {tab === "issue" && (
            <IssueTab scn={scn} params={params} specs={specs} busy={busy} onPatch={patch} />
          )}
          {tab === "cal" && (
            <CalendarTab
              scn={scn}
              runs={runs}
              selectedRunId={selectedRunId}
              busy={busy}
              onPick={setSelectedRunId}
              onPatch={patch}
            />
          )}
          {tab === "runs" && (
            <RunsTab
              scn={scn}
              runs={runs}
              selectedRunId={selectedRunId}
              body={body}
              onPick={setSelectedRunId}
            />
          )}
          {tab === "src" && (
            <SourcesTab scn={scn} params={params} specs={specs} busy={busy} onPatch={patch} />
          )}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────── общие мелочи ───────────────────────────

function Rule({
  title,
  hint,
  right,
}: {
  title: string;
  hint?: string;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline gap-2 border-b border-[var(--color-divider)] pb-1.5">
      <span className="text-[11px] uppercase tracking-[0.14em] text-[var(--color-text)]">
        {title}
      </span>
      {hint && <span className="text-[10.5px] text-[var(--color-neutral-500)]">{hint}</span>}
      {right && <span className="ml-auto flex items-center">{right}</span>}
    </div>
  );
}

/** Честное «этого нет» — с точным указанием, чего именно не хватает. */
function Honest({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] p-3">
      <div className="text-[10px] uppercase tracking-[0.14em] text-[var(--color-neutral-600)]">
        Недоступно
      </div>
      <div className="font-heading mt-1 text-[15px] leading-snug text-[var(--color-text)]">
        {title}
      </div>
      <p className="mt-1.5 text-[12.5px] leading-relaxed text-[var(--color-neutral-700)]">
        {children}
      </p>
    </div>
  );
}

function Switch({
  on,
  label,
  title,
  busy,
  onToggle,
}: {
  on: boolean;
  label: string;
  title?: string;
  busy?: boolean;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      disabled={busy}
      onClick={onToggle}
      className="flex items-center gap-2 text-[11px] uppercase tracking-[0.1em] transition-colors disabled:opacity-60"
      style={{ color: on ? "var(--color-accent-700)" : "var(--color-neutral-500)" }}
    >
      <span
        className="flex h-4 w-[30px] items-center p-px"
        style={{
          border: `1px solid ${on ? "var(--color-accent)" : "var(--color-neutral-400)"}`,
          justifyContent: on ? "flex-end" : "flex-start",
        }}
      >
        <span
          className="size-3"
          style={{ background: on ? "var(--color-accent)" : "var(--color-neutral-400)" }}
        />
      </span>
      {label}
    </button>
  );
}

// ─────────────────────────── редактор ручки ───────────────────────────

function ParamEditor({
  spec,
  params,
  busy,
  onSave,
  onClose,
}: {
  spec: ParamSpec;
  params: Record<string, unknown>;
  busy: boolean;
  onSave: (value: unknown) => void;
  onClose: () => void;
}) {
  const [draft, setDraft] = useState(() =>
    spec.kind === "int" ? String(intOf(params, spec)) : "",
  );

  return (
    <div className="flex flex-col gap-2 border border-[var(--color-accent)] bg-[var(--color-surface)] p-3 shadow-[var(--shadow-sm)]">
      <div className="flex items-baseline gap-2">
        <span className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-accent-700)]">
          {spec.label}
        </span>
        <span className="ml-auto text-right text-[10.5px] text-[var(--color-neutral-600)]">
          {spec.desc}
        </span>
      </div>

      {spec.kind === "int" && (
        <div className="flex items-center gap-2">
          <input
            type="number"
            min={spec.min}
            max={spec.max}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="w-[116px] rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-bg)] px-2 py-1 text-center text-[13.5px] text-[var(--color-text)] [font-variant-numeric:tabular-nums]"
          />
          {spec.presets.map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setDraft(String(p))}
              className="border border-[var(--color-divider)] px-2 py-0.5 text-[11.5px] text-[var(--color-neutral-700)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent-700)] [font-variant-numeric:tabular-nums]"
            >
              {p}
            </button>
          ))}
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              const n = parseInt(draft, 10);
              // Клэмп дублирует серверный (schema.validate_param) — чтобы
              // человек сразу видел, куда упрётся значение.
              const v = Number.isFinite(n)
                ? Math.max(spec.min, Math.min(spec.max, n))
                : intOf(params, spec);
              onSave(v);
              onClose();
            }}
            className="ml-auto rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] px-2.5 py-1 text-[12px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)] disabled:opacity-60"
          >
            Готово
          </button>
        </div>
      )}

      {spec.kind === "enum" && (
        <div className="flex flex-col">
          {spec.options.map((o) => {
            const active = enumOf(params, spec) === o.value;
            return (
              <button
                key={o.value}
                type="button"
                disabled={busy}
                onClick={() => {
                  onSave(o.value);
                  onClose();
                }}
                className="flex items-baseline gap-2 border-l-2 px-2 py-1.5 text-left transition-colors hover:bg-[var(--color-accent-100)] disabled:opacity-60"
                style={{
                  borderColor: active ? "var(--color-accent)" : "transparent",
                  background: active ? "var(--color-accent-100)" : "transparent",
                }}
              >
                <span
                  className="min-w-0 flex-1 text-[13px]"
                  style={{ color: active ? "var(--color-accent-700)" : "var(--color-text)" }}
                >
                  {o.label}
                </span>
                <span className="shrink-0 text-[10.5px] text-[var(--color-neutral-600)]">
                  {o.note}
                </span>
                <span className="w-3 shrink-0 text-[11px] text-[var(--color-accent-700)]">
                  {active ? "✓" : ""}
                </span>
              </button>
            );
          })}
        </div>
      )}

      {spec.kind === "flags" && (
        <FlagsRows
          spec={spec}
          params={params}
          busy={busy}
          onSave={(next) => onSave(next)}
        />
      )}
    </div>
  );
}

/** Группы-тумблеры. Отправляем ВЕСЬ объект: PATCH заменяет ключ целиком, а не
 * мержит его внутренности — частичный объект погасил бы неупомянутые группы. */
function FlagsRows({
  spec,
  params,
  busy,
  onSave,
}: {
  spec: FlagsSpec;
  params: Record<string, unknown>;
  busy: boolean;
  onSave: (next: Record<string, boolean>) => void;
}) {
  const cur = flagsOf(params, spec);
  return (
    <div className="flex flex-col">
      {spec.keys.map((k) => (
        <div
          key={k.key}
          className="flex items-center gap-2 border-b border-[var(--color-neutral-200)] py-1.5 last:border-b-0"
        >
          <Switch
            on={cur[k.key]}
            busy={busy}
            label=""
            onToggle={() => onSave({ ...cur, [k.key]: !cur[k.key] })}
          />
          <span
            className="min-w-0 flex-1 text-[12.5px]"
            style={{ color: cur[k.key] ? "var(--color-text)" : "var(--color-neutral-500)" }}
          >
            {k.label}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────── предпросмотр выпуска ───────────────────────────

function IssuePreview({
  kind,
  runs,
  selectedRunId,
  body,
}: {
  kind: ScenarioKind;
  runs: ScenarioRunRef[];
  selectedRunId: string;
  body: { runId: string; data: RunBody | null } | null;
}) {
  const ref = runs.find((r) => r.run_id === selectedRunId) ?? null;
  const fresh = body && body.runId === selectedRunId ? body : null;
  const data = fresh?.data;
  const events = useMemo(() => data?.payload?.events ?? [], [data]);
  const md = data?.payload?.markdown ?? "";

  return (
    <section className="flex flex-col gap-3">
      <Rule
        title={ref ? "Выпуск" : "Выпуск"}
        hint={ref?.summary || undefined}
        right={
          ref ? (
            <span className="text-[10px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
              {fmtWhen(ref.created_at)}
            </span>
          ) : undefined
        }
      />

      {!runs.length && (
        <p className="text-[12.5px] text-[var(--color-neutral-500)]">
          Прогонов ещё не было — показывать нечего. Нажмите «Собрать сейчас», выпуск
          появится здесь и в журнале.
        </p>
      )}

      {!!runs.length && !selectedRunId && (
        <p className="text-[12.5px] text-[var(--color-neutral-500)]">
          Выпуск не выбран — щёлкните день в календаре или строку в журнале «Сборки».
        </p>
      )}

      {!!selectedRunId && !fresh && (
        <p className="flex items-center gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
          <Loader2 className="size-3.5 animate-spin" />
          Читаю выпуск…
        </p>
      )}

      {fresh && !data && (
        <Honest title="Выпуск не пришёл">
          Ручка <code className="text-[11.5px]">GET /scenarios/…/runs/{selectedRunId}</code>{" "}
          ответила ошибкой. Тело прогона показать нечем.
        </Honest>
      )}

      {fresh && data && (
        <div className="border border-[var(--color-neutral-400)] bg-[var(--color-surface)] px-6 py-4 shadow-[var(--shadow-md)]">
          <div className="font-heading mb-3 text-[25px] leading-[1.15] text-[var(--color-text)]">
            {data.meta?.title || ref?.title || "Выпуск"}
          </div>

          {kind === "ir_calendar" && events.length > 0 && (
            <div className="text-[13px]">
              <CalendarSection
                title="Предстоящие"
                icon="🔜"
                events={events.filter((e) => e.when !== "recent")}
              />
              <CalendarSection
                title="Недавние (по факту)"
                icon="🆕"
                events={events.filter((e) => e.when === "recent")}
                desc
              />
            </div>
          )}

          {kind !== "ir_calendar" && md && (
            <div className="prose prose-sm max-w-none text-[13.5px] leading-relaxed text-[var(--color-text)]">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{md}</ReactMarkdown>
            </div>
          )}

          {((kind === "ir_calendar" && !events.length) || (kind !== "ir_calendar" && !md)) && (
            <p className="text-[12.5px] text-[var(--color-neutral-500)]">
              В этом прогоне нет данных: движок сохранил артефакт, но тело пустое.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

// ─────────────────────────── вкладка «Агент» ───────────────────────────

const PRESETS: Partial<Record<ScenarioKind, string[]>> = {
  ir_daily: [
    "собирай за 12 часов",
    "не бери Telegram",
    "только официальные источники",
    "покажи только обновления недели по экономике",
  ],
  ir_calendar: [
    "смотри на 30 дней вперёд",
    "оставь только отчётность",
    "назад достаточно 7 дней",
  ],
};

function AgentTab({
  scn,
  busy,
  onPatch,
}: {
  scn: Scenario;
  busy: boolean;
  onPatch: (input: Parameters<typeof patchScenario>[1], okText?: string) => Promise<void>;
}) {
  // Компилятор возвращает сохранённую инструкцию вместе с отчётом — пересеваем
  // черновик прямо в рендере (React-паттерн «adjusting state during render»):
  // через эффект был бы кадр со старым текстом поверх нового отчёта.
  const [text, setText] = useState(scn.instructions);
  const [seed, setSeed] = useState(scn.instructions);
  if (seed !== scn.instructions) {
    setSeed(scn.instructions);
    setText(scn.instructions);
  }

  const note = String((scn.params as Record<string, unknown>)?._compiled_note ?? "");
  const parts = parseNote(note);
  const dirty = text.trim() !== (scn.instructions || "").trim();

  return (
    <div className="flex flex-col gap-3">
      <Rule title="Инструкция агенту" />
      <p className="text-[11.5px] leading-relaxed text-[var(--color-neutral-600)]">
        Это не диалог: текст компилируется в параметры одним проходом при
        сохранении, а не отвечает репликами. Переписка со сценарием (ветка чата,
        как в эталоне) на бэке не заведена — есть только этот отчёт.
      </p>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        maxLength={4000}
        placeholder="например: собирай за 12 часов и не бери Telegram"
        className="w-full resize-y rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] px-2.5 py-2 text-[13px] leading-relaxed text-[var(--color-text)] placeholder:text-[var(--color-neutral-500)]"
      />

      <div className="flex flex-wrap gap-1.5">
        {(PRESETS[scn.kind] ?? []).map((p) => (
          <button
            key={p}
            type="button"
            onClick={() => setText((t) => (t.trim() ? `${t.trim()}\n${p}` : p))}
            className="border border-[var(--color-divider)] px-2 py-0.5 text-[11.5px] text-[var(--color-neutral-700)] transition-colors hover:border-[var(--color-accent)] hover:text-[var(--color-accent-700)]"
          >
            {p}
          </button>
        ))}
      </div>

      <button
        type="button"
        disabled={busy || !dirty}
        onClick={() => onPatch({ instructions: text.trim() })}
        className="self-start rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] px-3 py-1.5 text-[12.5px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)] disabled:cursor-not-allowed disabled:opacity-60"
      >
        {busy ? "Применяю…" : "Править"}
      </button>

      {parts.length > 0 && (
        <div className="flex flex-col gap-2">
          <Rule title="Что вышло" />
          {parts.map((p, i) => (
            <p
              key={i}
              className="border-l-2 pl-2.5 text-[11.5px] leading-relaxed"
              style={{
                borderColor:
                  p.tone === "gold" ? "var(--color-accent)" : "var(--color-neutral-400)",
                color:
                  p.tone === "gold" ? "var(--color-accent-700)" : "var(--color-neutral-600)",
              }}
            >
              {p.text}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// ─────────────────────────── вкладка «Выпуск» ───────────────────────────

function IssueTab({
  scn,
  params,
  specs,
  busy,
  onPatch,
}: {
  scn: Scenario;
  params: Record<string, unknown>;
  specs: ParamSpec[];
  busy: boolean;
  onPatch: (input: Parameters<typeof patchScenario>[1], okText?: string) => Promise<void>;
}) {
  // В выпуск попадает то, что разрешают ручки состава: у календаря — группы
  // событий, у дайджеста — режим макроблока. Остальной состав разделов зашит
  // в движок рендера.
  const composition = specs.filter(
    (s) => s.key === "event_groups" || s.key === "macro_mode",
  );

  return (
    <div className="flex flex-col gap-3">
      <Rule title="Состав выпуска" />

      {composition.map((s) => (
        <div key={s.key} className="flex flex-col gap-1.5">
          <span className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)]">
            {s.label}
          </span>
          {s.kind === "flags" && (
            <FlagsRows
              spec={s}
              params={params}
              busy={busy}
              onSave={(next) =>
                onPatch({ params: { [s.key]: next } }, "Состав выпуска сохранён")
              }
            />
          )}
          {s.kind === "enum" && (
            <div className="flex flex-col">
              {s.options.map((o) => {
                const active = enumOf(params, s) === o.value;
                return (
                  <button
                    key={o.value}
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      onPatch({ params: { [s.key]: o.value } }, "Состав выпуска сохранён")
                    }
                    className="flex items-baseline gap-2 border-l-2 px-2 py-1.5 text-left transition-colors hover:bg-[var(--color-accent-100)] disabled:opacity-60"
                    style={{
                      borderColor: active ? "var(--color-accent)" : "transparent",
                      background: active ? "var(--color-accent-100)" : "transparent",
                    }}
                  >
                    <span
                      className="min-w-0 flex-1 text-[12.5px]"
                      style={{ color: active ? "var(--color-accent-700)" : "var(--color-text)" }}
                    >
                      {o.label}
                    </span>
                    <span className="w-3 shrink-0 text-[11px] text-[var(--color-accent-700)]">
                      {active ? "✓" : ""}
                    </span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      ))}

      <Honest title="Заголовок, разделы и лимит пунктов не настраиваются">
        {scn.kind === "ir_daily"
          ? "Порядок и состав разделов дайджеста (макро, регуляторы, главные сюжеты, прочее) и размер списка главных сюжетов зашиты в движок рендера — наружу ни тумблеров, ни лимитов не выведено. В схеме параметров таких ручек нет, поэтому показывать «−/N/+» было бы обманом."
          : "Заголовок и структура таблицы задаются движком; наружу выведены только группы событий выше."}
      </Honest>

      <p className="text-[11.5px] leading-relaxed text-[var(--color-neutral-600)]">
        Слева — уже собранный выпуск, а не живой предпросмотр: правки применятся при
        следующем запуске.
      </p>
    </div>
  );
}

// ─────────────────────────── вкладка «Календарь» ───────────────────────────

const WEEK = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"];
type Repeat = "manual" | "hourly" | "daily" | "weekdays" | "custom";

function repeatOf(s: Schedule): Repeat {
  if (s.freq === "manual") return "manual";
  if (s.freq === "hourly") return "hourly";
  if (s.freq === "daily") return "daily";
  const d = [...(s.dows ?? [])].sort((a, b) => a - b);
  return d.length === 5 && d.every((x, i) => x === i) ? "weekdays" : "custom";
}

function CalendarTab({
  scn,
  runs,
  selectedRunId,
  busy,
  onPick,
  onPatch,
}: {
  scn: Scenario;
  runs: ScenarioRunRef[];
  selectedRunId: string;
  busy: boolean;
  onPick: (runId: string) => void;
  onPatch: (input: Parameters<typeof patchScenario>[1], okText?: string) => Promise<void>;
}) {
  const [month, setMonth] = useState(() => {
    const d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), 1);
  });

  // День → самый свежий прогон этого дня (журнал уже отсортирован новыми вперёд).
  const byDay = useMemo(() => {
    const m = new Map<string, ScenarioRunRef>();
    for (const r of runs) if (!m.has(dayKey(r.created_at))) m.set(dayKey(r.created_at), r);
    return m;
  }, [runs]);

  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const daysInMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const lead = (first.getDay() + 6) % 7; // неделя с понедельника
  const cells: (number | null)[] = [
    ...Array<null>(lead).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];

  const [rep, setRep] = useState<Repeat>(() => repeatOf(scn.schedule));
  const [at, setAt] = useState(scn.schedule.at_time || "09:00");
  const [dows, setDows] = useState<number[]>(() =>
    repeatOf(scn.schedule) === "custom" ? [...(scn.schedule.dows ?? [])] : [],
  );
  // Расписание мог переписать кто-то ещё (агент тулом fe_scenario_settings,
  // соседняя вкладка) — пересеваем поля в рендере, а не эффектом.
  const seedKey = `${scn.schedule.freq}|${scn.schedule.at_time}|${(scn.schedule.dows ?? []).join(",")}`;
  const [seed, setSeed] = useState(seedKey);
  if (seed !== seedKey) {
    setSeed(seedKey);
    setRep(repeatOf(scn.schedule));
    setAt(scn.schedule.at_time || "09:00");
    setDows(repeatOf(scn.schedule) === "custom" ? [...(scn.schedule.dows ?? [])] : []);
  }

  const saveSchedule = () => {
    const schedule: Schedule =
      rep === "manual"
        ? { freq: "manual", at_time: at, dows: [] }
        : rep === "hourly"
          ? { freq: "hourly", at_time: "00:00", dows: [] }
          : rep === "daily"
            ? { freq: "daily", at_time: at, dows: [] }
            : rep === "weekdays"
              ? { freq: "weekly", at_time: at, dows: [0, 1, 2, 3, 4] }
              : { freq: "weekly", at_time: at, dows };
    void onPatch({ schedule }, "Расписание сохранено");
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-baseline gap-2">
        <span className="text-[11px] uppercase tracking-[0.12em] text-[var(--color-text)] [font-variant-numeric:tabular-nums]">
          {month.toLocaleString("ru-RU", { month: "long", year: "numeric" })}
        </span>
        <span className="ml-auto flex items-center gap-1">
          <button
            type="button"
            onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
            className="grid size-5 place-items-center text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
            title="Предыдущий месяц"
          >
            <ChevronLeft className="size-3.5" />
          </button>
          <button
            type="button"
            onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
            className="grid size-5 place-items-center text-[var(--color-neutral-600)] transition-colors hover:text-[var(--color-accent-700)]"
            title="Следующий месяц"
          >
            <ChevronRight className="size-3.5" />
          </button>
        </span>
      </div>

      <div className="grid grid-cols-7 gap-1">
        {WEEK.map((w) => (
          <span
            key={w}
            className="text-center text-[9.5px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]"
          >
            {w}
          </span>
        ))}
        {cells.map((n, i) => {
          if (n === null) return <span key={`e${i}`} className="aspect-square" />;
          const key = `${month.getFullYear()}-${month.getMonth() + 1}-${n}`;
          const run = byDay.get(key) ?? null;
          const active = !!run && run.run_id === selectedRunId;
          return (
            <button
              key={key}
              type="button"
              disabled={!run}
              title={run ? `${run.title || "Прогон"} · ${fmtWhen(run.created_at)}` : "Прогонов в этот день нет"}
              onClick={() => run && onPick(run.run_id)}
              className={cn(
                "flex aspect-square flex-col items-center justify-center gap-1 border text-[12px] transition-colors [font-variant-numeric:tabular-nums]",
                run
                  ? "cursor-pointer hover:bg-[var(--color-accent-100)]"
                  : "cursor-default",
              )}
              style={{
                borderColor: active ? "var(--color-accent)" : "var(--color-divider)",
                background: active ? "var(--color-accent-100)" : "transparent",
                color: run ? "var(--color-text)" : "var(--color-neutral-500)",
              }}
            >
              {n}
              <span
                className="size-1"
                style={{ background: run ? "var(--color-accent)" : "transparent" }}
              />
            </button>
          );
        })}
      </div>

      <div className="flex items-center gap-1.5 text-[10.5px] text-[var(--color-neutral-600)]">
        <span className="size-1 bg-[var(--color-accent)]" />
        выпуск собран — щёлкните день, он откроется слева
      </div>
      <p className="text-[11px] leading-relaxed text-[var(--color-neutral-600)]">
        Отметок «сбой» и «запуск вручную» здесь нет намеренно: при ошибке движок не
        сохраняет прогон в журнал вовсе, а ручной и плановый запуски пишутся
        одинаковым признаком. Причина последнего сбоя — во вкладке «Сборки».
      </p>

      <div className="flex flex-col gap-2 border-t border-[var(--color-divider)] pt-3">
        <span className="text-[10px] uppercase tracking-[0.12em] text-[var(--color-neutral-500)]">
          Когда собирать
        </span>
        <select
          value={rep}
          onChange={(e) => setRep(e.target.value as Repeat)}
          className="rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1.5 text-[12.5px] text-[var(--color-text)]"
        >
          <option value="manual">Не повторять (вручную)</option>
          <option value="hourly">Каждый час</option>
          <option value="daily">Ежедневно</option>
          <option value="weekdays">По будням (Пн–Пт)</option>
          <option value="custom">По дням недели…</option>
        </select>

        {rep === "custom" && (
          <div className="flex flex-wrap gap-1.5">
            {DOW_LABELS.map((lbl, d) => (
              <button
                key={d}
                type="button"
                onClick={() =>
                  setDows((prev) =>
                    prev.includes(d) ? prev.filter((x) => x !== d) : [...prev, d],
                  )
                }
                className="border px-2 py-0.5 text-[11.5px] transition-colors"
                style={{
                  borderColor: dows.includes(d) ? "var(--color-accent)" : "var(--color-divider)",
                  color: dows.includes(d) ? "var(--color-accent-700)" : "var(--color-neutral-700)",
                  background: dows.includes(d) ? "var(--color-accent-100)" : "transparent",
                }}
              >
                {lbl}
              </button>
            ))}
          </div>
        )}

        {rep !== "manual" && rep !== "hourly" && (
          <label className="flex items-center gap-2 text-[11.5px] text-[var(--color-neutral-600)]">
            Время (МСК)
            <input
              type="time"
              value={at}
              onChange={(e) => setAt(e.target.value)}
              className="rounded-[var(--radius-token-sm)] border border-[var(--color-divider)] bg-[var(--color-surface)] px-2 py-1 text-[12.5px] text-[var(--color-text)] [font-variant-numeric:tabular-nums]"
            />
          </label>
        )}

        <div className="flex items-center gap-3">
          <button
            type="button"
            disabled={busy || (rep === "custom" && !dows.length)}
            onClick={saveSchedule}
            className="rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] px-3 py-1.5 text-[12.5px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            Сохранить расписание
          </button>
          {scn.schedule.freq !== "manual" && (
            <Switch
              on={!scn.paused}
              busy={busy}
              label={scn.paused ? "на паузе" : "включено"}
              onToggle={() => onPatch({ paused: !scn.paused })}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────── вкладка «Сборки» ───────────────────────────

function RunsTab({
  scn,
  runs,
  selectedRunId,
  body,
  onPick,
}: {
  scn: Scenario;
  runs: ScenarioRunRef[];
  selectedRunId: string;
  body: { runId: string; data: RunBody | null } | null;
  onPick: (runId: string) => void;
}) {
  const fresh = body && body.runId === selectedRunId ? body.data : null;
  const funnel = fresh?.payload?.meta;
  const tokens = fresh?.meta?.tokens;
  const counts = fresh?.meta?.counts;

  const raw = Number(funnel?.items_raw ?? NaN);
  const kept = Number(funnel?.items_kept ?? NaN);
  const cost = Number(funnel?.cost_usd ?? tokens?.cost_usd ?? NaN);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <Rule title="Как собирался выпуск" />

        {!selectedRunId && (
          <p className="text-[12.5px] text-[var(--color-neutral-500)]">
            Прогон не выбран — щёлкните строку в журнале ниже.
          </p>
        )}

        {!!selectedRunId && !fresh && (
          <p className="flex items-center gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
            <Loader2 className="size-3.5 animate-spin" /> Читаю прогон…
          </p>
        )}

        {fresh && (
          <div className="flex flex-col">
            {Number.isFinite(raw) && (
              <Stage title="Просмотрел новостей" value={fmtNum(raw)} />
            )}
            {Number.isFinite(kept) && (
              <Stage title="Оставил в выпуске" value={fmtNum(kept)} />
            )}
            {counts &&
              Object.entries(counts).map(([k, v]) => (
                <Stage key={k} title={`Сохранено: ${k}`} value={fmtNum(Number(v))} />
              ))}
            {tokens && (Number(tokens.in) > 0 || Number(tokens.out) > 0) && (
              <Stage
                title="Токенов у модели"
                value={`${fmtNum(Number(tokens.in ?? 0))} → ${fmtNum(Number(tokens.out ?? 0))}`}
              />
            )}
            {Number.isFinite(cost) && <Stage title="Цена прогона" value={fmtCost(cost)} />}
            {!Number.isFinite(raw) && !Number.isFinite(kept) && !counts && (
              <p className="text-[12.5px] text-[var(--color-neutral-500)]">
                Этот прогон не сохранил ни одного счётчика.
              </p>
            )}
          </div>
        )}

        <Honest title="Пошаговой воронки в прогоне нет">
          Движок сохраняет только концы цепочки — сколько просмотрел и сколько
          оставил. Промежуточные шаги эталона (убрал повторы −N, отбросил лишнее −N,
          отобрал кандидатов, пересказал моделью) считаются в памяти и уходят в логи,
          в артефакт прогона они не пишутся.
        </Honest>
      </div>

      {scn.last_status === "error" && scn.last_error && (
        <div className="flex flex-col gap-1 border-l-2 border-[var(--destructive)] pl-2.5">
          <span className="text-[10px] uppercase tracking-[0.12em] text-[var(--destructive)]">
            Последний запуск упал
          </span>
          <p className="text-[11.5px] leading-relaxed text-[var(--color-neutral-700)]">
            {scn.last_error}
          </p>
          <span className="text-[10.5px] text-[var(--color-neutral-500)]">
            Упавший прогон в журнал не попадает — это единственный его след.
          </span>
        </div>
      )}

      <div className="flex flex-col gap-2">
        <Rule title="Прошлые сборки" right={
          <span className="text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
            {runs.length}
          </span>
        } />
        {!runs.length && (
          <p className="text-[12.5px] text-[var(--color-neutral-500)]">
            Журнал пуст — успешных прогонов ещё не было.
          </p>
        )}
        {runs.map((r) => {
          const active = r.run_id === selectedRunId;
          return (
            <button
              key={r.run_id}
              type="button"
              onClick={() => onPick(r.run_id)}
              className="flex flex-col gap-0.5 border-l-2 p-2 text-left transition-colors hover:bg-[var(--color-accent-100)]"
              style={{
                borderColor: active ? "var(--color-accent)" : "var(--color-divider)",
                background: active ? "var(--color-accent-100)" : "transparent",
              }}
            >
              <span className="flex items-baseline gap-2 text-[12.5px] [font-variant-numeric:tabular-nums]">
                <span className="min-w-0 truncate text-[var(--color-text)]">
                  {r.title || "Прогон"}
                </span>
                <span className="ml-auto shrink-0 text-[10px] uppercase tracking-[0.11em] text-[var(--color-neutral-500)]">
                  {fmtWhen(r.created_at)}
                </span>
              </span>
              {r.summary && (
                <span className="text-[10.5px] text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
                  {r.summary}
                </span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function Stage({ title, value }: { title: string; value: string }) {
  return (
    <div className="flex items-baseline gap-2 border-l-2 border-[var(--color-accent)] px-2 py-1.5 text-[12.5px]">
      <span className="min-w-0 flex-1 text-[var(--color-text)]">{title}</span>
      <span className="shrink-0 text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
        {value}
      </span>
    </div>
  );
}

// ─────────────────────────── вкладка «Источники» ───────────────────────────

/** Тип источника из каталога → группа сбора. Зеркало _TYPE_TO_GROUP в
 * skills/news/collect.py: по этим же группам работает ручка source_groups. */
const TYPE_TO_GROUP: Record<string, string> = {
  official: "official",
  wire: "media",
  tier1: "media",
  wire_tg: "telegram",
  aggregator: "aggregator",
};
const GROUP_TITLE: Record<string, string> = {
  official: "Регуляторы и раскрытия",
  media: "Агентства и деловые СМИ",
  telegram: "Телеграм-каналы",
  aggregator: "Поисковые агрегаторы",
  other: "Прочие",
};

function SourcesTab({
  scn,
  params,
  specs,
  busy,
  onPatch,
}: {
  scn: Scenario;
  params: Record<string, unknown>;
  specs: ParamSpec[];
  busy: boolean;
  onPatch: (input: Parameters<typeof patchScenario>[1], okText?: string) => Promise<void>;
}) {
  const [sources, setSources] = useState<PersonalSource[] | null>(null);
  const [companies, setCompanies] = useState<IRCalendarCompany[] | null>(null);
  const [failed, setFailed] = useState(false);

  const daily = scn.kind === "ir_daily";

  useEffect(() => {
    let cancelled = false;
    const p = daily
      ? listSources().then((s) => {
          if (!cancelled) setSources(s);
        })
      : getIRCalendarCompanies().then((d) => {
          if (!cancelled) setCompanies(d.companies ?? []);
        });
    p.catch(() => {
      if (!cancelled) setFailed(true);
    });
    return () => {
      cancelled = true;
    };
  }, [daily]);

  const groupSpec = specs.find((s) => s.key === "source_groups");
  const grouped = useMemo(() => {
    const m = new Map<string, PersonalSource[]>();
    for (const s of sources ?? []) {
      const g = TYPE_TO_GROUP[s.type] ?? "other";
      const arr = m.get(g);
      if (arr) arr.push(s);
      else m.set(g, [s]);
    }
    return m;
  }, [sources]);

  if (!daily && scn.kind !== "ir_calendar") {
    return (
      <Honest title="Источников у этого вида нет">
        Автоматизация «{KIND_LABEL[scn.kind]}» ходит туда, куда её послал агент по
        инструкции — фиксированного набора коннекторов у неё не бывает.
      </Honest>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {daily && groupSpec && groupSpec.kind === "flags" && (
        <div className="flex flex-col gap-1.5">
          <Rule title="Группы сбора" hint="выключенная группа не опрашивается" />
          <FlagsRows
            spec={groupSpec}
            params={params}
            busy={busy}
            onSave={(next) => onPatch({ params: { source_groups: next } }, "Группы сохранены")}
          />
        </div>
      )}

      {failed && (
        <Honest title="Список не пришёл">
          Ручка{" "}
          <code className="text-[11.5px]">{daily ? "GET /sources" : "GET /ir/calendar/companies"}</code>{" "}
          ответила ошибкой.
        </Honest>
      )}

      {daily && !sources && !failed && (
        <p className="flex items-center gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
          <Loader2 className="size-3.5 animate-spin" /> Читаю коннекторы…
        </p>
      )}

      {daily &&
        sources &&
        [...grouped.entries()].map(([g, items]) => (
          <div key={g} className="flex flex-col gap-1.5">
            <Rule
              title={GROUP_TITLE[g] ?? g}
              right={
                <span className="text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                  {items.length}
                </span>
              }
            />
            {items.map((s) => (
              <div key={s.id} className="flex items-baseline gap-2 py-0.5 text-[12.5px]">
                <span
                  className="size-[5px] shrink-0 rounded-full"
                  style={{
                    background: s.active
                      ? "var(--color-accent)"
                      : "var(--color-neutral-400)",
                  }}
                />
                <span className="min-w-0 flex-1 truncate text-[var(--color-text)]" title={s.url}>
                  {s.display_name}
                </span>
                <span className="shrink-0 text-[10.5px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
                  {s.region}
                </span>
              </div>
            ))}
          </div>
        ))}

      {daily && (
        <Honest title="Сколько сюжетов дал каждый источник — не сохраняется">
          Движок считает это на каждом прогоне и один раз произносит строкой в живом
          логе сборки, но в артефакт не пишет. Поэтому у коннекторов нет счётчиков, а
          серая точка означает «выключен вами», а не «не ответил». Список источников
          общий для всех ваших автоматизаций и правится на старом экране.
        </Honest>
      )}

      {!daily && (
        <div className="flex flex-col gap-1.5">
          <Rule
            title="Компании, за которыми следим"
            right={
              <span className="text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                {companies?.length ?? "…"}
              </span>
            }
          />
          {!companies && !failed && (
            <p className="flex items-center gap-2 text-[12.5px] text-[var(--color-neutral-500)]">
              <Loader2 className="size-3.5 animate-spin" /> Читаю покрытие…
            </p>
          )}
          {companies?.map((c) => (
            <div
              key={`${c.company}-${c.ir_url}`}
              className="flex items-baseline gap-2 border-b border-[var(--color-neutral-200)] py-1 text-[12.5px]"
            >
              <span className="min-w-0 flex-1 truncate text-[var(--color-text)]">
                {c.company}
              </span>
              <span className="shrink-0 text-[10.5px] uppercase tracking-[0.1em] text-[var(--color-neutral-500)]">
                {c.region}
              </span>
            </div>
          ))}
          <p className="text-[11px] leading-relaxed text-[var(--color-neutral-600)]">
            Список покрытия задан в движке календаря и правится только там — ручки
            добавить или убрать компанию из интерфейса нет.
          </p>
        </div>
      )}
    </div>
  );
}
