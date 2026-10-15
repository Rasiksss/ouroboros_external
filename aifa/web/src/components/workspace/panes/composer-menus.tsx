"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronUp, Check, FileUp, Globe, Mail, Cpu, UserRound } from "lucide-react";
import {
  MODEL_OPTIONS,
  ROLE_OPTIONS,
  loadPrefs,
  savePrefs,
  type ExtractionRole,
} from "@/lib/prefs";
import type { SendOpts } from "@/components/workspace/use-chat-run";
import { cn } from "@/lib/utils";

/**
 * Меню композера нового экрана: «+» слева (настройки хода) и стрелка у
 * «Отправить» (шорткаты навыков).
 *
 * Шорткат — это МОДЕЛЬ ВХОДОВ: кнопка фиксирует и навык, и его аргументы,
 * бэкенд зовёт тул напрямую, модель ничего не выбирает. Поэтому здесь нет
 * «распознавания интенции» — каждая кнопка собирает готовый pinned-ход.
 */

/** Пункты «+»: что реально подключено, а что нет — говорим честно в title. */
export function PlusMenu({
  disabled,
  onAttach,
}: {
  disabled?: boolean;
  onAttach: () => void;
}) {
  const [open, setOpen] = useState(false);
  const boxRef = useOutsideClose(() => setOpen(false));
  // Настройки читаем на каждое открытие: их мог поменять диалог настроек
  // или соседняя вкладка — кэшировать в состоянии значит врать.
  const [, force] = useState(0);
  const prefs = loadPrefs();

  const setRole = (id: ExtractionRole) => {
    savePrefs({ ...loadPrefs(), role: id });
    force((v) => v + 1);
  };
  const toggleWeb = () => {
    const p = loadPrefs();
    savePrefs({ ...p, webSearch: !p.webSearch });
    force((v) => v + 1);
  };

  return (
    <div ref={boxRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={disabled}
        title="Настройки хода: файл, роль, поиск в интернете"
        className={cn(
          "grid size-[34px] shrink-0 place-items-center rounded-[var(--radius-token-sm)] text-[var(--color-neutral-600)] transition-colors hover:bg-[var(--color-neutral-100)] disabled:cursor-not-allowed disabled:opacity-55",
          open && "bg-[var(--color-neutral-100)] text-[var(--color-text)]",
        )}
      >
        <PlusGlyph />
      </button>

      {open && (
        <div className="absolute bottom-[40px] left-0 z-40 w-[300px] rounded-[var(--radius-token-md)] border border-[var(--color-divider)] bg-[var(--color-surface)] py-1 shadow-lg">
          <MenuButton
            icon={<FileUp className="size-3.5" />}
            label="Прикрепить файл (PDF)"
            note="документ появится в панели исходника"
            onClick={() => {
              setOpen(false);
              onAttach();
            }}
          />

          <MenuDivider label="Роль извлечения" />
          {ROLE_OPTIONS.map((r) => (
            <MenuButton
              key={r.id}
              icon={
                prefs.role === r.id ? (
                  <Check className="size-3.5 text-[var(--color-accent-700)]" />
                ) : (
                  <UserRound className="size-3.5 opacity-40" />
                )
              }
              label={r.label}
              note={r.note}
              active={prefs.role === r.id}
              onClick={() => setRole(r.id)}
            />
          ))}

          <MenuDivider label="Ход" />
          <MenuButton
            icon={
              <Globe
                className={cn(
                  "size-3.5",
                  prefs.webSearch ? "text-[var(--color-accent-700)]" : "opacity-40",
                )}
              />
            }
            label={`Поиск в интернете: ${prefs.webSearch ? "разрешён" : "выключен"}`}
            note="выключен — агент работает только по документам"
            onClick={toggleWeb}
          />
          <MenuButton
            icon={<Cpu className="size-3.5 opacity-40" />}
            label={`Модель: ${MODEL_OPTIONS.find((m) => m.id === prefs.model)?.label ?? "Базовая"}`}
            note="выбор модели для чата подключается на гейтвее — пока фиксирована"
            disabled
          />
          <MenuButton
            icon={<Mail className="size-3.5 opacity-40" />}
            label="Файл с почты"
            note="как в FinEngine — приёмник почты на этом стенде не развёрнут"
            disabled
          />
        </div>
      )}
    </div>
  );
}

export type ShortcutFire = (message: string, opts?: SendOpts) => void;

/**
 * Кнопка отправки со стрелкой ВНУТРИ: слева «Отправить», справа стрелка без
 * собственной рамки — одна кнопка, а не две рядом. Стрелка раскрывает панель
 * шорткатов прямо над кнопкой.
 *
 * Шорткаты детерминированные: Анализ → fe_analyze(depth) напрямую, Извлечение
 * по реестру → fe_extract_metrics(doc_id, role) напрямую; «Произвольное» —
 * обычный ход без пина, ответ таблицей в чате.
 */
export function SendControl({
  doc,
  canSend,
  onSend,
  onFire,
}: {
  /** Активный документ рабочего экрана — по нему бьют шорткаты. */
  doc: { doc_id: string; label: string } | null;
  /** Есть что отправить (непустой черновик и живой чат). */
  canSend: boolean;
  onSend: () => void;
  onFire: ShortcutFire;
}) {
  const [open, setOpen] = useState(false);
  /** На какую из двух пилюль наведён курсор — от этого зависит, чья пара
   *  выехала сверху. */
  const [hover, setHover] = useState<"extract" | "analyze" | null>(null);
  // Закрываем пару НЕ мгновенно: путь курсора от «Анализа» к «Быстрому» идёт
  // по диагонали и на миг выходит за обе кнопки. Без отсрочки пара исчезала
  // ровно тогда, когда до неё тянулись. Возврат внутрь отменяет закрытие.
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const holdHover = useCallback((next: "extract" | "analyze" | null) => {
    if (closeTimer.current) {
      clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    if (next === null) {
      closeTimer.current = setTimeout(() => setHover(null), 260);
    } else {
      setHover(next);
    }
  }, []);
  useEffect(
    () => () => {
      if (closeTimer.current) clearTimeout(closeTimer.current);
    },
    [],
  );
  const boxRef = useOutsideClose(() => {
    setOpen(false);
    setHover(null);
  });
  const roleId = loadPrefs().role;
  const roleName = ROLE_OPTIONS.find((r) => r.id === roleId)?.label ?? "Общая";

  const needDoc = !doc;
  const fire = (message: string, opts?: SendOpts) => {
    if (closeTimer.current) clearTimeout(closeTimer.current);
    setOpen(false);
    setHover(null);
    onFire(message, opts);
  };

  return (
    <div ref={boxRef} className="relative">
      <div
        className={cn(
          "flex items-stretch overflow-hidden rounded-[var(--radius-token-md)] border transition-colors",
          open
            ? "border-[var(--color-accent)]"
            : "border-[var(--color-accent)] hover:bg-[var(--color-accent-100)]",
        )}
      >
        <button
          type="button"
          onClick={onSend}
          disabled={!canSend}
          className="px-4 py-1.5 text-[13px] text-[var(--color-accent-700)] transition-colors disabled:cursor-not-allowed disabled:opacity-45"
        >
          Отправить
        </button>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          title="Анализ и извлечение одной кнопкой"
          className="grid w-[26px] shrink-0 place-items-center text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)]"
        >
          <ChevronUp className={cn("size-3.5 transition-transform", open && "rotate-180")} />
        </button>
      </div>

      {open && (
        // Только две пилюли и ничего больше. Вторая пара выезжает НАД той, на
        // которую навели, и заменяет предыдущую — поэтому обе пары всегда в
        // разметке, а видимость переключается классами: так выход и вход
        // анимируются в обе стороны, а не мигают пересозданием.
        <div
          className="absolute bottom-[46px] right-0 z-40 flex items-end gap-2"
          onMouseLeave={() => holdHover(null)}
        >
          <PillGroup
            label="Извлечение"
            active={hover === "extract"}
            onEnter={() => holdHover("extract")}
            disabled={needDoc}
            hint={needDoc ? NEED_DOC : undefined}
            items={[
              {
                label: "Произвольное",
                hint: "агент сам решит, что извлечь; ответ таблицей в чате",
                onClick: () =>
                  fire(
                    `Извлеки ключевые финансовые показатели из «${doc!.label}» и покажи таблицей в чате.`,
                  ),
              },
              {
                label: "Реестр",
                hint: `набор роли «${roleName}» — таблица в рабочей области + Excel`,
                onClick: () =>
                  fire(`Извлеки показатели по реестру роли «${roleName}» из «${doc!.label}»`, {
                    pinnedTool: "fe_extract_metrics",
                    pinnedToolArgs: {
                      doc_id: doc!.doc_id,
                      role: roleId,
                      // Ш13: прежнее include_segments тул игнорировал — решала
                      // станция по типу компании. «auto» = то же поведение,
                      // но теперь это сказано аргументом, а не молчанием.
                      segments: "auto",
                    },
                  }),
              },
            ]}
          />
          <PillGroup
            label="Анализ"
            active={hover === "analyze"}
            onEnter={() => holdHover("analyze")}
            disabled={needDoc}
            hint={needDoc ? NEED_DOC : undefined}
            items={[
              {
                label: "Полный",
                hint: "динамика, сегменты, долг, риски",
                onClick: () =>
                  fire(`Полный разбор по «${doc!.label}»`, {
                    pinnedTool: "fe_analyze",
                    pinnedToolArgs: { doc_id: doc!.doc_id, depth: "full" },
                  }),
              },
              {
                label: "Быстрый",
                hint: "главное в пяти пунктах",
                onClick: () =>
                  fire(`Быстрый обзор по «${doc!.label}»`, {
                    pinnedTool: "fe_analyze",
                    pinnedToolArgs: { doc_id: doc!.doc_id, depth: "quick" },
                  }),
              },
            ]}
          />
        </div>
      )}
    </div>
  );
}

// ─── мелкая начинка меню ───────────────────────────────────────────────

function useOutsideClose(onClose: () => void) {
  const ref = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [onClose]);
  return ref;
}

/** Плюс рисуем сами: lucide Plus уже занят кнопкой прикрепления в старом
 *  экране с другим размером — здесь важно совпасть с сеткой композера. */
function PlusGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden>
      <path d="M8 2v12M2 8h12" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  );
}

