import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import type { DocumentSummary } from "./api";
import { docTitle, docTypeRu } from "@/components/workspace/workspace-context";
import { formulaRu } from "@/components/workspace/panes/extracted-table";

/**
 * Имя документа и формула — на человеческом языке (правки Nate 18.08).
 *
 * ДВА РАЗНЫХ ТРЕБОВАНИЯ, ОДНА ПРИЧИНА. На экране висели внутренние обозначения:
 * надпись «ОТЧЁТНОСТЬ» отдельной подписью в шапке (шум — зона и так про
 * документ) и `NET_PROFIT / TOTAL_EQUITY` в подсказке к «расчёту». Аналитик
 * спрашивает «как это посчитано» и получает коды реестра.
 *
 * Тип документа теперь идёт ПЕРВЫМ СЛОВОМ имени: при нескольких документах в
 * чате компания у них одна, а различаются они именно типом.
 */
const doc = (over: Partial<DocumentSummary>): DocumentSummary =>
  ({
    doc_id: "d1",
    filename: "otp-2024.pdf",
    doc_type: "",
    company_name: "ОТП Банк",
    period: "2024",
    language: "ru",
    pages: 92,
    ...over,
  }) as DocumentSummary;

describe("имя документа несёт тип", () => {
  it("отчётность, пресс-релиз и транскрипт называются по-разному", () => {
    expect(docTitle(doc({ doc_type: "ifrs_report" }))).toBe("Отчётность ОТП Банк");
    expect(docTitle(doc({ doc_type: "rsbu_report" }))).toBe("Отчётность ОТП Банк");
    expect(docTitle(doc({ doc_type: "press_release" }))).toBe("Пресс-релиз ОТП Банк");
    expect(docTitle(doc({ doc_type: "transcript" }))).toBe("Транскрипт ОТП Банк");
  });

  it("не-PDF получают своё слово, а не «Отчётность»", () => {
    // Впереди счета, выгрузки и модели (#273). Назвать таблицу отчётностью
    // значило бы соврать о том, что за документ открыт.
    expect(docTypeRu(doc({ doc_type: "excel" }))).toBe("Таблица");
    expect(docTypeRu(doc({ doc_type: "presentation" }))).toBe("Презентация");
  });

  it("явное «прочее» не переигрывается догадкой по имени файла", () => {
    // Иначе ручная правка вида (PATCH /documents) не имела бы силы: выбрал
    // «прочее», а экран снова пишет «Отчётность».
    expect(docTypeRu(doc({ doc_type: "other" }))).toBe("Документ");
  });

  it("без типа судим по имени файла — тем же правилом, что и вкладки", () => {
    expect(docTypeRu(doc({ doc_type: "", filename: "пресс-релиз-2024.pdf" }))).toBe("Пресс-релиз");
    expect(docTypeRu(doc({ doc_type: "", filename: "otp-2024.pdf" }))).toBe("Отчётность");
  });

  it("без документа вкладка называется «Документы»", () => {
    expect(docTitle(null)).toBe("Документы");
  });
});

describe("формула читается по-русски", () => {
  const names = new Map([
    ["NET_PROFIT", "Чистая прибыль"],
    ["TOTAL_EQUITY", "Итого собственный капитал"],
  ]);

  it("коды реестра заменяются именами показателей", () => {
    expect(formulaRu("NET_PROFIT / TOTAL_EQUITY", names)).toBe(
      "Чистая прибыль / Итого собственный капитал",
    );
  });

  it("код, которого нет в прогоне, остаётся кодом", () => {
    // Перевести нечем — выдумывать имя нельзя, это было бы враньём о том,
    // что за показатель участвовал в расчёте.
    expect(formulaRu("NET_PROFIT / RWA_TOTAL", names)).toBe("Чистая прибыль / RWA_TOTAL");
  });

  it("без словаря формула не портится", () => {
    expect(formulaRu("NET_PROFIT / TOTAL_EQUITY")).toBe("NET_PROFIT / TOTAL_EQUITY");
  });
});

describe("шапка рабочей области очищена", () => {
  const PANE = readFileSync(
    fileURLToPath(new URL("../components/workspace/panes/extracted-pane.tsx", import.meta.url)),
    "utf8",
  );

  it("слова «отчётность» в шапке больше нет", () => {
    expect(PANE).not.toContain("ОТЧЁТНОСТЬ");
    expect(PANE).not.toContain("KIND_RU");
  });

  it("поиск подписан коротко", () => {
    expect(PANE).toContain('placeholder="Поиск"');
    expect(PANE).not.toContain("Поиск по показателям");
  });
});

describe("выбор роли НЕ запускает извлечение", () => {
  const ACTIONS = readFileSync(
    fileURLToPath(new URL("../components/workspace/panes/doc-actions.tsx", import.meta.url)),
    "utf8",
  );

  it("в обработчике пункта роли нет запуска хода", () => {
    // Выпадашка — это ВЫБОР формы, а не команда. Запуск остаётся за самой
    // кнопкой «Извлечь метрики»: иначе просмотр списка ролей стоил бы прогона.
    const from = ACTIONS.indexOf("ROLE_OPTIONS.map");
    const menu = ACTIONS.slice(from, ACTIONS.indexOf("</div>", from));
    expect(menu).toContain("savePrefs");
    // Раскладку ниже перестраивает подписка на `finengine:prefs`, а не колбэк
    // отсюда: колбэк был вторым входом в тот же запрос (проверяется в
    // role-table-contract.test.ts). Здесь предмет один — ход не запускается.
    expect(menu).not.toContain("run.send");
    expect(menu).not.toContain("fire()");
  });

  it("ход отправляет только обработчик кнопки", () => {
    expect(ACTIONS).toContain("onClick={fire}");
    expect(ACTIONS.match(/run\.send\(/g) ?? []).toHaveLength(1);
  });
});

describe("тумблер пути хода снят, путь зафиксирован", () => {
  const RUN = readFileSync(
    fileURLToPath(new URL("../components/workspace/use-chat-run.ts", import.meta.url)),
    "utf8",
  );
  const HEADER = readFileSync(
    fileURLToPath(new URL("../components/workspace/workspace-header.tsx", import.meta.url)),
    "utf8",
  );

  it("шапка больше не рисует выбор пути", () => {
    expect(HEADER).not.toContain("AgentPathSwitch");
  });

  it("ход уходит по петле гейтвея, а не по тому, что осталось в настройках", () => {
    // 🔴 БЕЗ ЭТОГО СНОС ТУМБЛЕРА — ЛОВУШКА. Петлю выбирает БЭКЕНД по значению
    // arch, а FINENGINE_CHAT_GATEWAY не задан ни на dev, ни на проде
    // (проверено 18.08). Убрали бы только кнопку — все, у кого в localStorage
    // остался `finengine_baseline`, навсегда уехали бы мёртвым путём.
    expect(RUN).toContain('const arch = "aifa_gateway"');
    expect(RUN).not.toContain("p.arch || prefs.arch");
  });
});
