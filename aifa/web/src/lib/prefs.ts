export type Persona = {
  id: string;
  name: string;
  system_prompt: string;
};

export type ExtractionPrefs = {
  ocrFallback: boolean;
  autoRotate: boolean;
  parallel: number;
  includeCalculated: boolean;
  /** Which OCR backend the upload pipeline should use to produce the .txt
   *  sidecar: "pymupdf" — fast built-in (default), "idp" — IDP-OCR cloud
   *  (per-page PNG, far better tables on bank IFRS, ~15-20× slower). */
  ocrEngine: "pymupdf" | "idp";
};

export type TelegramIntegration = {
  linkedUser: string | null;
  pairCode: string | null;
  pairedAt: number | null;
};

export type IntegrationsPrefs = {
  telegram: TelegramIntegration;
  webhookUrl: string;
  googleDrive: { folderId: string };
  slack: { webhook: string };
};

export type SecurityPrefs = {
  allowExternalProviders: boolean;
  piiMasking: boolean;
  historyRetentionDays: number;
};

/** Темы редизайна (дизайн-хендофф): «Классика», «Индастри», «Терминал».
 *  Терминал — тёмная; отдельного light/dark больше нет, тему выбирает
 *  пользователь явно. Токены — в globals.css, блоки [data-theme]. */
export type ThemeName = "classic" | "industry" | "terminal";

export const THEME_OPTIONS: { id: ThemeName; label: string; note: string }[] = [
  { id: "classic", label: "Классика", note: "книжная, тёплая" },
  { id: "industry", label: "Индастри", note: "чертёжная, стальная" },
  { id: "terminal", label: "Терминал", note: "тёмная, для долгих сессий" },
];

/** Размер текста и плотность — из раздела «Внешний вид» эталона. */
export type TextSize = "small" | "normal" | "large";
export type Density = "dense" | "normal" | "roomy";

export type AppearancePrefs = {
  theme: ThemeName;
  textSize: TextSize;
  density: Density;
  compact: boolean;
};

export type ApiKeys = {
  openrouter: string;
  openai: string;
  anthropic: string;
};

// Backend dispatcher mapping:
// - "finengine" / "finengine_baseline" → full-context (default).
// - "finengine_rag" → СНЯТ 13.08.2026. Бэкенд отвечает на него 400. В типе
//   ниже его больше нет; сохранённый выбор мигрируется на baseline.
// - "finengine_dev" → experimental sandbox (currently behaves like baseline,
//   will host unrestricted extract + web search etc.).
// - "aifa_baseline" / "aifa_dev" → СНЯТЫ 21.08.2026 (легаси: пятая копия
//   реестра, числа в сто раз меньше). Бэкенд отвечает 400, loadPrefs
//   переводит сохранённый выбор на finengine_baseline. Подпись в
//   archLabel остаётся: страница прогонов рисует ИСТОРИЮ.
// - "aifa_gateway" → ход ведёт ПЕТЛЯ ГЕЙТВЕЯ, а не наша (Ш0 миграции). Бэкенд
//   принимает это значение с #98 (api/main.py), но во фронте его не было —
//   попасть на путь можно было только переменной окружения на весь контейнер.
export type ExtractionArch =
  | "finengine"
  | "finengine_baseline"
  | "finengine_dev"
  | "aifa_gateway";

/** Analyst role for metric extraction — mirrors the backend
 * roles/access.py ROLE_LABELS. "other" = «Общая» (full cell metric set). */
export type ExtractionRole = "other" | "risks" | "baifp" | "die" | "cib" | "dgr";

export const ROLE_OPTIONS: { id: ExtractionRole; label: string; note?: string }[] = [
  { id: "other", label: "Общая", note: "Все показатели ячейки (по умолчанию)" },
  { id: "risks", label: "Риски" },
  { id: "baifp", label: "БАИФП" },
  { id: "die", label: "ДИЭ" },
  { id: "cib", label: "CIB" },
  { id: "dgr", label: "ДГР" },
];

/** Role→layout options used by the extraction-table renderer (extracted-pane).
 * Superset of ROLE_OPTIONS with an extra "all" pseudo-role (admin: full metric
 * set + segments). Kept separate from ROLE_OPTIONS so the composer/header role
 * picker (which drives the *extraction* role, not the table layout) is unaffected. */
export const EXTRACTION_ROLE_OPTIONS = [
  { id: "other", label: "Общая" },
  { id: "risks", label: "Риски" },
  { id: "baifp", label: "БАИФП" },
  { id: "die", label: "ДИЭ" },
  { id: "cib", label: "CIB" },
  { id: "dgr", label: "ДГР" },
  { id: "all", label: "Все показатели" },
] as const;

