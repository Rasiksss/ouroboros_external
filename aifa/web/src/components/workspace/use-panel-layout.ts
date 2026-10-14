"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  DEFAULT_LAYOUT,
  FLOAT_MIN_H,
  PANEL_IDS,
  PANEL_MIN,
  dockAt,
  dockToEdge,
  edgeWidth,
  evenWidths,
  isDocked,
  openCols,
  resizeBySplit,
  shareWidth,
  snapRect,
  snapTargetAt,
  stretchId,
  toggleMaximize,
  type Layout,
  type PanelId,
  type SnapTarget,
} from "@/lib/workspace-layout";

const STORAGE_KEY = "aifa:workspace-layout:v1";

type DragState =
  | { mode: "split"; x0: number; i: number; cols: PanelId[]; w0: Record<string, number> }
  | {
      mode: "move" | "resize";
      id: PanelId;
      x0: number;
      y0: number;
      base: { x: number; y: number; w: number; h: number };
      dir?: string;
      pending: boolean;
    };

/**
 * Состояние раскладки рабочей строки + жесты (границы, отстёгивание,
 * ресайз плавающего окна). Раскладка переживает перезагрузку — она часть
 * рабочего места, а не «оформления».
 */
export function usePanelLayout(railWidth: number) {
  const [lay, setLay] = useState<Layout>(DEFAULT_LAYOUT);
  /** Подсветка «сюда встанет»: верхняя кромка (колонка), боковая стенка (треть
   *  на всю высоту) или угол (четверть). */
  const [snap, setSnap] = useState<{ id: PanelId; target: SnapTarget } | null>(null);
  /** На время перетаскивания переходы отключаются — иначе граница отстаёт. */
  const [dragging, setDragging] = useState(false);

  const frameRef = useRef<HTMLDivElement | null>(null);
  const panelRefs = useRef<Record<PanelId, HTMLDivElement | null>>({
    chat: null,
    doc: null,
    ws: null,
  });
  const dragRef = useRef<DragState | null>(null);

  // Восстановление сохранённой раскладки.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw) as Partial<Layout>;
        if (saved?.ord?.length === 3) {
          setLay({ ...DEFAULT_LAYOUT, ...saved } as Layout);
        }
      }
    } catch {
      /* нет доступа к хранилищу — работаем с раскладкой по умолчанию */
    }
  }, []);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(lay));
    } catch {
      /* no-op */
    }
  }, [lay]);

  const frameBox = useCallback(() => {
    const el = frameRef.current;
    if (!el) return { left: 0, top: 0, width: 1280, height: 800 };
    const r = el.getBoundingClientRect();
    return { left: r.left, top: r.top, width: r.width, height: r.height };
  }, []);

  const measuredWidths = useCallback(() => {
    const out: Record<string, number> = {};
    for (const id of PANEL_IDS) {
      const el = panelRefs.current[id];
      out[id] = el ? el.offsetWidth : lay.w[id] ?? PANEL_MIN[id];
    }
    return out;
  }, [lay.w]);

  // ── жесты ────────────────────────────────────────────────────────────────
  const startSplit = useCallback(
    (slot: number, e: React.PointerEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const cols = openCols(lay);
      if (slot < 0 || slot + 1 >= cols.length) return;
      dragRef.current = {
        mode: "split",
        x0: e.clientX,
        i: slot,
        cols,
        w0: measuredWidths(),
      };
      setDragging(true);
    },
    [lay, measuredWidths],
  );

  const startMove = useCallback(
    (id: PanelId, e: React.PointerEvent) => {
      if (e.button !== 0) return;
      // Без preventDefault браузер начинает штатное выделение текста и к концу
      // жеста подсвечен весь экран.
      e.preventDefault();
      const el = panelRefs.current[id];
      const fb = frameBox();
      const cur = lay.fl[id];
      const base = cur ?? {
        x: el ? el.getBoundingClientRect().left - fb.left : 0,
        y: 0,
        w: el ? el.offsetWidth : PANEL_MIN[id],
        h: fb.height,
      };
      dragRef.current = { mode: "move", id, x0: e.clientX, y0: e.clientY, base, pending: true };
      setDragging(true);
    },
    [frameBox, lay.fl],
  );

  const startResize = useCallback(
    (id: PanelId, dir: string, e: React.PointerEvent) => {
      if (e.button !== 0) return;
      e.preventDefault();
      e.stopPropagation();
      const cur = lay.fl[id];
      if (!cur) return;
      dragRef.current = {
        mode: "resize",
        id,
        dir,
        x0: e.clientX,
        y0: e.clientY,
        base: cur,
        pending: false,
      };
      setDragging(true);
    },
    [lay.fl],
  );

  // Пока идёт жест — запрет выделения на весь документ и сброс уже выделенного:
  // preventDefault на pointerdown не спасает, если выделение началось раньше или
  // курсор ушёл за пределы панели.
  useEffect(() => {
    if (!dragging) return;
    document.body.classList.add("aifa-dragging");
    window.getSelection()?.removeAllRanges();
    return () => document.body.classList.remove("aifa-dragging");
  }, [dragging]);

  useEffect(() => {
    if (!dragging) return;

    const onMove = (e: PointerEvent) => {
      const d = dragRef.current;
      if (!d) return;
      const fb = frameBox();
      const dx = e.clientX - d.x0;

      if (d.mode === "split") {
        const next = resizeBySplit(d.cols, d.w0, d.i, dx);
        const stretch = stretchId(lay);
        setLay((s) => {
          const w = { ...s.w };
          for (const c of d.cols) if (c !== stretch) w[c] = Math.round(next[c]);
          return { ...s, w };
        });
        return;
      }

      const dy = e.clientY - d.y0;
      if (d.pending) {
        // Порог в 4px: клик по полосе не должен отрывать панель.
        if (Math.abs(dx) + Math.abs(dy) <= 4) return;
        d.pending = false;
      }

      if (d.mode === "move") {
        const target = snapTargetAt(
          lay,
          e.clientX - fb.left,
          e.clientY - fb.top,
          fb,
          d.id,
          railWidth,
          measuredWidths(),
        );
        setSnap(target ? { id: d.id, target } : null);
        setLay((s) => ({
          ...s,
          fl: {
            ...s.fl,
            [d.id]: {
              x: Math.max(-40, Math.min(fb.width - 90, Math.round(d.base.x + dx))),
              y: Math.max(0, Math.min(fb.height - 40, Math.round(d.base.y + dy))),
              w: d.base.w,
              h: d.base.h,
            },
          },
        }));
        return;
      }

      // resize: восемь зон — четыре угла по диагонали, четыре стороны.
      const dir = d.dir || "se";
      const b = d.base;
      let { x, y, w, h } = b;
      const minW = PANEL_MIN[d.id];
      if (dir.includes("e")) w = Math.max(minW, Math.round(b.w + dx));
      if (dir.includes("s")) h = Math.max(FLOAT_MIN_H, Math.round(b.h + dy));
      if (dir.includes("w")) {
        w = Math.max(minW, Math.round(b.w - dx));
        x = Math.round(b.x + (b.w - w));
      }
      if (dir.includes("n")) {
        h = Math.max(FLOAT_MIN_H, Math.round(b.h - dy));
        y = Math.round(b.y + (b.h - h));
      }
      setLay((s) => ({ ...s, fl: { ...s.fl, [d.id]: { x, y, w, h } } }));
    };

    const onUp = () => {
      const d = dragRef.current;
      dragRef.current = null;
      setDragging(false);
      // Брошено в зоне примагничивания → панель встаёт ровно туда, куда
      // показывала подсветка.
      if (d && d.mode === "move" && snap) {
        const t = snap.target;
        setLay((s) => {
          const fb = frameBox();
          if (t.kind === "slot") {
            const docked = dockAt(s, d.id, t.slot);
            return { ...docked, w: evenWidths(docked, fb.width, railWidth) };
          }
          if (t.kind === "edge") {
            // Прижатая панель — крайняя в порядке, с фиксированной шириной;
            // остальные делят остаток, поэтому ширины пересчитываем ПОСЛЕ неё
            // и её собственную возвращаем поверх результата.
            const docked = dockToEdge(s, d.id, t.side, fb.width);
            const rest = evenWidths(docked, fb.width, railWidth);
            return { ...docked, w: { ...rest, [d.id]: docked.w[d.id]! } };
          }
          // Угол: остаётся плавающей четвертинкой — это «отложить рядом»,
          // а не колонка в строке.
          const r = snapRect(t, fb, { left: 0, width: 0 });
          return {
            ...s,
            col: { ...s.col, [d.id]: false },
            fl: { ...s.fl, [d.id]: { x: r.left, y: r.top, w: r.width, h: r.height } },
          };
        });
      }
      setSnap(null);
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
  }, [dragging, frameBox, lay, measuredWidths, railWidth, snap]);

  // ── команды панели ───────────────────────────────────────────────────────
  /** Свернуть в корешок / развернуть обратно. */
  const toggleCollapse = useCallback(
    (id: PanelId) => {
      setLay((s) => {
        const next: Layout = {
          ...s,
          col: { ...s.col, [id]: !s.col[id] },
          fl: { ...s.fl, [id]: null },
        };
        const fb = frameBox();
        return { ...next, w: evenWidths(next, fb.width, railWidth) };
      });
    },
    [frameBox, railWidth],
  );

  /** Отстегнуть в плавающее окно (кнопка/перетаскивание) или вернуть назад. */
  const dock = useCallback(
    (id: PanelId) => {
      setLay((s) => {
        const next = dockAt(s, id, s.ord.indexOf(id));
        const fb = frameBox();
        return { ...next, w: evenWidths(next, fb.width, railWidth) };
      });
    },
    [frameBox, railWidth],
  );

  /** Выставить свёрнутость нескольким панелям разом (просьба извне). */
  const setCollapsed = useCallback(
    (next: Partial<Record<PanelId, boolean>>) => {
      setLay((s) => {
        const col = { ...s.col };
        const opened: PanelId[] = [];
        for (const id of PANEL_IDS) {
          if (next[id] === undefined) continue;
          // Плавающее окно УЖЕ видно — просьба «покажи панель» к нему не
          // относится. Раньше её пристёгивали обратно в колонку, и каждая
          // отправка сообщения перекраивала расстановку окон, которую
          // пользователь выставил руками.
          if (s.fl[id]) continue;
          if (s.col[id] && next[id] === false) opened.push(id);
          col[id] = !!next[id];
        }
        const lay: Layout = { ...s, col };
        // Ширины УЖЕ открытых панелей не трогаем: отправка сообщения не повод
        // перекраивать рабочее место. Долю получают только те, что реально
        // раскрылись из корешка, — остаток отдаёт тянущаяся колонка.
        if (!opened.length) return lay;
        const fb = frameBox();
        const share = shareWidth(lay, fb.width, railWidth);
        const w = { ...lay.w };
        for (const id of opened) w[id] = share;
        return { ...lay, w };
      });
    },
    [frameBox, railWidth],
  );

  const maximize = useCallback((id: PanelId) => {
    setLay((s) => toggleMaximize(s, id));
  }, []);

  /** Ширина панели для стиля: тянущаяся — flex, остальные — явная. */
  const widthOf = useCallback(
    (id: PanelId): { flex: string; width: string } => {
      const stretch = stretchId(lay);
      if (id === stretch) return { flex: "1 1 0", width: "auto" };
      const fb = frameBox();
      const w = lay.w[id] ?? shareWidth(lay, fb.width, railWidth);
      return { flex: "0 0 auto", width: `${w}px` };
    },
    [frameBox, lay, railWidth],
  );

  /** Левый край подсказки стыковки — по ФАКТИЧЕСКИМ ширинам колонок, а не по
   *  условному шагу: иначе золотая колонка обещает одно место, а панель встаёт
   *  в другое. */
  const snapLeft = useCallback(
    (slot: number | null): number => {
      const cols = openCols(lay).filter((id) => id !== snap?.id);
      const w = measuredWidths();
      const idx = slot === null ? cols.length : Math.min(slot, cols.length);
      let left = 0;
      for (let i = 0; i < idx; i++) left += (w[cols[i]] ?? PANEL_MIN[cols[i]]) + 13;
      return left;
    },
    [lay, measuredWidths, snap?.id],
  );

  /** Ширина подсказки = доля, которую панель получит после стыковки. */
  const snapWidth = useCallback(() => {
    const fb = frameBox();
    return shareWidth(lay, fb.width, railWidth);
  }, [frameBox, lay, railWidth]);

  /** Прямоугольник подсветки для текущей цели — его и рисует рамка. */
  const snapBox = useCallback(() => {
    if (!snap) return null;
    const fb = frameBox();
    const slotLeft = snap.target.kind === "slot" ? snapLeft(snap.target.slot) : 0;
    // Ширину прижатия берём ТОЙ ЖЕ функцией, что и стыковка, — подсветка обязана
    // обещать ровно то, что получится.
    const edgeW =
      snap.target.kind === "edge" ? edgeWidth(lay, snap.target.side, fb) : undefined;
    return snapRect(snap.target, fb, { left: slotLeft, width: snapWidth() }, edgeW);
  }, [snap, frameBox, snapLeft, snapWidth, lay]);

  return {
    lay,
    snap,
    snapLeft,
    snapWidth,
    snapBox,
    dragging,
    frameRef,
    panelRefs,
    startSplit,
    startMove,
    startResize,
    toggleCollapse,
    setCollapsed,
    dock,
    maximize,
    widthOf,
    isDocked: (id: PanelId) => isDocked(lay, id),
    openCols: () => openCols(lay),
  };
}
