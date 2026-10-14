"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ChevronDown } from "lucide-react";
import { useAuth } from "@/components/auth-context";
import { WorkspaceHeader } from "@/components/workspace/workspace-header";
import { StaleBuildBanner } from "@/components/workspace/stale-build-banner";
import { WorkspaceRail, type RailGroup } from "@/components/workspace/workspace-rail";
import { WorkspaceFrame } from "@/components/workspace/workspace-frame";
import { WorkspaceProvider, useWorkspace, docTitle, docTypeRu } from "@/components/workspace/workspace-context";
import {
  applyServerDeletions,
  createProject,
  lastStorageFailure,
  loadFolders,
  loadProjects,
  mergeServerProjects,
  reportStorage,
  saveProjects,
  sweepOrphanRunKeys,
  turnProgress,
  type Folder,
  type Project,
} from "@/lib/projects";
import { notify } from "@/lib/notices";
import { listScenarios, type Scenario } from "@/lib/scenarios";
import { toolLabel } from "@/lib/tool-labels";
import { fetchDialogs } from "@/lib/dialog-sync";
import { ChatPane } from "@/components/workspace/panes/chat-pane";
import { DocumentPane } from "@/components/workspace/panes/document-pane";
import { ExtractedPane } from "@/components/workspace/panes/extracted-pane";
import { ArtifactsPane } from "@/components/workspace/panes/artifacts-pane";
import { DocPicker } from "@/components/workspace/panes/doc-picker";
import { PlanIsland } from "@/components/workspace/panes/plan-island";
import { DynamicRunTraceView } from "@/components/workspace/panes/dynamic-run-trace-view";
import { PlanGraphWorkspace } from "@/components/workspace/panes/plan-graph-workspace";
import {
  executionGraphsOf,
  graphMessageForWorkspace,
  graphOwnerMessage,
  resolveGraphView,
  type GraphSelection,
} from "@/lib/execution-graphs";
import { ScenarioView } from "@/components/workspace/panes/scenario-view";
import { cn } from "@/lib/utils";

/**
 * Новый рабочий экран (дизайн-хендофф). Пока живёт отдельным адресом /v2 —
 * старый экран на «/» не трогаем, пока новый не наберёт паритет.
 *
 * Живое состояние хода держит ChatPane (useChatRun): он единственный владелец
 * подписки на прогон. Страница читает проекты из хранилища, а обновления по
 * ходу приходят событием «finengine:projects» — иначе рельс и шапка застывали
 * бы на снимке (имя чата, число документов после автопереименования).
 */
