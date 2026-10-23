/**
 * Движок раскладки рабочей строки (дизайн-хендофф, экран «работа с документами»).
 *
 * Три панели — чат, исходный документ, рабочая область — живут в одной строке.
 * Каждая может быть: пристёгнутой (доля ширины), свёрнутой в корешок 34px или
 * отстёгнутой в плавающее окно. Здесь только ЧИСТАЯ математика раскладки:
 * состояние, ширины, «гармошка» при перетаскивании границы, снап к верхней
 * кромке. Указатели/DOM — в use-panel-layout.ts, вид — в компонентах.
 *
 * Правила взяты из эталона:
 *  - минимумы: чат 320, документ 260, рабочая область 280;
 *  - ровно ОДНА пристёгнутая панель «тянущаяся» (забирает остаток ширины) —
 *    чат, если открыт, иначе последняя открытая; у остальных ширина явная,
 *    иначе границы нечего было бы тащить;
 *  - при перетаскивании границы ширина забирается у колонок с дальней стороны
 *    по очереди: упёрлась соседняя в минимум — давление идёт в следующую.
 */

export type PanelId = "chat" | "doc" | "ws";

export type FloatBox = { x: number; y: number; w: number; h: number };

export type Layout = {
  /** Порядок колонок слева направо. */
  ord: PanelId[];
  /** Явные ширины пристёгнутых панелей. */
  w: Partial<Record<PanelId, number>>;
  /** Свёрнута в корешок. */
  col: Record<PanelId, boolean>;
  /** Отстёгнута в плавающее окно (или null). */
  fl: Record<PanelId, FloatBox | null>;
  /** Панель, прижатая к боковой стенке. Её ширина ФИКСИРОВАНА и она никогда не
   *  «тянущаяся» — иначе, оставшись единственной колонкой, она разъезжалась бы
   *  во всю строку поверх плавающих окон. */
  pin?: { id: PanelId; side: "left" | "right" } | null;
};

export const PANEL_IDS: PanelId[] = ["chat", "doc", "ws"];

export const PANEL_MIN: Record<PanelId, number> = { chat: 320, doc: 260, ws: 280 };

export const PANEL_TITLE: Record<PanelId, string> = {
  chat: "Чат",
  doc: "План",
  ws: "Рабочая область",
};

/** Ширина корешка свёрнутой панели и зона границы между панелями. */
export const SPINE_W = 34;
export const SPLIT_W = 13;
/** Минимальная высота плавающего окна. */
export const FLOAT_MIN_H = 200;
/** Насколько близко к верхней кромке нужно поднести окно, чтобы оно пристало. */
export const SNAP_ZONE = 34;
/** Ширина полосы у боковой стенки, в которой срабатывает примагничивание. */
export const EDGE_ZONE = 56;
/** Высота угловой зоны: попал в неё у стенки — получишь четвертинку, а не
 *  колонку во всю высоту. */
export const CORNER_ZONE = 120;
/** Доля ширины строки, которую занимает панель, прижатая к боковой стенке. */
export const EDGE_SHARE = 1 / 3;
/** Доли для угла: ровно четверть рабочей строки. */
export const CORNER_W_SHARE = 1 / 2;
export const CORNER_H_SHARE = 1 / 2;

/**
 * Куда встанет панель, если отпустить её прямо сейчас.
 *
 * `slot` — прежнее поведение (поднёс к верхней кромке → колонка между другими).
 * `edge` — прижал к боковой стенке: панель становится КРАЙНЕЙ в своём порядке
 *   и занимает треть ширины на всю высоту.
 * `corner` — прижал к углу: четвертинка рабочей строки в этом углу
 *   (остаётся плавающей — это «отложить рядом», а не колонка).
 */
export type SnapTarget =
  | { kind: "slot"; slot: number | null }
  | { kind: "edge"; side: "left" | "right" }
  | { kind: "corner"; side: "left" | "right"; vert: "top" | "bottom" };

/**
 * Ширина панели, прижатой к стенке.
 *
 * По умолчанию треть строки, НО если с другой стороны висят плавающие окна —
 * панель доходит ровно до их края. Иначе между ней и окнами зияла бы полоса
 * пустоты (окна-четвертинки занимают половину, а панель брала треть).
 */
