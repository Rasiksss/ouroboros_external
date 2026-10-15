"use client";

import { Search } from "lucide-react";
import { toast } from "sonner";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  areaExtractionOf,
  extractionFromStoredRun,
  extractionsForDoc,
  findRunningExtraction,
  rowsFromExtraction,
  type ExtractionMessage,
  type MetricRow,
} from "@/lib/projects";
import { fetchLastRun, formatMetricsForRole, type FormattedTable } from "@/lib/api";
import {
  dieSegmentsTable,
  dieValuesTable,
  filterFormattedTable,
  filterRowsByOutputEnums,
  fmtFormattedCell,
  formattedCellValue,
  formattedDataRowCount,
  isFormattedNumeric,
  isGenericTableRole,
  roleLayoutMissingNote,
  visibleFormattedColumns,
} from "@/lib/formatted-table";
import { scopeRowsToSlice } from "@/lib/table-slice";
import { KIND_TABS, useWorkspace } from "../workspace-context";
import { DocActions, type DocActionsRun } from "./doc-actions";
import { ExtractedTable, type ExtractedVariant } from "./extracted-table";
import { roleLabel, tableRoleOf, type ExtractionRole } from "@/lib/prefs";
import { cn } from "@/lib/utils";

/**
 * Зона «Документ»: выбор документа (задел под мультидокумент — список берётся
 * из общего состояния), действия над ним, вкладки извлечённого по ВИДУ
 * ИСХОДНИКА и таблица.
 *
 * Таблица наполняется из прогонов извлечения, уже осевших в проекте. Запуск —
 * только через строку действий, и она делает это ходом чата, а не своей
 * ручкой. Для вкладок, под которые в бэкенде пайплайна нет, рисуется честная
 * надпись, а не пустая таблица и не выдуманные строки.
 *
 * `run` — рантайм хода из панели чата (см. DocActionsRun). Пока он не передан,
 * кнопки действий выключены и честно об этом пишут.
 */