/** Canonicalise a free-form / legacy role identifier to one of the ids in
 * EXTRACTION_ROLE_OPTIONS. Handles the mglob→dgr rename and Cyrillic aliases;
 * unknown values fall back to "other". */
export function normalizeExtractionRole(role?: string | null): string {
  const value = (role || "").trim().toLowerCase();
  if (value === "mglob" || value === "мглоб" || value === "drg" || value === "дгр") {
    return "dgr";
  }
  if (EXTRACTION_ROLE_OPTIONS.some((r) => r.id === value)) return value;
  return "other";
}

/**
 * Роль, под которую строится ТАБЛИЦА, — один вход для всех, кто её выбирает.
 *
 * 🔴 ПОЧЕМУ ОТДЕЛЬНАЯ ФУНКЦИЯ. Роль нормализовали в двух местах по РАЗНЫМ
 * спискам: подпись кнопки — по `ROLE_OPTIONS` (там нет «all»), а запрос
 * раскладки — по `normalizeExtractionRole` (там «all» есть). С `role: "all"`
 * в localStorage кнопка писала «Общая», а на сервер уходила раскладка «Все
 * показатели» — которой в бэкенде нет, и таблица молча оставалась общей.
 *
 * Здесь список ОДИН: всё, чего нет в меню (включая «all» и любую будущую
 * псевдороль), — это обычная таблица, а не запрос за несуществующей раскладкой.
 */
export function tableRoleOf(value: string | undefined | null): ExtractionRole {
  const id = normalizeExtractionRole(value);
  return ROLE_OPTIONS.find((r) => r.id === id)?.id ?? "other";
}

export type AgentPrefs = {
  model: string;
  /** Per-metric pipeline choice. "finengine" = full-context single-shot.
   * "aifa" = per-metric table reranker → top-3 → LLM. Default finengine. */
  arch: ExtractionArch;
  /** Analyst role for metric extraction (other/risks/baifp/die/cib/dgr).
   * Passed into the chat-extraction flow → fe_extract_metrics scopes its
   * metric set to this role. "other" = full cell set (default). */
  role: ExtractionRole;
  fallbackModels: string[];
  /** Legacy single temperature — kept for migration. Real callers should
   *  use the split fields below, which expose the extract vs chat/synth
   *  trade-off directly. */
  temperature: number;
  /** Temperature for /extract — defaults to 0 (deterministic per-metric). */
  extractTemperature: number;
  /** Temperature for the chat + analysis path —
   *  defaults to 0.6 (some creativity in narrative). */
  chatTemperature: number;
  /** Reasoning effort for models that support it (GLM-5.x, o1/o3 family).
   * Models without reasoning channels ignore this. "high" trades latency
   * and tokens for accuracy on complex extraction / chain steps. */
  reasoningEffort: "low" | "medium" | "high";
  maxTurns: number;
  webSearch: boolean;
  activePersonaId: string | null;
  defaultPersonaId: string | null;
  personas: Persona[];
  extraction: ExtractionPrefs;
  security: SecurityPrefs;
  appearance: AppearancePrefs;
  integrations: IntegrationsPrefs;
  apiKeys: ApiKeys;
  /** Служебная метка разового перевода на путь миграции (см. loadPrefs).
   *  Не настройка: нужна, чтобы ручной выбор пути не затирался при перезагрузке. */
  _archMigratedToGateway?: boolean;
  /** Метка разового включения «Поиска в интернете» (см. loadPrefs). */
  _webSearchOnByDefault?: boolean;
};

// Model labels are user-facing — we hide both the underlying model name and
// the provider on purpose. The "Базовая" alias is what every user sees;
// premium aliases (Kimi, GLM) are gated by per-user grants.
export const MODEL_OPTIONS: { id: string; label: string; note?: string; key: string }[] = [
  { id: "qwen/qwen3.6-plus", label: "Базовая", key: "base", note: "Универсальная модель — работает с любыми отчётами" },
  { id: "moonshotai/kimi-k2.5", label: "Kimi K2.5", key: "kimi", note: "Точнее на нефинансовых отчётах ≤90 стр" },
  { id: "z-ai/glm-5.1", label: "GLM 5.1", key: "glm", note: "Для небольших отчётов" },
  { id: "GigaChat-Max", label: "GigaChat Max", key: "gigachat", note: "Российская модель — для корпоративных задач" },
  { id: "GigaChat-Pro", label: "GigaChat Pro", key: "gigachat", note: "Российская модель — стандартный вариант" },
  { id: "GigaChat-2-Max", label: "GigaChat 2 Max", key: "gigachat", note: "Российская модель — новая линейка v2" },
];