export function edgeWidth(
  lay: Layout,
  side: "left" | "right",
  frame: { width: number },
): number {
  const floats = PANEL_IDS.map((id) => lay.fl[id]).filter(Boolean) as FloatBox[];
  const fallback = Math.round(frame.width * EDGE_SHARE);
  if (side === "left") {
    // Окна, стоящие правее середины: панель упирается в самое левое из них.
    const edges = floats.filter((f) => f.x > frame.width * 0.4).map((f) => f.x);
    return edges.length ? Math.max(PANEL_MIN.doc, Math.round(Math.min(...edges))) : fallback;
  }
  const edges = floats.filter((f) => f.x + f.w < frame.width * 0.6).map((f) => f.x + f.w);
  return edges.length
    ? Math.max(PANEL_MIN.doc, Math.round(frame.width - Math.max(...edges)))
    : fallback;
}

/** Прямоугольник подсветки для цели — в координатах рабочей строки. */
export function snapRect(
  target: SnapTarget,
  frame: { width: number; height: number },
  fallback: { left: number; width: number },
  edgeW?: number,
): { left: number; top: number; width: number; height: number } {
  if (target.kind === "slot") {
    return { left: fallback.left, top: 0, width: fallback.width, height: frame.height };
  }
  if (target.kind === "edge") {
    const w = edgeW ?? Math.round(frame.width * EDGE_SHARE);
    return {
      left: target.side === "left" ? 0 : frame.width - w,
      top: 0,
      width: w,
      height: frame.height,
    };
  }
  const w = Math.round(frame.width * CORNER_W_SHARE);
  const h = Math.round(frame.height * CORNER_H_SHARE);
  return {
    left: target.side === "left" ? 0 : frame.width - w,
    top: target.vert === "top" ? 0 : frame.height - h,
    width: w,
    height: h,
  };
}

export const DEFAULT_LAYOUT: Layout = {
  ord: ["chat", "doc", "ws"],
  w: {},
  col: { chat: false, doc: false, ws: false },
  fl: { chat: null, doc: null, ws: null },
  pin: null,
};

/** Панель пристёгнута и видна (не корешок, не плавающая). */
export function isDocked(lay: Layout, id: PanelId): boolean {
  return !lay.col[id] && !lay.fl[id];
}

/** Пристёгнутые колонки в порядке раскладки. */
export function openCols(lay: Layout): PanelId[] {
  return lay.ord.filter((id) => isDocked(lay, id));
}

/**
 * Та единственная колонка, что забирает остаток ширины. Чат, если он открыт —
 * иначе последняя открытая. У всех прочих ширина явная: это и делает границы
 * перетаскиваемыми (иначе flex сам бы всё пересчитал под курсором).
 */
export function stretchId(lay: Layout): PanelId | null {
  // Прижатая к стенке панель держит свою ширину и остаток не забирает.
  const open = openCols(lay).filter((id) => id !== lay.pin?.id);
  if (open.includes("chat")) return "chat";
  return open.length ? open[open.length - 1] : null;
}

/** Ширина одной открытой панели при равном делении рабочей строки. */
export function shareWidth(lay: Layout, frameWidth: number, railWidth: number): number {
  const open = openCols(lay).length;
  const spines = PANEL_IDS.filter((id) => lay.col[id]).length;
  const avail =
    frameWidth - railWidth - spines * SPINE_W - SPLIT_W * Math.max(0, open - 1);
  return Math.max(260, Math.round(avail / Math.max(1, open)));
}

/**
 * Раздать пристёгнутым панелям поровну. Нужно после того, как сосед свернулся,
 * отстегнулся или вернулся: иначе у него осталась бы «протухшая» ширина и
 * справа зияла бы пустота.
 */
export function evenWidths(lay: Layout, frameWidth: number, railWidth: number): Layout["w"] {
  const share = shareWidth(lay, frameWidth, railWidth);
  const w: Layout["w"] = { ...lay.w };
  // Тянущейся колонке ширина не нужна — она забирает остаток.
  const stretch = stretchId(lay);
  for (const id of PANEL_IDS) {
    if (isDocked(lay, id) && id !== stretch) w[id] = share;
  }
  return w;
}

/**
 * «Гармошка»: пересчёт ширин при перетаскивании границы между колонками
 * `cols[i]` и `cols[i+1]` на `dx` пикселей.
 *
 * `startWidths` — ширины на момент нажатия (замеренные, а не из состояния:
 * тянущаяся колонка своей ширины в состоянии не имеет).
 */