export function ExtractedPane({ run }: { run?: DocActionsRun | null }) {
  const { project, docs, activeDocId, setActiveDocId, activeDoc, activeKind, chatRun } =
    useWorkspace();
  // Рантайм хода регистрирует панель чата в контексте — здесь его берём оттуда,
  // если явно не передан пропом (подписка на прогон одна на чат).
  const effRun = run ?? chatRun;

  const tabs = KIND_TABS[activeKind];
  const [tabId, setTabId] = useState(tabs[0].id);
  // Сменился вид исходника — набор вкладок другой, и выбранной в нём может не
  // быть; тогда показываем первую (сбрасывать состоянием не надо).
  const activeTab = tabs.some((t) => t.id === tabId) ? tabId : tabs[0].id;

  const runs = useMemo(
    () => extractionsForDoc(project, activeDocId || null),
    [project, activeDocId],
  );
  // Точечный заказ живьём в область не идёт (решение владельца 31.08.2026):
  // его таблица — в чате; здесь такой прогон появится сохранённым итогом.
  const local = areaExtractionOf(runs);
  const docLabel = activeDoc?.company_name || activeDoc?.filename || activeDocId || "документ";

  // Локальная карточка ГОДНА в двух случаях: прогон в ней завершён, или он идёт
  // ПРЯМО СЕЙЧАС — тогда строки как раз доезжают потоком, и подменять её
  // серверной значило бы заморозить живую таблицу. Карточка, застрявшая в
  // «идёт» без живого хода, — это и есть потерянный исход: ровно так выглядели
  // ДОМ.РФ и ГК «Самолёт» 18.08 (#276).
  const localFresh = Boolean(local) && (local?.status !== "running" || Boolean(project?.activeChatRun));

  // Прогон, ПРОЧИТАННЫЙ С СЕРВЕРА. До этого таблица жила только в localStorage:
  // закрытая раньше времени вкладка стирала час работы движка безвозвратно.
  const [fetched, setFetched] = useState<ExtractionMessage | null>(null);
  useEffect(() => {
    // Выход БЕЗ сброса состояния — по той же причине, что у раскладки роли
    // ниже: синхронный setState в теле эффекта даёт каскадные перерисовки.
    // Годность ответа решает сверка `serverRun` под эффектом.
    if (!activeDocId || localFresh) return;
    let alive = true;
    // Карточка застряла в «идёт» без живого хода — прогон ОТОРВАЛСЯ от экрана
    // (обрыв SSE, рестарт бэкенда), но станция дорабатывает и запишет исход
    // через секунды-минуты. Один запрос здесь оставлял пустую зону до F5:
    // ручка отдаёт только ЗАВЕРШЁННЫЕ прогоны, и в момент вопроса завершённого
    // ещё не было (прогон 0038f0a6, 31.08 — «строки этого вида пока не
    // приходили» при готовом ответе в чате). Переспрашиваем, пока исход не
    // появится; документ без прогонов (`local` пуст) не поллим — там «нет» и
    // есть окончательный ответ.
    const stuck = local?.status === "running";
    let timer: ReturnType<typeof setTimeout> | null = null;
    const pull = () => {
      fetchLastRun(activeDocId)
        .then((stored) => {
          if (!alive) return;
          setFetched(stored ? extractionFromStoredRun(stored, docLabel) : null);
          if (!stored && stuck) timer = setTimeout(pull, 20_000);
        })
        .catch((e: unknown) => {
          // 404 сюда НЕ попадает — его `fetchLastRun` отдаёт как `null` (прогонов
          // нет либо хранилище исходов выключено, и то и другое штатно). Всё
          // остальное — настоящий отказ, и молчать о нём нельзя: пустая зона от
          // неудачи неотличима, на этом уже терялись дни (см. соседний catch).
          if (!alive) return;
          toast.error(
            `Не удалось прочитать прогон с сервера: ${e instanceof Error ? e.message : String(e)}`,
          );
        });
    };
    pull();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [activeDocId, localFresh, docLabel, local?.status]);

  // Прогон от ДРУГОГО документа на экран не пускаем: в шапке был бы документ B,
  // а в таблице числа документа A, ничем не помеченные. Та же сверка, что у
  // раскладки роли ниже.
  const serverRun = fetched && fetched.doc_id === activeDocId && !localFresh ? fetched : null;

  const lastRun = localFresh ? local : (serverRun ?? local);

  // 🔴 СТРОКИ — ОДНОГО ПРОГОНА (правка 18.08). Здесь стоял `collectMetrics` —
  // склейка ВСЕХ прогонов документа. Подписи колонок при этом брались у
  // свежего прогона (`lastRun.meta`), а часть значений приезжала из старого:
  // числа выглядели верными и молча относились не к тому периоду. Раскладку
  // роли это ломало вдвойне — два прогона в разных ролях смешивались в одну
  // таблицу, и состав получался не тот и не другой. Один прогон — одна
  // таблица, как в карточке извлечения на «/».
  const rows = useMemo(() => (lastRun ? rowsFromExtraction(lastRun) : []), [lastRun]);

  // Прогон в чате есть, но по ДРУГОМУ документу (два похожих файла, повторная
  // загрузка того же отчёта). Молчать нельзя: пользователь видит таблицу в
  // чате и пустую зону здесь и решает, что зона сломана. Показываем, где
  // результат, и даём переключиться одним щелчком.
  const elsewhere = useMemo(() => {
    // `serverRun` в условии обязателен: прогон по ЭТОМУ документу нашёлся на
    // сервере, и отправлять пользователя в другой документ было бы прямой
    // дезинформацией — таблица перед ним.
    if (!activeDocId || runs.length || serverRun) return null;
    const other = extractionsForDoc(project, null).find((m) => m.doc_id && m.doc_id !== activeDocId);
    if (!other) return null;
    const doc = docs.find((d) => d.doc_id === other.doc_id);
    return {
      docId: other.doc_id,
      label: doc?.company_name || doc?.filename || other.doc_label || "другой документ",
      known: Boolean(doc),
      count: other.metrics.length,
    };
  }, [project, activeDocId, runs.length, serverRun, docs]);
  const runningAny = findRunningExtraction(project, activeDocId || null);
  // Бейдж «Извлечение идёт» — только про прогоны, которые область рисует:
  // точечный заказ идёт в чате, и обещать здесь строки, которые не приедут
  // live, значит показать вечное «идёт» (жалоба 31.08 — прогон 0038f0a6).
  const running = runningAny && !runningAny.narrow_order ? runningAny : null;

  // Раскладка под роль. Считает БЭКЕНД (POST /metrics/format-role) — тот же
  // источник правды, что у старого экрана: у каждой роли свой набор колонок и
  // порядок строк, и придумывать их на клиенте нельзя. Пересчёт идёт по уже
  // извлечённым строкам, повторный прогон не нужен.
  const [roleView, setRoleView] = useState<RoleView | null>(null);
  // 🔴 РАСКЛАДКУ ЗАДАЁТ САМ ПРОГОН, А НЕ ПОСТОЯННАЯ РОЛЬ ИЗ НАСТРОЕК
  // (решение владельца 23.08.2026, после разбора прогона 49b398dc).
  //
  // Здесь стояло `useTableRole()` — чтение `prefs.role`, — и таблица ЛЮБОГО
  // прогона по общей роли молча перестраивалась в раскладку постоянной роли
  // пользователя. У владельца в настройках стоит CIB; он спросил пять
  // коэффициентов, станция честно прочитала 22 показателя и отметила к показу
  // 19, а в рабочей области открылось 65 СТРОК: раскладка CIB длинная — строка
  // на показатель на период. Ни одного признака того, что это чужая раскладка,
  // на экране не было.
  //
  // ГРАНИЦА, КОТОРУЮ ВЛАДЕЛЕЦ ПРОВЁЛ СЛОВАМИ: «роль влияет только на запуск по
  // детерминированному сценарию — если пользователь нажал шорткат и выбрал
  // роль. Если поступает вопрос, агент не должен ограничиваться ролевой
  // принадлежностью». Ровно так и сделано, и вторая половина этого правила уже
  // работала: шорткат «Извлечь метрики» называет роль во фразе
  // (`doc-actions.tsx`), модель запускает извлечение с ней, и раскладка
  // приезжает СВОЯ — `extraction_role` у такого прогона и есть выбранная роль.
  // Свободный же вопрос идёт с ролью `other`, и его таблица остаётся широкой.
  //
  // `prefs.role` от этого не умирает: он по-прежнему решает, что отправит
  // шорткат. Он перестал решать, как выглядит таблица чужого прогона.
  const appliedRole: ExtractionRole = tableRoleOf(
    lastRun?.extraction_role || lastRun?.meta?.role || "other",
  );

  // Тело запроса берём из РЕФА, а не из зависимостей эффекта. `lastRun` меняет
  // identity на каждое событие стрима (объект проекта пересобирается целиком),
  // и эффект с ним в зависимостях слал POST /metrics/format-role по два-три
  // раза в секунду всё извлечение — а побеждал последний ПРИШЕДШИЙ ответ, а не
  // последний отправленный. Реф обновляется отдельным эффектом, объявленным
  // ВЫШЕ запрашивающего: эффекты компонента исполняются в порядке объявления.
  const lastRunRef = useRef<ExtractionMessage | null>(lastRun);
  useEffect(() => {
    lastRunRef.current = lastRun;
  });

  const runId = lastRun?.id ?? "";
  const runStatus = lastRun?.status ?? "";
  const runRowCount = lastRun?.metrics.length ?? 0;

  useEffect(() => {
    // Выходы БЕЗ сброса состояния: пустоту решает `activeRoleView` ниже — он
    // сверяет документ, прогон и роль. Синхронный setState в теле эффекта
    // запрещён (cascading renders), и обходить это флагом не нужно: раскладка,
    // не прошедшая сверку, на экран всё равно не попадёт.
    if (!activeDocId || !runId) return;
    // «Общая» и «Все показатели» — это и есть таблица по умолчанию. FinEngine
    // на них к серверу не ходит вовсе, и правильно: раскладки для них нет,
    // ответ всегда `formatted: null`, а лишний вызов — лишняя точка отказа
    // (на 5xx роль «залипала» тостом об ошибке на ровном месте).
    if (isGenericTableRole(appliedRole)) return;
    // Идёт прогон — строки ещё приезжают. Раскладка по половине набора показала
    // бы пропуски там, где значения просто не дошли.
    if (runStatus === "running") return;

    const runNow = lastRunRef.current;
    if (!runNow || !runRowCount) return;

    let alive = true;
    void formatMetricsForRole({
      company_name: runNow.meta?.company_name,
      company_type: runNow.meta?.company_type,
      role: appliedRole,
      period_current: runNow.meta?.period_current,
      period_prev: runNow.meta?.period_prev,
      unit: runNow.meta?.unit,
      rows: runNow.metrics.map((r) => ({
        enum: r.enum,
        name_ru: r.name_ru,
        segment: r.segment,
        dimension: r.dimension,
        value_current: r.value_current,
        value_prev: r.value_prev,
        period_values: r.period_values,
        unit: r.unit,
        page: r.page,
        // Без формы источника колонка «Источник» у ДИЭ беднее, чем на «/»:
        // страницу она показывает, а из какого отчёта строка — нет.
        source_form: r.source_form,
        is_calculated: r.is_calculated,
        formula: r.formula,
        // Без входов книга роли теряет происхождение расчёта: формулу она
        // покажет, а числа, по которым та посчитана, — нет (Б5).
        inputs: r.inputs,
      })),
    })
      .then((res) => {
        // Ответ опоздавшего запроса относится к другой роли или другому
        // документу — состояние им не трогаем.
        if (!alive) return;
        const table = res.formatted ?? null;
        const hasRows = table !== null && formattedDataRowCount(table) > 0;
        setRoleView({
          docId: activeDocId,
          runId,
          role: appliedRole,
          table: hasRows ? table : null,
          outputEnums: res.output_enums ?? [],
          // 🔴 ПУСТОЙ ОТВЕТ — НЕ ОШИБКА СЕТИ И НЕ ПУСТОЙ ОТЧЁТ. Для трёх пар
          // роль×отчётность (Риски×банк, БАИФП×нефин., ДГР×нефин.) раскладки в
          // бэкенде нет вовсе: он отдаёт 200 и `formatted: null`. Без этой
          // надписи пользователь видел общую таблицу под подписью «ДГР» и
          // справедливо решал, что роль не работает.
          note: hasRows
            ? null
            : roleLayoutMissingNote(roleLabel(appliedRole), res.role_format, Boolean(table)),
        });
      })
      .catch((e: unknown) => {
        if (!alive) return;
        // 🔴 ОТКАЗ ГОВОРИТ ВСЛУХ (правка 17.08). Здесь стояло молчаливое
        // `setRoleTable(null)`, и оно стоило дней: схема ручки не принимала
        // строковые значения, каждый выбор роли отбивался 422-й, а на экране
        // просто оставалась общая таблица. Владелец спрашивал «почему роль не
        // работает» неделю; ответ всё это время лежал в access-логе бэкенда.
        //
        // Пустая таблица от неудачи неотличима — значит неудачу надо назвать.
        setRoleView(null);
        toast.error(
          `Не удалось перестроить таблицу под роль: ${
            e instanceof Error ? e.message : String(e)
          }`,
        );
      });

    return () => {
      alive = false;
    };
  }, [activeDocId, runId, runStatus, runRowCount, appliedRole]);

  // Раскладка от ДРУГОГО документа или другого прогона на экране — ложь:
  // в шапке документ B, а в таблице числа документа A, ничем не помеченные.
  // Пока новый ответ не пришёл, показываем обычную таблицу, а не чужую.
  const activeRoleView =
    roleView && roleView.docId === activeDocId && roleView.runId === runId
      && roleView.role === appliedRole
      ? roleView
      : null;

  // Роль сузила набор показателей — счётчик и вкладки обязаны считать по нему,
  // иначе цифры не бьются с тем, что на экране.
  // 🔴 РОЛЬ СУЖАЕТ ПОКАЗАТЕЛИ, И ТОЛЬКО ИХ (правка 18.08, поймано состязательной
  // проверкой). Фильтр по `outputEnums` применялся ко ВСЕМ строкам, включая
  // сегментные и строки состава холдинга. Их enum'ов в перечне роли нет и быть
  // не может — перечень про показатели, — поэтому обе вкладки под узкой ролью
  // пустели, а панель писала «в этом прогоне их не было». Это прямая ложь:
  // строки лежали в прогоне рядом. Поведение успели закрепить сторожем, то есть
  // ложь ещё и охранялась; сторож переписан вместе с правкой.
  //
  // 🔴 И ОТДЕЛЬНО — СРЕЗ САМОГО ПРОГОНА (24.08.2026, найдено владельцем).
  //
  // Фильтр выше — про РОЛЬ: `filterRowsByOutputEnums` для общей роли отдаёт
  // строки как есть (`isGenericTableRole` → return rows), и это верно: у
  // «Общей» своего перечня нет. Но у ПРОГОНА перечень есть — `table_output_enums`
  // из `extract_done`, — и его здесь не спрашивал никто.
  //
  // Цена: человек попросил пять коэффициентов, станция сузила состав до
  // двадцати пяти имён и честно их прислала, а в рабочей области открылось
  // СЕМЬДЕСЯТ СЕМЬ — весь реестр ячейки. «Почему агент снова вывел 77
  // показателей? Мы же уже делали механику» — и он прав: механику сделали, но
  // ДЛЯ ДРУГОГО ЭКРАНА. 22.08 это закрыли в карточке старого экрана «/»
  // (metrics-panel, снесена в Р20), а рабочая область фильтрует своим путём.
  // Правило в одном месте из двух, наш постоянный класс.
  //
  // Функция одна на оба экрана (`scopeRowsToSlice`), и порядок важен: сперва
  // срез прогона (что человек просил), потом раскладка роли (как показать).
  const buckets = useMemo(() => {
    const all = splitRows(rows);
    const свои = scopeRowsToSlice(all.metrics, lastRun?.table_output_enums);
    return {
      ...all,
      metrics: filterRowsByOutputEnums(свои, appliedRole, activeRoleView?.outputEnums),
    };
  }, [rows, appliedRole, activeRoleView, lastRun?.table_output_enums]);
  // «Извлечено N» — про показатели: разрезы и состав считаются на своих вкладках.
  const scopedRows = buckets.metrics;

  const variant = VARIANT_OF[activeTab] ?? null;
  // Поиск живёт здесь, а таблице приходит пропом: одно поле на зону.
  const [query, setQuery] = useState("");

  // ДИЭ приезжает одной таблицей: сверху значения, ниже разбивка по сегментам.
  // Вкладка обязана показывать свою половину — иначе на «Сегментах» стоит то
  // же полотно, что на «Показателях», а поиск в роль-таблице не работал вовсе.
  const roleTableForTab = useMemo(() => {
    const table = activeRoleView?.table;
    if (!table) return null;
    if (variant === "metrics") return filterFormattedTable(dieValuesTable(table), query);
    if (variant === "segments") {
      const segments = dieSegmentsTable(table);
      return segments ? filterFormattedTable(segments, query) : null;
    }
    return null;
  }, [activeRoleView, variant, query]);

  const roleNote = activeRoleView?.note ?? null;

  return (
    <div className="flex h-full flex-col">
      <div className="flex shrink-0 items-end gap-3 border-b border-[var(--color-divider)] px-4 py-2">
        {/* ПОИСК НА МЕСТЕ ВЫПАДАШКИ (правка Nate 17.08). Выбор документа уехал
            в кнопку «Документ» — там ему и место, зона и так называется по
            документу. Освободившаяся строка отдана поиску по таблице: раньше
            он жил ниже, отдельной полосой, и на узкой панели две почти
            одинаковые строки шли подряд. */}
        <div className="flex min-w-0 flex-1 items-center gap-2 border-b border-[var(--color-divider)] py-1 focus-within:border-[var(--color-accent)]">
          <Search className="size-3.5 shrink-0 text-[var(--color-neutral-500)]" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Поиск"
            className="min-w-0 flex-1 bg-transparent text-[12.5px] outline-none placeholder:text-[var(--color-neutral-500)]"
          />
        </div>
      </div>

      <DocActions doc={activeDoc} run={effRun} />

      <div
        role="radiogroup"
        aria-label="Вид извлечённого"
        className="flex shrink-0 flex-wrap items-baseline gap-4 border-b border-[var(--color-divider)] px-4 py-2"
      >
        {tabs.map((t) => {
          const v = VARIANT_OF[t.id];
          const count = v ? buckets[v].length : null;
          const on = t.id === activeTab;
          return (
            <button
              key={t.id}
              type="button"
              role="radio"
              aria-checked={on}
              onClick={() => setTabId(t.id)}
              className={cn(
                "flex items-baseline gap-1.5 border-b pb-0.5 text-[12.5px] transition-colors",
                on
                  ? "border-[var(--color-accent)] text-[var(--color-text)]"
                  : "border-transparent text-[var(--color-neutral-600)] hover:text-[var(--color-text)]",
              )}
            >
              {t.label}
              {count !== null && count > 0 && (
                <span className="text-[10px] text-[var(--color-neutral-500)] [font-variant-numeric:tabular-nums]">
                  {count}
                </span>
              )}
            </button>
          );
        })}
        {/* СЧЁТЧИК ПЕРЕЕХАЛ СЮДА (правка Nate 18.08): его место — рядом с
            разбивкой по видам строк, а не в строке поиска, где он спорил за
            внимание с полем ввода. БЕЗ «N из N»: знаменатель реестра сбивал с
            толку — показателя может в отчёте просто не быть, а «35 из 35»
            читалось как «что-то не нашли». */}
        {scopedRows.length > 0 && (
          <span className="ml-auto shrink-0 text-[11px] text-[var(--color-neutral-600)] [font-variant-numeric:tabular-nums]">
            извлечено {scopedRows.length}
          </span>
        )}
      </div>

      {!activeDocId ? (
        <Notice
          title={docs.length ? "Документ не выбран" : "В проекте нет документов"}
          body={
            docs.length
              ? "Выберите документ в списке выше — извлечённое показывается по одному документу."
              : "Загрузите отчётность, пресс-релиз или транскрипт — тогда здесь появится извлечённое."
          }
        />
      ) : !variant ? (
        <Notice title="Извлечение этого типа готовится" body={NOT_READY[activeTab] ?? NOT_READY_FALLBACK} />
      ) : runs.length === 0 && elsewhere ? (
        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
          <p className="text-[12.5px] text-[var(--color-text)]">
            Извлечение в этом чате есть — но по другому документу
          </p>
          <p className="mt-1 max-w-[46ch] text-[12.5px] leading-relaxed text-[var(--color-neutral-500)]">
            Прогон прошёл по «{elsewhere.label}» ({elsewhere.count} показателей). Сейчас
            выбран другой документ — вероятно, тот же отчёт загружен дважды.
          </p>
          {elsewhere.known && (
            <button
              type="button"
              onClick={() => setActiveDocId(elsewhere.docId)}
              className="mt-3 rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] px-2.5 py-1 text-[10px] uppercase tracking-[0.12em] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-accent-100)]"
            >
              Показать результат
            </button>
          )}
        </div>
      ) : runs.length === 0 ? (
        <Notice
          title="Извлечение по этому документу ещё не запускалось"
          body="Таблица наполняется из прогона извлечения — попросите агента в чате извлечь показатели по этому документу."
        />
      ) : (
        <>
          {activeKind === "press_release" && (
            <p className="shrink-0 border-b border-[var(--color-divider)] px-4 py-1.5 text-[11px] leading-snug text-[var(--color-neutral-600)]">
              Реестр показателей рассчитан на отчётность: по пресс-релизу извлечение
              формально отрабатывает, но качество на нём не измерялось.
            </p>
          )}
          {roleNote && (
            <p className="shrink-0 border-b border-[var(--color-divider)] px-4 py-1.5 text-[11px] leading-snug text-[var(--color-neutral-600)]">
              {roleNote}
            </p>
          )}
          {roleTableForTab ? (
            formattedDataRowCount(roleTableForTab) > 0 ? (
              <RoleTable table={roleTableForTab} />
            ) : (
              <Notice
                title="По запросу ничего не найдено"
                body="В раскладке роли таких строк нет — очистите поиск, чтобы увидеть таблицу целиком."
              />
            )
          ) : buckets[variant].length === 0 ? (
            <Notice
              title={running ? "Извлечение идёт" : "В прогоне таких строк нет"}
              body={running ? RUNNING_NOTE : EMPTY_NOTE[variant]}
            />
          ) : (
            <ExtractedTable
              query={query}
              rows={buckets[variant]}
              variant={variant}
              company={lastRun?.meta?.company_name}
              unit={lastRun?.meta?.unit}
              periodCurrent={lastRun?.meta?.period_current}
              periodPrev={lastRun?.meta?.period_prev}
            />
          )}
        </>
      )}
    </div>
  );
}

/** Раскладка роли вместе с адресом, к которому она относится. Без адреса
 *  таблица прошлого документа держалась на экране до ответа ручки. */
type RoleView = {
  docId: string;
  runId: string;
  role: ExtractionRole;
  table: FormattedTable | null;
  /** Пустой список = «фильтра нет», а не «показывать нечего». */
  outputEnums: string[];
  /** Почему таблицы нет; null — таблица есть. */
  note: string | null;
};

function Notice({ title, body }: { title: string; body: string }) {
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6">
      <p className="text-[12.5px] text-[var(--color-text)]">{title}</p>
      <p className="mt-1 max-w-[46ch] text-[12.5px] leading-relaxed text-[var(--color-neutral-500)]">
        {body}
      </p>
    </div>
  );
}

