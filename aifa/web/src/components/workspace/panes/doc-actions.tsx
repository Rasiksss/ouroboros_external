"use client";

import { useEffect, useState } from "react";
import { ChevronDown } from "lucide-react";
import { getDocumentStatus, type DocumentSummary } from "@/lib/api";
import {
  ROLE_OPTIONS,
  loadPrefs,
  savePrefs,
  tableRoleOf,
  type ExtractionRole,
} from "@/lib/prefs";
import { cn } from "@/lib/utils";

/**
 * Действия над активным документом — компактная строка над вкладками
 * извлечённого. Три входа пользователя: извлечение по реестру (с выбором роли)
 * и два вида анализа.
 *
 * Все три — это ОБЫЧНЫЙ ХОД ЧАТА с пришпиленным тулом, а не отдельные ручки:
 * тот же контур, что и слова в чате (живая карточка извлечения, остров
 * «Прогресс», Excel-артефакт), но выбор навыка и его аргументов не отдаётся
 * модели — бэкенд зовёт тул напрямую (`_DIRECT_PINNABLE`).
 */

/** Кусок рантайма хода (use-chat-run), нужный кнопкам. Панель держит его
 *  снаружи: подписка на прогон должна быть ОДНА на чат, поэтому свой
 *  useChatRun здесь заводить нельзя — две копии проекта затирали бы друг
 *  друга в localStorage. */
export type DocActionsRun = {
  streaming: boolean;
  send: (
    message: string,
    opts?: { pinnedTool?: string; pinnedToolArgs?: Record<string, unknown> },
  ) => Promise<void>;
};

