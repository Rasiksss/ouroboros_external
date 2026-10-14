"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Sparkles,
  User,
  Workflow,
  BarChart3,
  Plug,
  Shield,
  Palette,
  Check,
  Copy,
  X,
  Link2,
  Unlink,
  RefreshCw,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import {
  DEFAULT_PREFS,
  archLabel,
  loadPrefs,
  savePrefs,
  MODEL_OPTIONS,
  THEME_OPTIONS,
  type AgentPrefs,
  type Persona,
} from "@/lib/prefs";
import { loadProjects } from "@/lib/projects";
import { computeStats, fmtDuration } from "@/lib/stats";
import { telegramPair, telegramStatus, telegramUnlink } from "@/lib/api";
import { useAuth } from "@/components/auth-context";

type SectionId =
  | "models"
  | "personalization"
  | "extraction"
  | "stats"
  | "integrations"
  | "security"
  | "appearance";

type Section = { id: SectionId; label: string; icon: React.ComponentType<{ className?: string }> };

const SECTIONS: Section[] = [
  { id: "models", label: "Модели и провайдеры", icon: Sparkles },
  { id: "personalization", label: "Персонализация", icon: User },
  { id: "extraction", label: "Извлечение", icon: Workflow },
  { id: "stats", label: "Статистика", icon: BarChart3 },
  { id: "integrations", label: "Интеграции", icon: Plug },
  { id: "security", label: "Безопасность", icon: Shield },
  { id: "appearance", label: "Внешний вид", icon: Palette },
];

function uid(): string {
  return Math.random().toString(36).slice(2, 10);
}

export default function SettingsPage() {
  const [prefs, setPrefs] = useState<AgentPrefs | null>(null);
  const [section, setSection] = useState<SectionId>("models");

  useEffect(() => setPrefs(loadPrefs()), []);
  useEffect(() => {
    if (prefs) savePrefs(prefs);
  }, [prefs]);

  const update = useCallback(<K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => {
    setPrefs((p) => (p ? { ...p, [k]: v } : p));
  }, []);

  if (!prefs) {
    return <div className="h-full flex items-center justify-center text-sm text-muted-foreground">Загрузка…</div>;
  }

  return (
    <div className="h-full flex bg-background">
      <aside className="w-60 shrink-0 border-r bg-sidebar text-sidebar-foreground flex flex-col">
        <div className="p-3 border-b">
          <Link
            href="/"
            className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft className="size-4" />
            К чату
          </Link>
          <h1 className="mt-2 text-lg font-semibold tracking-tight">Настройки</h1>
        </div>
        <nav className="flex-1 overflow-y-auto p-2 space-y-0.5">
          {SECTIONS.map((s) => {
            const Icon = s.icon;
            const active = s.id === section;
            return (
              <button
                key={s.id}
                type="button"
                onClick={() => setSection(s.id)}
                className={cn(
                  "w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-sm",
                  active
                    ? "bg-sidebar-accent text-sidebar-accent-foreground"
                    : "hover:bg-sidebar-accent/60 text-muted-foreground",
                )}
              >
                <Icon className="size-4" />
                {s.label}
              </button>
            );
          })}
        </nav>
        <div className="p-3 border-t">
          <Button
            size="sm"
            variant="outline"
            className="w-full"
            onClick={() => {
              if (confirm("Сбросить все настройки к дефолтным?")) {
                setPrefs(DEFAULT_PREFS);
                toast.success("Настройки сброшены");
              }
            }}
          >
            Сбросить настройки
          </Button>
        </div>
      </aside>

      <main className="flex-1 overflow-y-auto">
        <div className="max-w-3xl mx-auto px-8 py-8 space-y-8">
          {section === "models" && <ModelsSection prefs={prefs} update={update} />}
          {section === "personalization" && <PersonalizationSection prefs={prefs} update={update} />}
          {section === "extraction" && <ExtractionSection prefs={prefs} update={update} />}
          {section === "stats" && <StatsSection />}
          {section === "integrations" && <IntegrationsSection prefs={prefs} update={update} />}
          {section === "security" && <SecuritySection prefs={prefs} update={update} />}
          {section === "appearance" && <AppearanceSection prefs={prefs} update={update} />}
        </div>
      </main>
    </div>
  );
}

