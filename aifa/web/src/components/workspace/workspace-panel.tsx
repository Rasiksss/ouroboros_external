"use client";

import type { ReactNode } from "react";
import { ChevronsLeftRight, ChevronsRightLeft, CornerDownLeft } from "lucide-react";
import { PANEL_TITLE, SPINE_W, type PanelId } from "@/lib/workspace-layout";
import { cn } from "@/lib/utils";

/** Подпись панели: 9.5px, uppercase, широкий трекинг — как в эталоне. */
const CAPTION =
  "text-[9.5px] uppercase tracking-[0.15em] text-[var(--color-neutral-500)] whitespace-nowrap overflow-hidden text-ellipsis";

/**
 * Полоса захвата 30px: ручка-точки, название, «вернуть в раскладку» и
 * сворачивание. Тянуть — отстегнуть панель, двойной клик — на всю рабочую
 * область.
 */
function GripBar({
  id,
  floating,
  onMoveStart,
  onDock,
  onCollapse,
  onMaximize,
}: {
  id: PanelId;
  floating: boolean;
  onMoveStart: (e: React.PointerEvent) => void;
  onDock: () => void;
  onCollapse: () => void;
  onMaximize: () => void;
}) {
  return (
    <div
      onPointerDown={onMoveStart}
      onDoubleClick={onMaximize}
      title="Потяните, чтобы отстегнуть · двойной клик — на всю ширину"
      className="flex h-[30px] shrink-0 cursor-grab touch-none select-none items-center gap-2 border-b border-[var(--color-divider)] bg-[var(--color-surface)] px-3 active:cursor-grabbing"
    >
      <span className="text-[9px] tracking-[0.2em] text-[var(--color-neutral-400)]">⠿</span>
      <span className={cn(CAPTION, "flex-1")}>{PANEL_TITLE[id]}</span>
      {floating && (
        <button
          type="button"
          title="Вернуть в раскладку"
          onPointerDown={(e) => e.stopPropagation()}
          onDoubleClick={(e) => e.stopPropagation()}
          onClick={onDock}
          className="grid size-[22px] shrink-0 place-items-center rounded-[var(--radius-token-sm)] text-[var(--color-neutral-500)] transition-colors hover:bg-[var(--color-accent-100)] hover:text-[var(--color-accent-700)]"
        >
          <CornerDownLeft className="size-4" strokeWidth={1.6} />
        </button>
      )}
      <button
        type="button"
        title="Свернуть в корешок"
        onPointerDown={(e) => e.stopPropagation()}
        onDoubleClick={(e) => e.stopPropagation()}
        onClick={onCollapse}
        className="grid size-4 shrink-0 place-items-center text-[var(--color-neutral-500)] transition-colors hover:text-[var(--color-accent-700)]"
      >
        <ChevronsRightLeft className="size-4" strokeWidth={1.6} />
      </button>
    </div>
  );
}

/** Свёрнутая панель — корешок 34px с вертикальной подписью. */
export function PanelSpine({ id, onExpand }: { id: PanelId; onExpand: () => void }) {
  return (
    <button
      type="button"
      onClick={onExpand}
      title="Развернуть панель"
      style={{ width: SPINE_W }}
      className="flex shrink-0 flex-col items-center gap-3 border-r border-[var(--color-divider)] bg-[var(--color-surface)] pt-3 transition-colors hover:bg-[var(--color-accent-100)]"
    >
      <ChevronsLeftRight
        className="size-[17px] text-[var(--color-accent-700)]"
        strokeWidth={1.6}
      />
      <span
        className="text-[10px] uppercase tracking-[0.16em] text-[var(--color-neutral-600)]"
        style={{ writingMode: "vertical-rl", transform: "rotate(180deg)" }}
      >
        {PANEL_TITLE[id]}
      </span>
    </button>
  );
}