// IDs of the models that are always available, regardless of per-user grants.
export const PUBLIC_MODEL_IDS = new Set(["qwen/qwen3.6-plus"]);

export const DEFAULT_PREFS: AgentPrefs = {
  model: "qwen/qwen3.6-plus",
  // Умолчание — ПУТЬ МИГРАЦИИ. Иначе новый путь проверялся бы только тогда,
  // когда о нём вспомнят, а вспоминать приходилось человеку: три живых прогона
  // подряд ушли старым путём просто потому, что переключить забывали (а до
  // 05.08 ещё и было нечем). Умолчание превращает обычную работу в проверку.
  // Аварийный выход — тумблер в шапке /v2, один клик, без деплоя.
  arch: "aifa_gateway",
  role: "other",
  fallbackModels: [],
  temperature: 0.2,
  extractTemperature: 0,
  chatTemperature: 0.6,
  reasoningEffort: "medium",
  maxTurns: 5,
  webSearch: true,
  activePersonaId: null,
  defaultPersonaId: null,
  personas: [],
  extraction: {
    ocrFallback: true,
    autoRotate: true,
    parallel: 16,
    includeCalculated: true,
    // Layer 1.5 bench (6 docs) showed IDP-OCR wins decisively on table
    // extraction (5–50× more tables, ∞ on banks where pdfplumber returns
    // zero). Trade-off: ~10–15× slower upload. Users who need fast iteration
    // on simple PDFs can flip to "pymupdf" in Settings or via the composer.
    ocrEngine: "idp",
  },
  security: {
    allowExternalProviders: true,
    piiMasking: false,
    historyRetentionDays: 30,
  },
  appearance: {
    theme: "classic",
    textSize: "normal",
    density: "normal",
    compact: false,
  },
  integrations: {
    telegram: { linkedUser: null, pairCode: null, pairedAt: null },
    webhookUrl: "",
    googleDrive: { folderId: "" },
    slack: { webhook: "" },
  },
  apiKeys: {
    openrouter: "",
    openai: "",
    anthropic: "",
  },
};

const KEY = "finengine:prefs:v1";

function deepMerge<T>(base: T, partial: Partial<T>): T {
  const out = { ...base } as Record<string, unknown>;
  for (const [k, v] of Object.entries(partial ?? {})) {
    const bv = (base as Record<string, unknown>)[k];
    if (v && typeof v === "object" && !Array.isArray(v) && bv && typeof bv === "object" && !Array.isArray(bv)) {
      out[k] = deepMerge(bv as Record<string, unknown>, v as Record<string, unknown>);
    } else {
      out[k] = v;
    }
  }
  return out as T;
}