function MenuDivider({ label }: { label: string }) {
  return (
    <div className="mt-1 border-t border-[var(--color-divider)] px-3 pb-0.5 pt-1.5 text-[9.5px] uppercase tracking-[0.14em] text-[var(--color-neutral-500)]">
      {label}
    </div>
  );
}

function MenuButton({
  icon,
  label,
  note,
  onClick,
  disabled,
  active,
}: {
  icon: React.ReactNode;
  label: string;
  note?: string;
  onClick?: () => void;
  disabled?: boolean;
  active?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={note}
      className={cn(
        "flex w-full items-start gap-2 px-3 py-1.5 text-left transition-colors",
        disabled
          ? "cursor-not-allowed opacity-50"
          : "hover:bg-[var(--color-neutral-100)]",
        active && "bg-[var(--color-accent-100)]",
      )}
    >
      <span className="mt-[2px] shrink-0 text-[var(--color-neutral-600)]">{icon}</span>
      <span className="min-w-0">
        <span className="block truncate text-[12.5px] text-[var(--color-text)]">{label}</span>
        {note && (
          <span className="block truncate text-[10.5px] leading-snug text-[var(--color-neutral-500)]">
            {note}
          </span>
        )}
      </span>
    </button>
  );
}

const NEED_DOC = "Нужен документ — прикрепите отчёт или выберите его в панели исходника";