/** Восемь зон изменения размера плавающего окна; правый нижний — с уголком. */
const RESIZE_ZONES: { dir: string; className: string; cursor: string }[] = [
  { dir: "n", className: "left-2 right-2 top-0 h-1.5", cursor: "ns-resize" },
  { dir: "s", className: "left-2 right-2 bottom-0 h-1.5", cursor: "ns-resize" },
  { dir: "w", className: "top-2 bottom-2 left-0 w-1.5", cursor: "ew-resize" },
  { dir: "e", className: "top-2 bottom-2 right-0 w-1.5", cursor: "ew-resize" },
  { dir: "nw", className: "left-0 top-0 size-3", cursor: "nwse-resize" },
  { dir: "ne", className: "right-0 top-0 size-3", cursor: "nesw-resize" },
  { dir: "sw", className: "left-0 bottom-0 size-3", cursor: "nesw-resize" },
  { dir: "se", className: "right-0 bottom-0 size-4", cursor: "nwse-resize" },
];

export function WorkspacePanel({
  id,
  floating,
  style,
  panelRef,
  dragging,
  onMoveStart,
  onResizeStart,
  onDock,
  onCollapse,
  onMaximize,
  children,
}: {
  id: PanelId;
  floating: boolean;
  style: React.CSSProperties;
  panelRef: (el: HTMLDivElement | null) => void;
  dragging: boolean;
  onMoveStart: (e: React.PointerEvent) => void;
  onResizeStart: (dir: string, e: React.PointerEvent) => void;
  onDock: () => void;
  onCollapse: () => void;
  onMaximize: () => void;
  children: ReactNode;
}) {
  return (
    <div
      ref={panelRef}
      style={{
        ...style,
        // Единая кривая на ширину/положение; на время жеста переходы сняты,
        // иначе панель тянется за курсором с задержкой.
        transition: dragging ? "none" : "width .2s cubic-bezier(.2,.75,.25,1), left .2s cubic-bezier(.2,.75,.25,1), top .2s cubic-bezier(.2,.75,.25,1)",
      }}
      className={cn(
        "flex min-w-0 flex-col overflow-hidden bg-[var(--color-bg)]",
        floating
          ? "absolute z-30 rounded-[var(--radius-token-md)] border border-[var(--color-divider)] shadow-lg"
          : "relative border-r border-[var(--color-divider)]",
      )}
    >
      <GripBar
        id={id}
        floating={floating}
        onMoveStart={onMoveStart}
        onDock={onDock}
        onCollapse={onCollapse}
        onMaximize={onMaximize}
      />
      <div className="min-h-0 flex-1 overflow-hidden">{children}</div>

      {floating &&
        RESIZE_ZONES.map((z) => (
          <div
            key={z.dir}
            onPointerDown={(e) => onResizeStart(z.dir, e)}
            style={{ cursor: z.cursor }}
            className={cn("absolute touch-none", z.className)}
          >
            {z.dir === "se" && (
              // Золотой уголок — метка «тянуть отсюда» (эталон, 16px).
              <span className="pointer-events-none absolute right-0 bottom-0 size-4 border-r-2 border-b-2 border-[var(--color-accent)]" />
            )}
          </div>
        ))}
    </div>
  );
}

/** Граница между колонками: зона 13px, линия на всю высоту и золотой «хват». */
export function PanelSplitter({ onStart }: { onStart: (e: React.PointerEvent) => void }) {
  return (
    <div
      onPointerDown={onStart}
      className="relative z-10 w-[13px] shrink-0 cursor-col-resize touch-none transition-colors hover:bg-[var(--color-accent-100)]"
    >
      <span className="absolute inset-y-0 left-[6px] w-px bg-[var(--color-divider)]" />
      <span className="absolute top-1/2 left-[4px] flex -translate-y-1/2 flex-col gap-[3px] opacity-75">
        <span className="h-px w-[5px] bg-[var(--color-accent)]" />
        <span className="h-px w-[5px] bg-[var(--color-accent)]" />
        <span className="h-px w-[5px] bg-[var(--color-accent)]" />
      </span>
    </div>
  );
}