export function DocActions({
  doc,
  run,
}: {
  doc: DocumentSummary | null;
  run?: DocActionsRun | null;
}) {
  const [rolesOpen, setRolesOpen] = useState(false);
  const text = useDocTextState(doc?.doc_id ?? "");
  const [role, setRole] = useState<ExtractionRole>(() => tableRoleOf(loadPrefs().role));

  // Роль выбирается в меню «+» композера (prefs) — подписываемся на смену,
  // иначе подпись кнопки и аргументы пина отставали бы от выбора пользователя.
  useEffect(() => {
    const onPrefs = () => setRole(tableRoleOf(loadPrefs().role));
    window.addEventListener("finengine:prefs", onPrefs);
    return () => window.removeEventListener("finengine:prefs", onPrefs);
  }, []);

  const blocker = blockerOf(run, doc, text);
  const blocked = blocker !== null;
  const label = doc ? docLabel(doc) : "";
  const roleName = ROLE_OPTIONS.find((r) => r.id === role)?.label ?? "Общая";

  // Анализ и выбор роли живут в чате — здесь оставлено только извлечение
  // (решение владельца продукта: рабочая область не должна дублировать чат).
  // СЛОВАМИ, БЕЗ ПИНА (#166): вместе с монолитом ушёл и пин тула с аргументами.
  // Роль и документ не потерялись — они названы во фразе, и по ней маршрут
  // выбирает модель.
  const fire = () => {
    if (blocked || !run || !doc) return;
    void run.send(`Извлеки показатели по реестру роли «${roleName}» из «${label}»`);
  };

  return (
    <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-[var(--color-divider)] px-4 py-2">
      {/* Кнопка со стрелкой: справа выбор роли. Роль определяет и НАБОР
          показателей при запуске, и раскладку таблицы под ним. */}
      {/* ОДНА рамка на кнопку и стрелку: внутренней границы между ними нет. */}
      <div className="relative flex shrink-0 items-stretch rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] transition-colors hover:bg-[var(--color-accent-100)]">
        <button
          type="button"
          onClick={fire}
          disabled={blocked}
          title={
            blocker ??
            `Реестр показателей по документу «${label}», роль «${roleName}» — ход в чате`
          }
          className={cn(
            "py-1 pl-2.5 pr-1.5 text-[10px] uppercase tracking-[0.12em] text-[var(--color-accent-700)]",
            "transition-colors disabled:cursor-not-allowed disabled:opacity-45",
          )}
        >
          Извлечь метрики
        </button>
        <button
          type="button"
          onClick={() => setRolesOpen((v) => !v)}
          title={`Роль извлечения: ${roleName}`}
          className={cn(
            "grid w-[22px] place-items-center pr-1",
            "text-[var(--color-accent-700)] transition-colors",
          )}
        >
          <ChevronDown className={cn("size-3 transition-transform", rolesOpen && "rotate-180")} />
        </button>

        {rolesOpen && (
          <div className="absolute left-0 top-[30px] z-40 w-[190px] rounded-[var(--radius-token-md)] border border-[var(--color-divider)] bg-[var(--color-surface)] py-1 shadow-lg">
            {ROLE_OPTIONS.map((r) => (
              <button
                key={r.id}
                type="button"
                // Таблицу ниже перестраивает подписка на `finengine:prefs`, а
                // не колбэк отсюда: колбэк был ВТОРЫМ входом в тот же запрос, и
                // клик по роли отправлял POST /metrics/format-role дважды.
                onClick={() => {
                  savePrefs({ ...loadPrefs(), role: r.id });
                  setRole(r.id);
                  setRolesOpen(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] transition-colors hover:bg-[var(--color-neutral-100)]",
                  r.id === role && "bg-[var(--color-accent-100)] text-[var(--color-accent-700)]",
                )}
              >
                {r.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <span className="shrink-0 text-[11.5px] text-[var(--color-neutral-600)]">{roleName}</span>

      {(blocker || text === "unknown") && (
        <span className="ml-auto max-w-[42ch] text-[10.5px] italic leading-snug text-[var(--color-neutral-500)]">
          {blocker ?? UNKNOWN_NOTE}
        </span>
      )}
    </div>
  );
}

/** Одна причина, по которой действия недоступны, — иначе кнопка гаснет молча. */
function blockerOf(
  run: DocActionsRun | null | undefined,
  doc: DocumentSummary | null,
  text: TextState,
): string | null {
  if (!run) {
    return "Запуск хода в эту зону ещё не проброшен: рантайм чата живёт в панели чата и сюда не передан.";
  }
  if (!doc) return "Документ не выбран — выберите его в списке выше.";
  if (run.streaming) return "Идёт ход — дождитесь ответа агента.";
  if (text === "checking") return "Проверяю, распознан ли текст документа…";
  if (text === "ocr") return "Идёт распознавание документа (OCR) — текста для работы пока нет.";
  return null;
}

const UNKNOWN_NOTE =
  "Статус распознавания недоступен — запуск разрешён, агент дождётся текста сам.";

function docLabel(d: DocumentSummary): string {
  return d.company_name?.trim() || d.filename || d.doc_id;
}

type TextState = "checking" | "ready" | "ocr" | "unknown";

/**
 * Готовность текста документа. Событий о завершении распознавания бэкенд не
 * шлёт — единственный путь тот же, что на «/»: опрос `/documents/{id}/status`.
 * Опрашиваем, только пока документ распознаётся; готовый больше не дёргаем.
 */
function useDocTextState(docId: string): TextState {
  // Ответ храним вместе с id документа, а «проверяю» ВЫВОДИМ: пока для текущего
  // документа ответа нет, состояние и так известно — иначе пришлось бы сбрасывать
  // его setState прямо в теле эффекта (лишний каскад рендеров).
  const [known, setKnown] = useState<{ docId: string; state: TextState } | null>(null);

  useEffect(() => {
    if (!docId) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const st = await getDocumentStatus(docId);
        if (!alive) return;
        setKnown({ docId, state: st.processing ? "ocr" : "ready" });
        if (st.processing) timer = setTimeout(tick, 2500);
      } catch {
        // Статус недоступен ≠ документ не готов. Гейт здесь — удобство: тул
        // всё равно ждёт распознавания сам, поэтому не запрещаем запуск.
        if (alive) setKnown({ docId, state: "unknown" });
      }
    };
    void tick();

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [docId]);

  if (!docId) return "unknown";
  return known?.docId === docId ? known.state : "checking";
}