export default function WorkspaceV2Page() {
  const { user, loading } = useAuth();
  const router = useRouter();

  const [projects, setProjects] = useState<Project[]>([]);
  const [folders, setFolders] = useState<Folder[]>([]);
  const [scenarios, setScenarios] = useState<Scenario[]>([]);
  const [activeId, setActiveId] = useState<string>("");
  /** Открытая страница сценария накрывает рабочую строку; рельс остаётся. */
  const [scenarioId, setScenarioId] = useState<string>("");
  /** Просьбу к раскладке нельзя послать отсюда — контекст ниже по дереву;
   *  поднимаем флаг, а Shell (уже внутри провайдера) её отправит. */
  const newChatLayout = useRef(false);

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  const reload = useCallback(() => {
    if (!user) return;
    setProjects(loadProjects(user.username));
    setFolders(loadFolders(user.username));
  }, [user]);

  const reloadScenarios = useCallback(() => {
    listScenarios()
      .then(setScenarios)
      .catch(() => setScenarios([]));
  }, []);

  useEffect(() => {
    reload();
    reloadScenarios();
  }, [reload, reloadScenarios]);

  // Сервер — источник истины списка чатов. Подтягиваем серверную копию и
  // сливаем с локальной: чат, стёртый локально (старой вкладкой, чисткой
  // браузера), возвращается. Запись идёт через merge-safe saveProjects, так
  // что слияние не может ничего ужать. Подтяжка — не только при старте, но и
  // при КАЖДОМ возврате на вкладку: пропажа, случившаяся пока вкладка спала,
  // самовосстанавливается в момент, когда пользователь на неё смотрит.
  const pullingRef = useRef(false);
  /** След о подтяжке шлём раз за сессию — подтяжка бьёт на каждый возврат на
   *  вкладку, а журнал не помойка. Сокращение списка отчитывается всегда. */
  const pullReportedRef = useRef(false);
  const pullFromServer = useCallback(async () => {
    if (!user || pullingRef.current) return;
    pullingRef.current = true;
    try {
      const server = await fetchDialogs().catch(() => null);
      if (!server) return;
      // Удаления применяются ПЕРВЫМИ, ДО проверки на непустой список, и в
      // строгом порядке (сокращение списка → надгробия): при переполненном
      // хранилище это единственный способ освободить место (applyServerDeletions).
      //
      // ⚠️ Именно ДО проверки. Пока применение стояло после `if
      // (!server.projects.length) return`, удаление ВСЕХ чатов не доезжало до
      // браузера никогда: сервер отдаёт пустой projects при полном списке
      // надгробий, и выход срабатывал раньше.
      const before = loadProjects(user.username).length;
      const applied = applyServerDeletions(user.username, server.deleted);
      // ⚠️ БЕЗУСЛОВНЫЙ след, по одному на сессию. Разбор 11.08 встал ровно
      // здесь: по журналу нельзя было отличить «в браузере старый бандл» от
      // «новый бандл, но метки не применились» — а applyServerDeletions молчит
      // трояко (нет меток / removed=0 / метки уже усыновлены), и отсутствие
      // строки не доказывает ничего. Строка есть ВСЕГДА, и в ней отпечаток
      // сборки: старый бандл её напечатать физически не может.
      if (!pullReportedRef.current || applied.removed) {
        pullReportedRef.current = true;
        reportStorage(
          "dialogs_pull",
          `build=${process.env.NEXT_PUBLIC_BUILD_ID ?? "dev"}; on_disk=${before}; ` +
            `server=${server.projects?.length ?? 0}; tombs=${Object.keys(server.deleted ?? {}).length}; ` +
            `removed=${applied.removed}; adopted=${applied.adopted}`,
        );
      }
      if (!server.projects?.length) {
        reload();
        return;
      }
      // Само слияние — чистой функцией в projects.ts: там же живёт правило
      // «сервер добавляет, но не обедняет», и оно накрыто тестом.
      const next = mergeServerProjects(
        loadProjects(user.username),
        server.projects,
        server.deleted,
      );
      if (!next) return;
      saveProjects(user.username, next);
      reload();
    } finally {
      pullingRef.current = false;
    }
  }, [user, reload]);

  useEffect(() => {
    void pullFromServer();
    const onVisible = () => {
      if (document.visibilityState === "visible") void pullFromServer();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [pullFromServer]);

  // Ключи прогонов, чьи чаты давно удалены, снимаем ОДИН РАЗ за загрузку.
  //
  // Уборка в deleteProject лечит будущее, но не прошлое: осиротевшие раньше
  // ключи снять некому — их владельца нет. Место они занимают в тех же 5 МиБ,
  // из-за которых зеркало выбрасывает живые диалоги, поэтому метём.
  //
  // ⚠️ ПОСЛЕ подтяжки с сервера, а не до: до неё локальный список может быть
  // ещё не пополнен серверными чатами, и метла сочла бы их ключи ничейными.
  const подметеноRef = useRef(false);
  useEffect(() => {
    if (!user || подметеноRef.current || pullingRef.current) return;
    подметеноRef.current = true;
    const снято = sweepOrphanRunKeys(user.username);
    if (снято) reportStorage("orphan_run_keys_swept", `снято=${снято}`);
  }, [user, projects]);

  // Открытый чат переживает перезагрузку: без этого страница поднималась с
  // пустым выбором, шапка писала «выберите чат слева», а композер был заперт.
  useEffect(() => {
    if (!user) return;
    const saved = localStorage.getItem(`aifa:v2:active:${user.username}`);
    if (saved) setActiveId(saved);
  }, [user]);

  useEffect(() => {
    if (!user || !activeId) return;
    try {
      localStorage.setItem(`aifa:v2:active:${user.username}`, activeId);
    } catch {
      // Переполненная квота роняла эффект исключением. Запомнить открытый чат
      // — удобство; ронять из-за него страницу нельзя (разбор 11.08).
    }
  }, [user, activeId]);

  // Ничего не выбрано, но чаты есть — открываем самый свежий. Пустой экран с
  // запертым вводом пользователю не нужен.
  useEffect(() => {
    if (activeId || scenarioId || !projects.length) return;
    const chats = projects
      .filter((p) => !p.folder_id)
      .sort((a, b) => b.updated_at - a.updated_at);
    if (chats.length) setActiveId(chats[0].id);
  }, [activeId, scenarioId, projects]);

  // Ход правит проект в хранилище — подхватываем, чтобы рельс и шапка не врали.
  useEffect(() => {
    const onChange = () => reload();
    window.addEventListener("finengine:projects", onChange);
    window.addEventListener("storage", onChange);
    return () => {
      window.removeEventListener("finengine:projects", onChange);
      window.removeEventListener("storage", onChange);
    };
  }, [reload]);

  const groups: RailGroup[] = useMemo(() => {
    const chats = projects.filter((p) => !p.folder_id);
    return [
      {
        id: "automations",
        title: "Автоматизации",
        items: scenarios.map((s) => ({
          id: s.id,
          label: s.name,
          dot: s.paused ? "var(--color-neutral-400)" : "var(--color-accent)",
        })),
      },
      {
        id: "projects",
        title: "Проекты",
        items: folders.map((f) => {
          const inside = projects.filter((p) => p.folder_id === f.id);
          return {
            id: f.id,
            label: f.name,
            badge: inside.length || undefined,
            disabled: inside.length === 0,
            hint: inside.length ? undefined : "в проекте пока нет чатов",
          };
        }),
      },
      {
        id: "chats",
        title: "Чаты",
        hint: "по дате",
        items: chats
          .slice()
          .sort((a, b) => b.updated_at - a.updated_at)
          .map((p) => ({ id: p.id, label: p.name })),
      },
    ];
  }, [folders, projects, scenarios]);

  // Открытый чат держим ЗАЛИПШИМ: список перечитывается при возврате на
  // вкладку и на каждое событие хранилища, и в момент подмены массива чат
  // может на кадр «не найтись». Раньше экран на этот кадр гас — отсюда моргание
  // и ощущение переподключения. Пока id тот же, показываем прошлую копию.
  const found = projects.find((p) => p.id === activeId) ?? null;
  const lastActiveRef = useRef<Project | null>(null);
  if (found) lastActiveRef.current = found;
  const active =
    found ?? (lastActiveRef.current?.id === activeId ? lastActiveRef.current : null);

  // Настоящая пропажа (чат не вернулся за пару секунд) — редкость и повод для
  // следа в журнале. Пользователя НЕ дёргаем: экран не мигает, восстановление
  // идёт молча, а разбираться по журналу — наша работа, не его.
  const lostRef = useRef("");
  useEffect(() => {
    if (!user || !activeId || scenarioId || found) {
      lostRef.current = "";
      return;
    }
    if (lostRef.current === activeId) return;
    const timer = setTimeout(() => {
      if (loadProjects(user.username).some((p) => p.id === activeId)) return;
      lostRef.current = activeId;
      reportStorage(
        "active_chat_lost",
        `projects_in_state=${projects.length}; on_disk=${loadProjects(user.username).length}`,
        activeId,
      );
      void pullFromServer();
    }, 2000);
    return () => clearTimeout(timer);
  }, [user, activeId, scenarioId, found, projects.length, pullFromServer]);

  const pick = useCallback(
    (groupId: string, id: string) => {
      if (groupId === "automations") {
        setScenarioId(id);
        return;
      }
      // Любой другой выбор возвращает рабочую строку — страница сценария её
      // накрывает целиком, и оставлять её поверх выбранного чата нельзя.
      setScenarioId("");
      if (groupId === "projects") {
        // У папки своего треда нет — открываем её самый свежий чат.
        const inside = projects
          .filter((p) => p.folder_id === id)
          .sort((a, b) => b.updated_at - a.updated_at);
        if (inside.length) setActiveId(inside[0].id);
        return;
      }
      if (groupId === "chats") setActiveId(id);
    },
    [projects],
  );

  const newChat = useCallback(() => {
    if (!user) return;
    const p = createProject();
    const next = [p, ...loadProjects(user.username)];
    if (!saveProjects(user.username, next)) {
      // Чат не лёг на диск. Раньше это было НЕМЫМ: экран показывал чат,
      // которого в хранилище нет, а расплата приходила позже и не по адресу —
      // «документ не прикреплён к чату». Отказываем сразу и называем причину.
      const fail = lastStorageFailure();
      notify(
        "error",
        fail?.quota
          ? `Чат не создан: хранилище браузера переполнено (${fail.projects} чатов). ` +
            `Удалите старые чаты через меню «…» над первым сообщением.`
          : "Чат не создан: браузер не дал записать его в хранилище.",
      );
      return;
    }
    setProjects(next);
    setActiveId(p.id);
    setScenarioId("");
    // Новый чат начинается с чистого листа: только переписка. Документ и
    // рабочая область раскроются сами, когда появится с чем работать.
    newChatLayout.current = true;
  }, [user]);

  // Чат удалили из меню «…». Снимаем выбор сразу: иначе экран остался бы на
  // несуществующем id, а сторож «пропажа активного чата» написал бы в журнал
  // ложную тревогу — удаление было намеренным.
  const onChatDeleted = useCallback(
    (id: string) => {
      if (!user) return;
      const rest = loadProjects(user.username);
      setProjects(rest);
      if (activeId !== id) return;
      setActiveId("");
      lastActiveRef.current = null;
    },
    [user, activeId],
  );

  if (loading || !user) return null;

  return (
    <WorkspaceProvider project={active}>
      <Shell
        project={active}
        groups={groups}
        activeId={scenarioId || activeId}
        scenarioId={scenarioId}
        user={{
          initials: user.username.slice(0, 2).toUpperCase(),
          name: user.username,
          role: user.is_admin ? "Админ" : "Аналитик",
        }}
        onNewChat={newChat}
        onChatDeleted={onChatDeleted}
        newChatLayout={newChatLayout}
        onPick={pick}
        onCloseScenario={() => setScenarioId("")}
        onScenarioChanged={reloadScenarios}
      />
    </WorkspaceProvider>
  );
}

/**
 * Внутренний каркас — уже внутри провайдера, поэтому шапка может показывать
 * АКТИВНЫЙ ДОКУМЕНТ (мультидокументная работа: переключил документ в ленте —
 * сменились компания и мета в шапке), а не имя чата.
 */
function Shell({
  project,
  groups,
  activeId,
  scenarioId,
  user,
  onNewChat,
  onChatDeleted,
  newChatLayout,
  onPick,
  onCloseScenario,
  onScenarioChanged,
}: {
  project: Project | null;
  groups: RailGroup[];
  activeId: string;
  /** Непустой — вместо трёх панелей показываем страницу сценария. */
  scenarioId: string;
  user: { initials: string; name: string; role: string };
  onNewChat: () => void;
  /** Чат удалён из меню «…» — снять выбор на странице. */
  onChatDeleted: (id: string) => void;
  newChatLayout: React.MutableRefObject<boolean>;
  onPick: (groupId: string, id: string) => void;
  onCloseScenario: () => void;
  onScenarioChanged: () => void;
}) {
  const ws = useWorkspace();
  const doc = ws.activeDoc;

  // Новый чат: показываем только переписку — документ и рабочая область в
  // корешках, раскроются, когда появится документ или результат прогона.
  useEffect(() => {
    if (!newChatLayout.current) return;
    newChatLayout.current = false;
    ws.requestLayout({ chat: false, doc: true, ws: true });
  }, [project?.id, newChatLayout, ws]);

  const company = doc?.company_name?.trim() || doc?.filename || project?.name || "Рабочее место";
  // Вид ПО-РУССКИ (правка 18.08). Здесь печатался сырой `doc_type`, и в шапке
  // над всей страницей висело «OTHER · 36 СТР.» — внутренний код перечисления
  // на самом видном месте. Тот же вывод, что и в имени документа, чтобы шапка
  // и вкладка не расходились в словах об одном и том же файле.
  const meta = doc
    ? [docTypeRu(doc), doc.period, doc.pages ? `${doc.pages} стр.` : ""]
        .filter(Boolean)
        .join(" · ") || doc.filename
    : project
      ? "документ не выбран"
      : "выберите чат или проект слева";

  return (
    <div className="flex h-full flex-col bg-[var(--color-bg)] text-[var(--color-text)]">
      <StaleBuildBanner />
      <WorkspaceHeader company={company} meta={meta} />
      <div className="flex min-h-0 flex-1">
        <WorkspaceRail
          groups={groups}
          activeId={activeId}
          user={user}
          onNewChat={onNewChat}
          onPick={onPick}
        />
        {scenarioId ? (
          <div className="min-w-0 flex-1">
            <ScenarioView
              key={scenarioId}
              scenarioId={scenarioId}
              onBack={onCloseScenario}
              onChanged={onScenarioChanged}
            />
          </div>
        ) : (
          <WorkspaceFrame
            panes={{
              chat: (
                <ChatPane
                  project={project}
                  onNeedProject={onNewChat}
                  onDeleted={onChatDeleted}
                />
              ),
              doc: <DocumentPane project={project} />,
              ws: <WorkArea project={project} />,
            }}
          />
        )}
      </div>
    </div>
  );
}

/**
 * Рабочая область: островок плана сверху, ниже две зоны на золотой линии —
 * «Документ» (извлечённое) и «Артефакты». Открытый артефакт занимает всю зону.
 */
function WorkArea({ project }: { project: Project | null }) {
  const ws = useWorkspace();
  const [planOpen, setPlanOpen] = useState(true);
  const [zone, setZone] = useState<"doc" | "artifacts">("doc");
  const [planTab, setPlanTab] = useState<"trace" | "graph">("trace");
  const [graphSelection, setGraphSelection] = useState<GraphSelection | null>(null);
  // Раскрытие выбора документа: живёт во вкладке, а не отдельной строкой.
  const [docsOpen, setDocsOpen] = useState(false);

  // Линии работ и карточки исполнителей — ОДИН разбор истории (`turnProgress`).
  // Раньше это были два независимых memo, и на экране рядом висело «воркер:
  // ГОТОВО» при «шаг: ВЫПОЛНЯЕТСЯ». Теперь обе половины острова выводятся из
  // одного факта, и разъехаться им негде.
  const { lanes, workers } = useMemo(() => turnProgress(project), [project]);

  const plan = useMemo(() => {
    const msgs = project?.messages ?? [];
    // Сначала ищем ход, который ещё идёт: его план и есть «что происходит».
    const runningId = project?.activeChatRun?.assistant_id;
    const running = runningId ? msgs.find((m) => m.id === runningId) : null;
    if (running?.role === "assistant" && running.plan?.length) return running.plan;
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role !== "assistant") continue;
      if (m.plan?.length) return m.plan;
      if (m.toolCalls?.length) {
        return m.toolCalls.map((t) => ({
          title: toolLabel(t.name).step,
          status: t.status === "error" ? ("error" as const) : t.status === "done" ? ("done" as const) : ("running" as const),
          note: t.count > 1 ? `вызовов: ${t.count}` : undefined,
        }));
      }
      // Ход идёт, но инструментов ещё не было — говорим об этом шагом, а не
      // пустым островом: пользователь должен видеть, что агент уже думает.
      if (m.thinking) return [{ title: m.thinking, status: "running" as const }];
      // Плана у этой реплики нет — смотрим более ранние. Обрыв на первой же
      // приводил к «нет активного плана» сразу после обычного вопроса, хотя
      // прошлый разбор со всеми шагами никуда не делся.
    }
    return null;
  }, [project?.messages, project?.activeChatRun?.assistant_id]);

  // Срез 29a, §4.1: построенный PlanGraph занимает ВСЮ рабочую область целиком
  // (не строку над прежними панелями) — тот же поиск «активный/последний ход
  // с данными», что уже у `plan` выше, но по `planGraph`, не по `plan`/
  // `toolCalls`. Граф остаётся на экране после завершения хода и меняется
  // только при переходе в другой чат или после построения следующего плана —
  // «последнее сообщение с planGraph» даёт ровно это поведение бесплатно
  // (реюз project?.messages как единственного источника, без второго стейта).
  const planGraphMsg = useMemo(() => {
    const msgs = project?.messages ?? [];
    const runningId = project?.activeChatRun?.assistant_id;
    return graphMessageForWorkspace(
      msgs.filter((message) => message.role === "assistant"), runningId ?? null,
    );
  }, [project?.messages, project?.activeChatRun?.assistant_id]);

  // Лента гибридного планировщика (ходы planner и попытки автора) — тот же поиск «активный/последний
  // ход с данными», что у графа выше. Показывается вкладкой рядом с графом.
  const traceMsg = useMemo(() => {
    const msgs = project?.messages ?? [];
    const runningId = project?.activeChatRun?.assistant_id;
    const running = runningId ? msgs.find((m) => m.id === runningId) : null;
    if (runningId) return running?.role === "assistant" && running.dynamicTrace ? running : null;
    for (let i = msgs.length - 1; i >= 0; i--) {
      const m = msgs[i];
      if (m.role === "assistant" && m.dynamicTrace) return m;
    }
    return null;
  }, [project?.messages, project?.activeChatRun?.assistant_id]);

  const graphMsg = graphOwnerMessage(traceMsg, planGraphMsg);
  const executionGraphs = useMemo(
    () => graphMsg ? executionGraphsOf(graphMsg) : [],
    [graphMsg],
  );
  const { selected: selectedGraph, moveGraphs, graphCounts } = resolveGraphView(
    executionGraphs, graphMsg?.id, graphSelection,
  );

  // Открытый артефакт показывается на месте зоны — с явным возвратом к списку.
  useEffect(() => {
    if (ws.artifact) setZone("artifacts");
  }, [ws.artifact]);

  // Срез 29a, §4.1: пока показан построенный граф, содержимое рабочей
  // области ПОЛНОСТЬЮ заменяется им — «ПЛАН АГЕНТА»-остров, вкладки
  // Документ/Артефакты, поиск и пустая таблица убираются. Это только смена
  // ПРЕДСТАВЛЕНИЯ: сами документы/результаты/артефакты не удаляются и
  // становятся видны снова, как только появится чат без построенного графа.
  if (traceMsg?.dynamicTrace) {
    // Гибридный планировщик: две вкладки — лента «Планировщик и автор» и построенный граф.
    const showGraph = planTab === "graph" && Boolean(selectedGraph);
    const live = project?.activeChatRun?.assistant_id === traceMsg.id;
    return (
      <div className="flex h-full min-h-0 flex-col">
        <div className="flex shrink-0" role="tablist">
          {(
            [
              ["trace", "Планировщик и автор"],
              ["graph", "Граф исполнения"],
            ] as const
          ).map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={id === (showGraph ? "graph" : "trace")}
              disabled={id === "graph" && !selectedGraph}
              onClick={() => setPlanTab(id)}
              className={cn(
                "flex flex-1 items-center justify-center border-b-2 px-4 py-2.5 text-[13px] transition-colors disabled:opacity-50",
                id === (showGraph ? "graph" : "trace")
                  ? "border-[var(--color-accent)] text-[var(--color-text)]"
                  : "border-[var(--color-divider)] text-[var(--color-neutral-600)] hover:bg-[var(--color-accent-100)]",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        <div className="min-h-0 flex-1">
          {showGraph && selectedGraph ? (
            <div className="flex h-full min-h-0 flex-col">
              {moveGraphs.length > 1 ? (
                <div className="flex shrink-0 gap-1 border-b border-[var(--color-divider)] px-3 py-2">
                  {moveGraphs.map((graph, index) => (
                    <button
                      key={graph.id}
                      type="button"
                      className={cn(
                        "rounded border px-2 py-1 text-[11px]",
                        graph.id === selectedGraph.id
                          ? "border-[var(--color-accent)] text-[var(--color-text)]"
                          : "border-[var(--color-divider)] text-[var(--color-neutral-600)]",
                      )}
                      onClick={() => setGraphSelection({
                        messageId: graphMsg?.id, move: selectedGraph.move, graphId: graph.id,
                      })}
                    >
                      Граф {index + 1}
                    </button>
                  ))}
                </div>
              ) : null}
              <div className="min-h-0 flex-1">
                <PlanGraphWorkspace
                  graph={selectedGraph.graph}
                  nodeRuns={selectedGraph.nodeRuns}
                  terminal={selectedGraph.terminal ? {
                    ...selectedGraph.terminal, startedAtMs: selectedGraph.openedAtMs,
                  } : undefined}
                />
              </div>
            </div>
          ) : (
            <DynamicRunTraceView
              trace={traceMsg.dynamicTrace}
              live={live}
              graphCounts={graphCounts}
              onSelectGraphMove={(move) => {
                setGraphSelection({ messageId: graphMsg?.id, move, graphId: null });
                setPlanTab("graph");
              }}
            />
          )}
        </div>
      </div>
    );
  }

  if (selectedGraph) {
    return (
      <PlanGraphWorkspace
        graph={selectedGraph.graph}
        nodeRuns={selectedGraph.nodeRuns}
        terminal={selectedGraph.terminal ? {
          ...selectedGraph.terminal, startedAtMs: selectedGraph.openedAtMs,
        } : undefined}
      />
    );
  }

  return (
    <div className="flex h-full flex-col">
      <PlanIsland plan={plan} lanes={lanes} workers={workers} open={planOpen} onOpenChange={setPlanOpen} />

      {/* ВЫБОР ЖИВЁТ В САМОЙ ВКЛАДКЕ (правка Nate 18.08). Сначала список
          документов и вид стояли отдельной строкой над поиском — владелец
          назвал это лишней кнопкой: «выпадающий список должен быть в самой
          кнопке, которая пишет „Документ ГК «Самолет»“».
          Спора с переключением зоны нет: первый щелчок по неактивной вкладке
          переключает зону, второй — по уже активной — раскрывает выбор. */}
      <div className="relative flex shrink-0">
        {(
          [
            ["doc", "Документы"],
            ["artifacts", "Артефакты"],
          ] as const
        ).map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-haspopup={id === "doc" && ws.docs.length > 0 ? "listbox" : undefined}
            aria-expanded={id === "doc" ? docsOpen : undefined}
            onClick={() => {
              if (id === "artifacts") {
                setZone(id);
                return;
              }
              ws.openArtifact(null);
              if (zone === "doc" && ws.docs.length > 0) setDocsOpen((v) => !v);
              else setZone(id);
            }}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 border-b-2 px-4 py-2.5 text-[13px] transition-colors",
              zone === id
                ? "border-[var(--color-accent)] text-[var(--color-text)]"
                : "border-[var(--color-divider)] text-[var(--color-neutral-600)] hover:bg-[var(--color-accent-100)]",
            )}
          >
            {/* Имя документа несёт ТИП первым словом — «Отчётность ГК Самолет»:
                при нескольких документах компания у них одна, различает их тип. */}
            {id === "doc" && zone === "doc" && ws.activeDoc ? (
              <>
                <span className="min-w-0 truncate">{docTitle(ws.activeDoc)}</span>
                {ws.docs.length > 0 && (
                  <ChevronDown
                    className={cn("size-3.5 shrink-0 transition-transform", docsOpen && "rotate-180")}
                  />
                )}
              </>
            ) : (
              label
            )}
          </button>
        ))}

        {docsOpen && zone === "doc" && ws.docs.length > 0 && (
          <DocPicker onClose={() => setDocsOpen(false)} />
        )}
      </div>

      {/* Именно flex-КОЛОНКА: внутренние панели растягиваются на высоту зоны и
          их собственная прокрутка получает границу. Блочный контейнер высоты не
          давал — карточка анализа просто росла и обрезалась без полосы. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {zone === "doc" ? <ExtractedPane /> : <ArtifactsPane />}
      </div>
    </div>
  );
}