export function loadPrefs(): AgentPrefs {
  if (typeof window === "undefined") return DEFAULT_PREFS;
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return DEFAULT_PREFS;
    const merged = deepMerge(DEFAULT_PREFS, JSON.parse(raw));
    // Миграция тем: до редизайна хранились system/light/dark. Тёмная →
    // «Терминал», светлая и системная → «Классика» (дефолт продукта).
    const savedTheme = merged.appearance?.theme as string | undefined;
    if (savedTheme && !THEME_OPTIONS.some((t) => t.id === savedTheme)) {
      merged.appearance.theme = savedTheme === "dark" ? "terminal" : "classic";
    }
    // Migrate stale model ids — earlier versions defaulted to "claude-sonnet-4-5"
    // which was never a valid OpenRouter model id. Snap back to current default
    // if the saved value is no longer in the supported list.
    if (!MODEL_OPTIONS.some((m) => m.id === merged.model)) {
      merged.model = DEFAULT_PREFS.model;
    }
    // Migrate "finengine" → "finengine_baseline" (renamed when "finengine_dev"
    // arch was first introduced).
    if (merged.arch === "finengine") {
      merged.arch = "finengine_baseline";
    }
    // RAG-путь снят 13.08.2026 вместе с движком (бэкенд отвечает на него 400).
    // Без миграции сохранённый выбор пережил бы правку и упёрся бы в 400 при
    // первом же извлечении: умолчание до localStorage не доходит, оно только
    // под ним. Прежняя миграция "finengine_dev" → "finengine_rag" СНЯТА, а не
    // перенаправлена: она отрабатывала разовое переименование и с тех пор у
    // всех уже сработала, а сегодня "finengine_dev" — это живой выбор
    // «экспериментальная песочница», и уводить с него нельзя.
    // Сравнение строкой, а не по типу: из ExtractionArch значение уже убрано,
    // но в localStorage оно лежит — ровно поэтому миграция и нужна.
    if ((merged.arch as string) === "finengine_rag") {
      merged.arch = "finengine_baseline";
    }
    // Тем же порядком сняты "aifa_baseline" / "aifa" / "aifa_dev" (21.08.2026):
    // они считали по ПЯТОЙ копии реестра и после перевода доходностей в
    // проценты отдавали числа ровно в сто раз меньше. Бэкенд отвечает на них
    // 400; без миграции сохранённый выбор упёрся бы в этот отказ при первом же
    // извлечении. Сравнение строкой — значения уже убраны из типа.
    // Уводим на ПЕТЛЮ ЯДРА, а не на Finengine: человек, выбравший «AIFA»,
    // хотел AIFA — сегодня это `aifa_gateway`. Отправить его на Finengine
    // значило бы молча сменить продукт, а не починить снятый пункт.
    if (["aifa_baseline", "aifa", "aifa_dev"].includes(merged.arch as string)) {
      merged.arch = "aifa_gateway";
    }
    // Разовый перевод на путь миграции. Правки одного лишь DEFAULT_PREFS НЕ
    // ХВАТАЕТ: loadPrefs мержит сохранённое ПОВЕРХ дефолта, и у того, кто уже
    // пользовался стендом, в localStorage лежит старое значение — умолчание до
    // него не доходит вовсе. Именно так «переключение по умолчанию» и
    // оказывается переключением никого.
    //
    // РОВНО ОДИН РАЗ, по метке: иначе ручной выбор «Finengine» в тумблере
    // затирался бы при следующей загрузке страницы, и человек не смог бы
    // остаться на старом пути, даже нажав кнопку.
    if (!merged._archMigratedToGateway) {
      merged._archMigratedToGateway = true;
      if (merged.arch === "finengine_baseline") merged.arch = "aifa_gateway";
      try {
        localStorage.setItem(KEY, JSON.stringify(merged));
      } catch {
        /* приватный режим — метка не сохранится, переведёт ещё раз. Не беда. */
      }
    }
    // Разовое включение «Поиска в интернете» (08.10.2026): умолчание стало
    // «включён», но savePrefs пишет настройки целиком, и у каждого, кто уже
    // открывал приложение, сохранено прежнее false — «не трогал» от «выключил
    // сам» по нему не отличить. Поэтому один раз включаем у всех и ставим
    // метку; дальше выбор человека не трогаем — «выключен» обязан значить
    // выключен.
    if (!merged._webSearchOnByDefault) {
      merged._webSearchOnByDefault = true;
      merged.webSearch = true;
      try {
        localStorage.setItem(KEY, JSON.stringify(merged));
      } catch {
        /* приватный режим — включит ещё раз при следующей загрузке */
      }
    }
    return merged;
  } catch {
    return DEFAULT_PREFS;
  }
}

export function savePrefs(p: AgentPrefs): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(KEY, JSON.stringify(p));
  // Broadcast in-tab so listeners (e.g. ThemeBridge) can react. Browsers'
  // own "storage" event only fires across tabs, not within the writer tab.
  window.dispatchEvent(new CustomEvent<AgentPrefs>("finengine:prefs", { detail: p }));
}

/** Human-readable label for an extraction role identifier. Normalises legacy
 * aliases (mglob→dgr, Cyrillic) first, then looks up the label. Unknown values
 * fall back to «Общая» so legacy prefs render sensibly. */
export function roleLabel(role: string | undefined | null): string {
  const normalized = normalizeExtractionRole(role);
  return EXTRACTION_ROLE_OPTIONS.find((r) => r.id === normalized)?.label ?? "Общая";
}

/** Human-readable label for an extraction arch identifier. Used by the
 * metrics panel cards so each run is tagged with its agent. Unknown
 * values pass through unchanged so a future arch added on the backend
 * still renders sensibly. Returns "" for missing arch (legacy localStorage
 * runs from before v78 — UI shows a "не определён" placeholder for those). */
export function archLabel(arch: string | undefined | null): string {
  switch (arch) {
    case "finengine":
    case "finengine_baseline":
      return "Finengine Baseline";
    // Режим снят 13.08.2026, но подпись остаётся: страница прогонов рисует
    // ИСТОРИЮ, и у старых прогонов в базе стоит именно этот arch.
    case "finengine_rag":
      return "Finengine RAG";
    case "finengine_dev":
      return "Finengine DEV";
    case "aifa_baseline":
      return "AIFA Baseline";
    case "aifa_dev":
      return "AIFA DEV";
    case "aifa_gateway":
      return "AIFA Gateway";
    default:
      return arch || "";
  }
}