/** Вкладки, под которыми есть реальный источник данных. Остальные — честная надпись. */
const VARIANT_OF: Record<string, ExtractedVariant | undefined> = {
  metrics: "metrics",
  segments: "segments",
  group: "group",
};

/**
 * Разложить строки прогона по вкладкам. Признаки те же, что у рабочей таблицы
 * на «/»: состав холдинга помечен осью dimension, сегментные строки — именем
 * сегмента (итог «TOTAL» относится к показателям, а не к сегментам).
 */
function splitRows(rows: MetricRow[]): Record<ExtractedVariant, MetricRow[]> {
  const out: Record<ExtractedVariant, MetricRow[]> = { metrics: [], segments: [], group: [] };
  for (const r of rows) {
    if (isGroupRow(r)) out.group.push(r);
    else if (isSegmentRow(r)) out.segments.push(r);
    else out.metrics.push(r);
  }
  return out;
}

function isGroupRow(r: MetricRow): boolean {
  const d = (r.dimension || "").trim().toLowerCase();
  return ["consolidated_group", "consolidation_group", "group_structure"].includes(d);
}

function isSegmentRow(r: MetricRow): boolean {
  const seg = (r.segment || "").trim();
  if (!seg || isGroupRow(r)) return false;
  if (seg.toUpperCase() === "TOTAL") return false;
  if (r.segment_fallback === "main_metric_total") return false;
  return true;
}