/**
 * Пара «категория + её варианты». Варианты живут в разметке всегда и
 * показываются классами: при переходе курсора с одной категории на другую
 * прошлая пара уезжает вниз и гаснет, новая выезжает — без пересоздания узлов,
 * поэтому анимация идёт в обе стороны.
 *
 * Список подвешен НАД категорией. Зазор между ними — ПАДДИНГ самого списка
 * (`pb-1.5`), а не внешний отступ: паддинг входит в область наведения, поэтому
 * курсор идёт от категории к варианту по сплошному «мостику». С внешним
 * отступом там была мёртвая полоса в 6px — курсор попадал в неё, группа
 * считала, что её покинули, и пара исчезала ровно на пути к ней. `min-w-full`
 * гарантирует, что мостик не уже самой категории.
 */
function PillGroup({
  label,
  items,
  active,
  onEnter,
  disabled,
  hint,
}: {
  label: string;
  items: { label: string; hint: string; onClick: () => void }[];
  active: boolean;
  onEnter: () => void;
  disabled?: boolean;
  hint?: string;
}) {
  return (
    <div className="relative" onMouseEnter={onEnter}>
      <div
        className={cn(
          "absolute bottom-full left-1/2 flex min-w-full -translate-x-1/2 flex-col items-center gap-1.5 pb-1.5",
          "transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none",
          active
            ? "translate-y-0 opacity-100"
            : "pointer-events-none translate-y-1.5 opacity-0",
        )}
      >
        {items.map((it) => (
          <Pill key={it.label} label={it.label} title={disabled ? hint : it.hint} onClick={it.onClick} disabled={disabled} sub />
        ))}
      </div>
      <Pill
        label={label}
        title={disabled ? hint : undefined}
        active={active}
        // Стрелка вверх у категории: видно, что над ней раскроются варианты.
        caret
        // Клик по категории — для тачпада без наведения: раскрывает её пару.
        onClick={onEnter}
      />
    </div>
  );
}

/** Полуовальная кнопка: при наведении слегка подрастает и подсвечивается.
 *  `caret` — стрелка вверх сбоку от подписи: знак, что у кнопки есть варианты. */
function Pill({
  label,
  title,
  onClick,
  active,
  disabled,
  sub,
  caret,
}: {
  label: string;
  title?: string;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
  sub?: boolean;
  caret?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "inline-flex items-center gap-1.5 whitespace-nowrap rounded-full border px-4 py-1.5 text-[12.5px] shadow-sm",
        "transition-[transform,background-color,border-color,color] duration-150 ease-out",
        "hover:scale-[1.06] motion-reduce:transition-none motion-reduce:hover:scale-100",
        "disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:scale-100",
        sub
          ? "border-[var(--color-divider)] bg-[var(--color-surface)] text-[var(--color-neutral-700)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent-700)]"
          : active
            ? "scale-[1.06] border-[var(--color-accent)] bg-[var(--color-accent-100)] text-[var(--color-accent-700)]"
            : "border-[var(--color-divider)] bg-[var(--color-surface)] text-[var(--color-text)] hover:border-[var(--color-accent)] hover:text-[var(--color-accent-700)]",
      )}
    >
      {label}
      {caret && (
        <ChevronUp
          className={cn(
            "size-3 shrink-0 transition-opacity duration-150",
            active ? "opacity-100" : "opacity-45",
          )}
          aria-hidden
        />
      )}
    </button>
  );
}