export function resizeBySplit(
  cols: PanelId[],
  startWidths: Record<string, number>,
  i: number,
  dx: number,
): Record<string, number> {
  const next: Record<string, number> = { ...startWidths };
  // Отдают ширину те, кто с дальней стороны от направления движения.
  const give = dx > 0 ? cols.slice(i + 1) : cols.slice(0, i + 1).reverse();
  const grow = dx > 0 ? cols[i] : cols[i + 1];
  if (!grow) return next;

  let room = 0;
  for (const c of give) room += startWidths[c] - PANEL_MIN[c as PanelId];
  let rest = Math.min(Math.abs(dx), Math.max(0, room));
  const taken = rest;

  for (let j = 0; j < give.length && rest > 0.5; j++) {
    const c = give[j];
    const cut = Math.min(rest, startWidths[c] - PANEL_MIN[c as PanelId]);
    next[c] = startWidths[c] - cut;
    rest -= cut;
  }
  next[grow] = startWidths[grow] + taken;
  return next;
}

/**
 * Двойной клик по полосе захвата: панель на всю рабочую область (остальные —
 * в корешки), повторный — назад к раскладке.
 */
export function toggleMaximize(lay: Layout, id: PanelId): Layout {
  const others = PANEL_IDS.filter((x) => x !== id);
  const isMax = others.every((x) => lay.col[x]);
  return {
    ord: lay.ord,
    w: {},
    col: {
      chat: isMax ? false : id !== "chat",
      doc: isMax ? false : id !== "doc",
      ws: isMax ? false : id !== "ws",
    },
    fl: { chat: null, doc: null, ws: null },
  };
}

/** Куда встанет отстёгнутое окно, если бросить его у верхней кромки на x. */
export function slotAt(
  lay: Layout,
  x: number,
  dragged: PanelId,
  railWidth: number,
  colWidths: Partial<Record<PanelId, number>>,
): number | null {
  const others = lay.ord.filter((id) => id !== dragged && isDocked(lay, id));
  if (!others.length) return 0;
  let edge = railWidth;
  for (let i = 0; i < others.length; i++) {
    const w = colWidths[others[i]] ?? PANEL_MIN[others[i]];
    if (x < edge + w / 2) return i;
    edge += w + SPLIT_W;
  }
  return others.length;
}

/**
 * Куда встанет панель при текущем положении курсора.
 *
 * Порядок проверок задаёт приоритет: угол сильнее стенки (иначе до угла было бы
 * не дотянуться — боковая полоса перехватывала бы его), стенка сильнее верхней
 * кромки. Координаты — относительно рабочей строки.
 */
export function snapTargetAt(
  lay: Layout,
  x: number,
  y: number,
  frame: { width: number; height: number },
  dragged: PanelId,
  railWidth: number,
  colWidths: Partial<Record<PanelId, number>>,
): SnapTarget | null {
  const nearLeft = x < EDGE_ZONE;
  const nearRight = x > frame.width - EDGE_ZONE;
  if (nearLeft || nearRight) {
    const side = nearLeft ? "left" : "right";
    if (y < CORNER_ZONE) return { kind: "corner", side, vert: "top" };
    if (y > frame.height - CORNER_ZONE) return { kind: "corner", side, vert: "bottom" };
    return { kind: "edge", side };
  }
  if (y < SNAP_ZONE) {
    return { kind: "slot", slot: slotAt(lay, x + railWidth, dragged, railWidth, colWidths) };
  }
  return null;
}

/**
 * Прижать панель к боковой стенке: она становится КРАЙНЕЙ в порядке колонок и
 * получает треть ширины строки. Ширина проставляется явно — иначе выравнивание
 * долей тут же вернуло бы прежний размер.
 */
export function dockToEdge(
  lay: Layout,
  id: PanelId,
  side: "left" | "right",
  frameWidth: number,
): Layout {
  const width = edgeWidth(lay, side, { width: frameWidth });
  const docked = dockAt(lay, id, side === "left" ? 0 : null);
  // `pin` держит ширину: без него панель, оставшись единственной колонкой,
  // становилась «тянущейся» и разъезжалась во всю строку поверх окон.
  return { ...docked, pin: { id, side }, w: { ...docked.w, [id]: width } };
}

/** Пристегнуть плавающую панель обратно в позицию `slot`. */
export function dockAt(lay: Layout, id: PanelId, slot: number | null): Layout {
  const others = lay.ord.filter((k) => k !== id);
  const idx = slot === null ? others.length : Math.min(slot, others.length);
  const ord = [...others];
  ord.splice(idx, 0, id);
  return {
    ord: ord as PanelId[],
    w: {},
    col: { ...lay.col, [id]: false },
    fl: { ...lay.fl, [id]: null },
    // Панель переехала — прежний прижим к стенке снимается (dockToEdge
    // выставит свой сразу после этого вызова).
    pin: lay.pin?.id === id ? null : lay.pin,
  };
}