const RUNNING_NOTE =
  "Прогон ещё идёт — строки этого вида пока не приходили. Таблица дорисуется по ходу извлечения.";

const EMPTY_NOTE: Record<ExtractedVariant, string> = {
  metrics: "Прогон завершился, но показателей в нём нет. Проверьте, распознан ли текст документа.",
  segments:
    "Разбивку по сегментам даёт отдельная стадия извлечения: она включается только на агентском пути и не работает для банков. В этом прогоне её не было.",
  group:
    "Состав холдинга извлекает отдельная стадия, доступная только на агентском пути извлечения. В этом прогоне её не было.",
};

const NOT_READY_FALLBACK =
  "Пайплайна для этого вида извлечения в системе пока нет — строк не будет, пока он не появится в бэкенде.";

/** Честно, по каждой вкладке: чего именно нет и что есть вместо этого. */
const NOT_READY: Record<string, string> = {
  events:
    "События из текста документа не извлекаются. Ближайшее, что есть в системе, — календарь корпоративных событий, но он собирает их из сети, а не из этого файла.",
  insights:
    "Инсайты из документа не извлекаются. Классификатор, который есть в системе, работает по новостной ленте из сети, а не по загруженному файлу.",
  guidance:
    "Guidance из транскриптов не извлекается. Движок извлечения устроен наоборот — он намеренно отбрасывает прогнозные значения и берёт только фактические.",
  drivers:
    "Драйверы отдельными строками не извлекаются: сейчас они остаются прозой внутри разделов финанализа.",
  facts:
    "Факты из транскриптов не извлекаются. Приём транскриптов в бэкенде есть только каркасом: на обычной загрузке тип документа и спикеры не проставляются.",
};

