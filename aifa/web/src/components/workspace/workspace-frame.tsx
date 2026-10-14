"use client";

import { Fragment, useEffect, useRef, type ReactNode } from "react";
import { usePanelLayout } from "./use-panel-layout";
import { useWorkspace } from "./workspace-context";
import { PanelSpine, PanelSplitter, WorkspacePanel } from "./workspace-panel";
import { type PanelId } from "@/lib/workspace-layout";

/**
 * Рабочая строка экрана «работа с документами»: три панели, которые можно
 * тянуть за границы, сворачивать в корешки, отстёгивать в плавающие окна и
 * возвращать обратно. Содержимое панелей приходит извне — здесь только
 * раскладка.
 */
export function WorkspaceFrame({ panes }: { panes: Record<PanelId, ReactNode> }) {
  // Рельс лежит СНАРУЖИ этой строки, поэтому её ширина его уже не включает —
  // вычитать railWidth ещё раз нельзя (иначе доли и слоты снапа врут на 232px).
  const L = usePanelLayout(0);
  const { lay } = L;

  // Просьбы к раскладке от чужих мест (создание чата, отправка документа).
  // Реагируем на НОМЕР просьбы, а не на её содержимое: две одинаковые подряд
  // должны сработать оба раза.
  const { layoutIntent } = useWorkspace();
  const seenSeq = useRef(0);
  useEffect(() => {
    if (!layoutIntent || layoutIntent.seq === seenSeq.current) return;
    seenSeq.current = layoutIntent.seq;
    L.setCollapsed(layoutIntent.collapsed);
  }, [layoutIntent, L]);

  const docked = lay.ord.filter((id) => L.isDocked(id));

  return (
    <div ref={L.frameRef} className="relative flex min-h-0 flex-1 overflow-hidden">
      {/* ОДИН проход по панелям и СТАБИЛЬНЫЙ key на панель. Раньше
          пристёгнутые и плавающие рисовались двумя разными списками, и при
          отстёгивании React размонтировал панель и монтировал новую: чат
          терял живой ход вместе с несохранённой перепиской. Свёрнутая панель
          тоже не выбрасывается — прячется, а рядом рисуется корешок. */}
      {lay.ord.map((id) => {
        const collapsed = lay.col[id];
        const box = lay.fl[id];
        const idx = docked.indexOf(id);
        const isLast = idx >= 0 && idx === docked.length - 1;
        const { flex, width } = collapsed || box ? { flex: "0 0 auto", width: "0px" } : L.widthOf(id);

        // Панель, прижатая к ПРАВОЙ стенке, обязана прижаться к ней и в потоке.
        // Иначе, оставшись единственной колонкой (соседи свёрнуты или отстёгнуты),
        // она вставала бы у левого края: подсветка обещала право, а панель
        // приезжала влево — ровно этот баг и ловился при свёрнутой рабочей области.
        const pinRight = lay.pin?.id === id && lay.pin.side === "right";
        const style: React.CSSProperties = box
          ? { left: box.x, top: box.y, width: box.w, height: box.h }
          : collapsed
            ? { display: "none" }
            : { flex, width, ...(pinRight ? { marginInlineStart: "auto" } : null) };

        return (
          <Fragment key={id}>
            {collapsed && <PanelSpine id={id} onExpand={() => L.toggleCollapse(id)} />}
            <WorkspacePanel
              id={id}
              floating={!!box}
              dragging={L.dragging}
              style={style}
              panelRef={(el) => {
                L.panelRefs.current[id] = el;
              }}
              onMoveStart={(e) => L.startMove(id, e)}
              onResizeStart={(dir, e) => L.startResize(id, dir, e)}
              onDock={() => L.dock(id)}
              onCollapse={() => L.toggleCollapse(id)}
              onMaximize={() => L.maximize(id)}
            >
              {panes[id]}
            </WorkspacePanel>
            {!collapsed && !box && !isLast && (
              <PanelSplitter onStart={(e) => L.startSplit(idx, e)} />
            )}
          </Fragment>
        );
      })}

      {/* Подсветка «сюда встанет»: колонка у верхней кромки, треть у боковой
          стенки, четверть в углу. Прямоугольник ОДИН и переезжает между зонами
          с переходом — так видно, как окно «примагничивается», а не мигает. */}
      <SnapHint L={L} />
    </div>
  );
}

/**
 * Подсветка зоны примагничивания.
 *
 * Держится в DOM всё перетаскивание (пока цель есть) и МЕНЯЕТ ГЕОМЕТРИЮ через
 * переход — поэтому при переносе окна от верхней кромки к стенке и в угол
 * прямоугольник плавно переезжает и меняет размер, а не пересоздаётся рывком.
 * Появление и исчезновение — по прозрачности.
 */
function SnapHint({ L }: { L: ReturnType<typeof usePanelLayout> }) {
  const box = L.snapBox();
  // Последнюю геометрию помним, чтобы на исчезновении подсветка гасла на месте,
  // а не схлопывалась в угол.
  const last = useRef(box);
  if (box) last.current = box;
  const r = box ?? last.current;
  if (!r) return null;

  return (
    <div
      aria-hidden
      className={
        "pointer-events-none absolute z-20 rounded-[var(--radius-token-md)] " +
        "border-2 border-[var(--color-accent)] bg-[var(--color-accent)]/12 " +
        "transition-[left,top,width,height,opacity] duration-150 ease-out " +
        (box ? "opacity-100" : "opacity-0")
      }
      style={{
        left: r.left,
        top: r.top,
        width: r.width,
        height: r.height,
        boxShadow: "0 0 0 1px var(--color-surface), 0 12px 32px -8px var(--color-accent)",
      }}
    >
    </div>
  );
}