function SectionHeader({ title, desc }: { title: string; desc?: string }) {
  return (
    <div className="space-y-1">
      <h2 className="text-xl font-semibold tracking-tight">{title}</h2>
      {desc && <p className="text-sm text-muted-foreground">{desc}</p>}
    </div>
  );
}

function Row({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid grid-cols-[200px_1fr] gap-4 items-start py-3 border-b last:border-b-0">
      <div>
        <div className="text-sm font-medium">{label}</div>
        {hint && <div className="text-xs text-muted-foreground mt-0.5">{hint}</div>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <label className="inline-flex items-center gap-2 cursor-pointer select-none">
      <span
        className={cn(
          "relative w-9 h-5 rounded-full transition-colors",
          checked ? "bg-primary" : "bg-muted",
        )}
        onClick={() => onChange(!checked)}
      >
        <span
          className={cn(
            "absolute top-0.5 size-4 rounded-full bg-background transition-transform",
            checked ? "translate-x-[18px]" : "translate-x-0.5",
          )}
        />
      </span>
      {label && <span className="text-sm">{label}</span>}
    </label>
  );
}

// --- Models ---
function ModelsSection({
  prefs,
  update,
}: {
  prefs: AgentPrefs;
  update: <K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => void;
}) {
  return (
    <>
      <SectionHeader
        title="Модели и провайдеры"
        desc="Основная модель используется для чата и извлечения. Fallback вызываются при ошибках/rate-limit."
      />
      <div className="rounded-md border p-4 bg-card">
        <Row label="Основная модель">
          <select
            value={prefs.model}
            onChange={(e) => update("model", e.target.value)}
            className="w-full max-w-xs rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            {MODEL_OPTIONS.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
        </Row>
        <Row
          label="Temperature: извлечение"
          hint="Используется при /extract. 0 — детерминированно (рекомендуется для метрик)"
        >
          <input
            type="number"
            min={0}
            max={1}
            step={0.1}
            value={prefs.extractTemperature ?? 0}
            onChange={(e) => update("extractTemperature", Number(e.target.value))}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </Row>
        <Row
          label="Temperature: чат и выводы"
          hint="Используется в чате и анализе. 0.6 — баланс между связностью и креативностью"
        >
          <input
            type="number"
            min={0}
            max={1}
            step={0.1}
            value={prefs.chatTemperature ?? 0.6}
            onChange={(e) => update("chatTemperature", Number(e.target.value))}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </Row>
        <Row
          label="Уровень рассуждения"
          hint="Применяется только к моделям с reasoning-каналом (GLM-5.x, o1/o3). high — выше точность ценой токенов и латентности"
        >
          <div className="flex gap-1">
            {(["low", "medium", "high"] as const).map((opt) => (
              <button
                key={opt}
                type="button"
                onClick={() => update("reasoningEffort", opt)}
                className={cn(
                  "px-3 py-1.5 rounded-md border text-sm capitalize",
                  prefs.reasoningEffort === opt
                    ? "bg-primary text-primary-foreground border-primary"
                    : "hover:bg-muted",
                )}
              >
                {opt}
              </button>
            ))}
          </div>
        </Row>
      </div>

    </>
  );
}

// --- Personalization ---
function PersonalizationSection({
  prefs,
  update,
}: {
  prefs: AgentPrefs;
  update: <K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<Partial<Persona>>({});

  const editing = editingId ? prefs.personas.find((p) => p.id === editingId) : null;
  const isNew = editingId === "__new__";

  function startNew() {
    setEditingId("__new__");
    setDraft({
      name: "",
      system_prompt:
        "Ты — финансовый аналитик. Отвечаешь структурно, со ссылками на страницы отчёта. Приводи цифры с единицами измерения.",
    });
  }

  function startEdit(p: Persona) {
    setEditingId(p.id);
    setDraft({ name: p.name, system_prompt: p.system_prompt });
  }

  function save() {
    const name = (draft.name ?? "").trim();
    const sp = (draft.system_prompt ?? "").trim();
    if (!name) {
      toast.error("Укажи название пресета");
      return;
    }
    if (isNew) {
      const np: Persona = { id: uid(), name, system_prompt: sp };
      update("personas", [...prefs.personas, np]);
    } else if (editing) {
      update(
        "personas",
        prefs.personas.map((p) => (p.id === editing.id ? { ...p, name, system_prompt: sp } : p)),
      );
    }
    setEditingId(null);
    setDraft({});
  }

  function remove(id: string) {
    if (!confirm("Удалить пресет?")) return;
    update(
      "personas",
      prefs.personas.filter((p) => p.id !== id),
    );
    if (prefs.activePersonaId === id) update("activePersonaId", null);
    if (prefs.defaultPersonaId === id) update("defaultPersonaId", null);
  }

  return (
    <>
      <SectionHeader
        title="Персонализация"
        desc="Пресеты с системными промптами. Можно применять к конкретному чату или сделать пресетом по умолчанию для новых чатов."
      />

      <div className="rounded-md border p-4 bg-card space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-medium">Пресеты ({prefs.personas.length})</h3>
          <Button size="sm" onClick={startNew}>
            + Новый пресет
          </Button>
        </div>
        {prefs.personas.length === 0 && (
          <p className="text-sm text-muted-foreground">
            Пока нет пресетов. Создай первый — например, «CFO-стиль: строгие цифры с источниками».
          </p>
        )}
        <div className="space-y-2">
          {prefs.personas.map((p) => {
            const isActive = prefs.activePersonaId === p.id;
            const isDefault = prefs.defaultPersonaId === p.id;
            return (
              <div key={p.id} className="rounded-md border p-3 space-y-2">
                <div className="flex items-start gap-2">
                  <div className="flex-1 min-w-0">
                    <div className="font-medium flex items-center gap-2">
                      {p.name}
                      {isActive && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-primary text-primary-foreground uppercase tracking-wide">
                          активен
                        </span>
                      )}
                      {isDefault && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded bg-muted text-foreground uppercase tracking-wide">
                          по умолчанию
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-muted-foreground whitespace-pre-wrap line-clamp-3 mt-1">
                      {p.system_prompt || "без промпта"}
                    </div>
                  </div>
                  <div className="flex gap-1">
                    <Button size="sm" variant="outline" onClick={() => startEdit(p)}>
                      Редактировать
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => remove(p.id)}>
                      Удалить
                    </Button>
                  </div>
                </div>
                <div className="flex gap-2 text-xs">
                  <button
                    type="button"
                    onClick={() => update("activePersonaId", isActive ? null : p.id)}
                    className="text-muted-foreground hover:text-foreground"
                  >
                    {isActive ? "Отключить в текущем чате" : "Применить в текущем чате"}
                  </button>
                  <span className="text-muted-foreground">·</span>
                  <button
                    type="button"
                    onClick={() => update("defaultPersonaId", isDefault ? null : p.id)}
                    className="text-muted-foreground hover:text-foreground"
                  >
                    {isDefault ? "Снять по умолчанию" : "Сделать по умолчанию"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {editingId && (
        <div className="rounded-md border p-4 bg-card space-y-3">
          <h3 className="font-medium">{isNew ? "Новый пресет" : `Редактирование: ${editing?.name}`}</h3>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Название</label>
            <input
              value={draft.name ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
              placeholder="Например: CFO-стиль"
              className="w-full rounded-md border bg-background px-2 py-1.5 text-sm"
            />
          </div>
          <div className="space-y-1">
            <label className="text-xs text-muted-foreground">Системный промпт</label>
            <textarea
              value={draft.system_prompt ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, system_prompt: e.target.value }))}
              rows={8}
              placeholder="Опиши, какие метрики извлекать, как их считать, в каком стиле отвечать…"
              className="w-full rounded-md border bg-background px-2 py-1.5 text-sm resize-y font-mono"
            />
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => { setEditingId(null); setDraft({}); }}>
              Отмена
            </Button>
            <Button size="sm" onClick={save}>
              Сохранить
            </Button>
          </div>
        </div>
      )}
    </>
  );
}

// --- Extraction ---
function ExtractionSection({
  prefs,
  update,
}: {
  prefs: AgentPrefs;
  update: <K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => void;
}) {
  const ex = prefs.extraction;
  const set = <K extends keyof typeof ex>(k: K, v: (typeof ex)[K]) =>
    update("extraction", { ...ex, [k]: v });
  return (
    <>
      <SectionHeader
        title="Извлечение"
        desc="Параметры обработки документов и пайплайна извлечения метрик. Какие метрики извлечь в конкретном запросе — лучше указать в чате."
      />
      <div className="rounded-md border p-4 bg-card">
        <Row
          label="OCR-движок"
          hint="PyMuPDF — быстрый встроенный (хорош на цифровых PDF). IDP — облачный по-страничный (точные таблицы для банков и сложного IFRS, в 15–20× медленнее). Выбор применяется при следующей загрузке документа."
        >
          <div className="flex gap-1">
            {(
              [
                { id: "pymupdf" as const, label: "PyMuPDF (быстро)" },
                { id: "idp" as const, label: "IDP-OCR (точные таблицы)" },
              ]
            ).map((o) => (
              <button
                key={o.id}
                type="button"
                onClick={() => set("ocrEngine", o.id)}
                className={cn(
                  "px-3 py-1.5 rounded-md border text-sm",
                  ex.ocrEngine === o.id
                    ? "bg-primary text-primary-foreground border-primary"
                    : "hover:bg-muted",
                )}
              >
                {o.label}
              </button>
            ))}
          </div>
        </Row>
        <Row
          label="OCR fallback"
          hint="Распознавать страницы, где PyMuPDF не вернул текст (сканы, кривые шрифты)"
        >
          <Toggle checked={ex.ocrFallback} onChange={(v) => set("ocrFallback", v)} />
        </Row>
        <Row label="Авто-поворот страниц" hint="Перебор 4 поворотов для повёрнутых сканов">
          <Toggle checked={ex.autoRotate} onChange={(v) => set("autoRotate", v)} />
        </Row>
        <Row label="Параллельность" hint="Количество одновременных subprocess-вызовов">
          <input
            type="number"
            min={1}
            max={8}
            value={ex.parallel}
            onChange={(e) => set("parallel", Number(e.target.value))}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </Row>
        <Row label="Считать производные метрики" hint="EBITDA margin, ROE, коэффициенты">
          <Toggle checked={ex.includeCalculated} onChange={(v) => set("includeCalculated", v)} />
        </Row>
      </div>
    </>
  );
}

// --- Statistics ---
function StatsSection() {
  const { user } = useAuth();
  const [allProjects, setAllProjects] = useState<ReturnType<typeof loadProjects>>([]);
  const [archFilter, setArchFilter] = useState<string>("all");

  useEffect(() => {
    if (user?.username) setAllProjects(loadProjects(user.username));
  }, [user]);

  // Project-level arch filter. Each project has an optional arch field;
  // "all" leaves the full set untouched.
  const filteredProjects = useMemo(
    () =>
      archFilter === "all"
        ? allProjects
        : allProjects.filter((p) => (p.arch ?? "") === archFilter),
    [allProjects, archFilter],
  );

  const counts = useMemo(() => computeStats(filteredProjects), [filteredProjects]);

  const archOptions: { id: string; label: string }[] = useMemo(() => {
    const seen = new Set<string>();
    for (const p of allProjects) if (p.arch) seen.add(p.arch);
    return [
      { id: "all", label: "Все агенты" },
      ...Array.from(seen).map((id) => ({ id, label: archLabel(id) || id })),
    ];
  }, [allProjects]);

  const stat = (label: string, value: string, hint?: string) => (
    <div className="rounded-md border p-4 bg-card">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold tabular-nums mt-1">{value}</div>
      {hint && <div className="text-xs text-muted-foreground mt-1">{hint}</div>}
    </div>
  );

  return (
    <>
      <SectionHeader
        title="Статистика"
        desc="Локальная статистика по работе с агентом (из истории чатов в этом браузере). Фильтр выбирает один агент или показывает все."
      />
      <div className="rounded-md border p-3 bg-card mb-3">
        <div className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Агент:</span>
          <select
            value={archFilter}
            onChange={(e) => setArchFilter(e.target.value)}
            className="rounded-md border bg-background px-2 py-1.5 text-sm"
          >
            {archOptions.map((o) => (
              <option key={o.id} value={o.id}>{o.label}</option>
            ))}
          </select>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3">
        {stat("Чатов", String(counts.chats), `${counts.projects} проектов всего`)}
        {stat("Документов", String(counts.documents))}
        {stat("Сообщений", String(counts.userMessages + counts.assistantMessages), `ваших: ${counts.userMessages}, агента: ${counts.assistantMessages}`)}
        {stat("Запусков извлечения", String(counts.extractions))}
        {stat(
          "Извлечено метрик",
          String(counts.extractedMetrics),
          `найдено: ${counts.foundMetrics} (${counts.extractedMetrics ? Math.round((counts.foundMetrics / counts.extractedMetrics) * 100) : 0}%)`,
        )}
        {stat(
          "Среднее время на метрику",
          counts.avgMetricMs ? fmtDuration(counts.avgMetricMs) : "—",
          counts.totalElapsedMs ? `всего ${fmtDuration(counts.totalElapsedMs)}` : undefined,
        )}
      </div>
    </>
  );
}

// --- Integrations ---
function IntegrationsSection({
  prefs,
  update,
}: {
  prefs: AgentPrefs;
  update: <K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => void;
}) {
  const tg = prefs.integrations.telegram;
  const [pair, setPair] = useState<{ code: string; bot: string; expiresAt: number } | null>(null);
  const [polling, setPolling] = useState(false);

  const setIntegration = useCallback(
    <K extends keyof typeof prefs.integrations>(k: K, v: (typeof prefs.integrations)[K]) => {
      update("integrations", { ...prefs.integrations, [k]: v });
    },
    [prefs.integrations, update],
  );

  useEffect(() => {
    if (!pair || tg.linkedUser) return;
    let stop = false;
    setPolling(true);
    const tick = async () => {
      try {
        const s = await telegramStatus(pair.code);
        if (stop) return;
        if (s.expired) {
          setPair(null);
          toast.error("Код истёк, сгенерируй новый");
          return;
        }
        if (s.linked && s.linked_user) {
          setIntegration("telegram", {
            linkedUser: s.linked_user,
            pairCode: pair.code,
            pairedAt: Date.now(),
          });
          setPair(null);
          toast.success(`Telegram привязан: ${s.linked_user}`);
          return;
        }
      } catch {
        // ignore transient errors
      }
      if (!stop) setTimeout(tick, 2000);
    };
    tick();
    return () => {
      stop = true;
      setPolling(false);
    };
  }, [pair, tg.linkedUser, setIntegration]);

  async function startPairing() {
    try {
      const r = await telegramPair();
      setPair({
        code: r.code,
        bot: r.bot_username,
        expiresAt: Date.now() + r.expires_in_sec * 1000,
      });
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  async function unlink() {
    if (!tg.pairCode && !tg.linkedUser) return;
    if (!confirm("Отвязать Telegram?")) return;
    try {
      if (tg.pairCode) await telegramUnlink(tg.pairCode);
    } catch {
      // ignore
    }
    setIntegration("telegram", { linkedUser: null, pairCode: null, pairedAt: null });
    toast.success("Telegram отвязан");
  }

  return (
    <>
      <SectionHeader
        title="Интеграции"
        desc="Внешние сервисы и продолжение работы вне веба."
      />

      <div className="rounded-md border p-4 bg-card space-y-3">
        <div className="flex items-start gap-3">
          <div className="size-10 rounded-md bg-muted flex items-center justify-center">
            <Plug className="size-5" />
          </div>
          <div className="flex-1 min-w-0">
            <h3 className="font-medium">Telegram</h3>
            <p className="text-xs text-muted-foreground">
              Продолжай диалог с агентом в Telegram, получай уведомления о завершении извлечения.
            </p>
          </div>
          {tg.linkedUser ? (
            <span className="text-xs inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
              <Check className="size-3.5" /> привязан
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">не подключён</span>
          )}
        </div>

        {tg.linkedUser ? (
          <div className="flex items-center gap-3 rounded-md border bg-background p-3">
            <Link2 className="size-4 text-muted-foreground" />
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium truncate">@{tg.linkedUser}</div>
              <div className="text-xs text-muted-foreground">
                привязан {tg.pairedAt ? new Date(tg.pairedAt).toLocaleString("ru-RU") : ""}
              </div>
            </div>
            <Button size="sm" variant="outline" onClick={unlink}>
              <Unlink className="size-4 mr-1.5" />
              Отвязать
            </Button>
          </div>
        ) : pair ? (
          <div className="rounded-md border bg-background p-3 space-y-2">
            <div className="text-sm">
              1. Открой бота <span className="font-mono">{pair.bot}</span> в Telegram
            </div>
            <div className="text-sm">
              2. Отправь ему команду <span className="font-mono">/pair</span> и этот код:
            </div>
            <div className="flex items-center gap-2">
              <code className="text-2xl font-mono tracking-[0.3em] font-semibold px-3 py-2 rounded-md border bg-muted flex-1 text-center">
                {pair.code}
              </code>
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  void navigator.clipboard.writeText(pair.code);
                  toast.success("Скопировано");
                }}
              >
                <Copy className="size-4" />
              </Button>
            </div>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              {polling && <RefreshCw className="size-3 animate-spin" />}
              Жду подтверждения от бота…
              <button
                type="button"
                className="ml-auto text-muted-foreground hover:text-foreground inline-flex items-center gap-1"
                onClick={() => setPair(null)}
              >
                <X className="size-3" /> отмена
              </button>
            </div>
          </div>
        ) : (
          <Button size="sm" onClick={startPairing}>
            Подключить Telegram
          </Button>
        )}
      </div>

    </>
  );
}

// --- Security ---
function SecuritySection({
  prefs,
  update,
}: {
  prefs: AgentPrefs;
  update: <K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => void;
}) {
  const sec = prefs.security;
  const set = <K extends keyof typeof sec>(k: K, v: (typeof sec)[K]) =>
    update("security", { ...sec, [k]: v });
  return (
    <>
      <SectionHeader title="Безопасность" desc="Срок хранения истории чата на сервере." />
      <div className="rounded-md border p-4 bg-card">
        <Row label="История чата (дней)" hint="0 — не хранить между сессиями">
          <input
            type="number"
            min={0}
            max={365}
            value={sec.historyRetentionDays}
            onChange={(e) => set("historyRetentionDays", Number(e.target.value))}
            className="w-24 rounded-md border bg-background px-2 py-1.5 text-sm"
          />
        </Row>
      </div>
    </>
  );
}

// --- Appearance ---
function AppearanceSection({
  prefs,
  update,
}: {
  prefs: AgentPrefs;
  update: <K extends keyof AgentPrefs>(k: K, v: AgentPrefs[K]) => void;
}) {
  const a = prefs.appearance;
  const set = <K extends keyof typeof a>(k: K, v: (typeof a)[K]) =>
    update("appearance", { ...a, [k]: v });

  const textSizes = useMemo(
    () => [
      { id: "small" as const, label: "Мелкий" },
      { id: "normal" as const, label: "Обычный" },
      { id: "large" as const, label: "Крупный" },
    ],
    [],
  );
  const densities = useMemo(
    () => [
      { id: "dense" as const, label: "Плотная" },
      { id: "normal" as const, label: "Обычная" },
      { id: "roomy" as const, label: "Просторная" },
    ],
    [],
  );

  return (
    <>
      <SectionHeader title="Внешний вид" />
      <div className="rounded-md border p-4 bg-card space-y-4">
        <Row label="Тема">
          {/* Карточки, а не кнопки: у темы есть характер, и его стоит назвать —
              выбор «Классика / Индастри / Терминал» иначе выглядит абстрактным. */}
          <div className="flex flex-wrap gap-2">
            {THEME_OPTIONS.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => set("theme", t.id)}
                aria-pressed={a.theme === t.id}
                className={cn(
                  "min-w-36 rounded-md border px-3 py-2 text-left transition-colors",
                  a.theme === t.id
                    ? "border-primary bg-accent"
                    : "hover:bg-muted",
                )}
              >
                <span className="block text-sm font-medium">{t.label}</span>
                <span className="block text-xs text-muted-foreground">{t.note}</span>
              </button>
            ))}
          </div>
        </Row>
        <Row label="Размер текста">
          <div className="flex gap-1">
            {textSizes.map((s) => (
              <button
                key={s.id}
                type="button"
                onClick={() => set("textSize", s.id)}
                className={cn(
                  "px-3 py-1.5 rounded-md border text-sm",
                  a.textSize === s.id
                    ? "bg-primary text-primary-foreground border-primary"
                    : "hover:bg-muted",
                )}
              >
                {s.label}
              </button>
            ))}
          </div>
        </Row>
        <Row label="Плотность">
          <div className="flex gap-1">
            {densities.map((d) => (
              <button
                key={d.id}
                type="button"
                onClick={() => set("density", d.id)}
                className={cn(
                  "px-3 py-1.5 rounded-md border text-sm",
                  a.density === d.id
                    ? "bg-primary text-primary-foreground border-primary"
                    : "hover:bg-muted",
                )}
              >
                {d.label}
              </button>
            ))}
          </div>
        </Row>
      </div>
    </>
  );
}