/**
 * Таблица в раскладке РОЛИ. Колонки, порядок строк, отступы и выделения
 * приходят с бэкенда (POST /metrics/format-role) — фронт их только рисует.
 * Своей логики раскладки здесь нет намеренно: у каждой роли она своя, и
 * второй источник правды разошёлся бы с Excel и со старым экраном.
 *
 * Разрешение ячейки и формат числа — из общего модуля `lib/formatted-table`:
 * своя реализация («первая колонка — label, остальные — values[key]») оставляла
 * у CIB и БАИФП все колонки кроме первой пустыми, а у «Рисков» — и первую.
 */
function RoleTable({ table }: { table: FormattedTable }) {
  const cols = visibleFormattedColumns(table);
  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <table className="w-max min-w-full border-collapse text-[12px]">
        <thead className="sticky top-0 bg-[var(--color-bg)]">
          <tr>
            {cols.map((c) => (
              <th
                key={c.key}
                className={cn(
                  "border-b border-[var(--color-divider)] px-3 py-1.5 text-left text-[10px] font-normal uppercase tracking-[0.11em] text-[var(--color-neutral-600)]",
                  isFormattedNumeric(c) && "text-right",
                )}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {table.rows.map((r, i) => (
            <tr
              key={i}
              className={cn(
                "border-b border-[var(--color-divider)]",
                r.highlight && "bg-[var(--color-accent-100)]",
              )}
            >
              {cols.map((c, ci) => {
                const numeric = isFormattedNumeric(c);
                return (
                  <td
                    key={c.key}
                    style={ci === 0 && r.indent ? { paddingLeft: 12 + r.indent * 14 } : undefined}
                    className={cn(
                      "px-3 py-1 align-top text-[var(--color-text)]",
                      numeric && "whitespace-nowrap text-right tabular-nums",
                      r.bold && "font-medium",
                    )}
                  >
                    {fmtFormattedCell(formattedCellValue(r, c), c, r)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
