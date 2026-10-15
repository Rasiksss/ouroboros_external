"use client";

import { useRef, useState, type ReactNode } from "react";
import { Check, Copy, Table as TableIcon } from "lucide-react";
import { toast } from "sonner";
import { tableToTSV } from "@/lib/download";

/**
 * Таблица внутри ответа агента — ОТДЕЛЬНЫМ блоком с кнопкой копирования.
 *
 * Зачем блок: ответ идёт сплошным текстом, и таблица показателей в нём
 * теряется — глазу не за что зацепиться. Рамка и шапка отделяют её от прозы.
 *
 * Зачем кнопка: аналитик всё равно переносит числа к себе. Копируем ТSV —
 * Excel и Таблицы раскладывают его по ячейкам без диалога импорта.
 * Сериализатор один на всё приложение (lib/download.ts), второй разошёлся бы
 * по экранированию.
 *
 * Кнопка — ИКОНКА без подписи (правка Nate 02.08). Слово «Excel» обещало
 * лишнее: буфер обмена одинаково ложится и в Excel, и в Таблицы, и в Numbers,
 * а «копировать» — единственное, что кнопка действительно делает. Понятность
 * держится на общеизвестной иконке двух листов и подсказке при наведении;
 * подтверждение — галочкой на месте иконки, чтобы не гадать, сработало ли.
 */
export function CopyableTable({
  children,
  caption,
}: {
  children: ReactNode;
  /** Чем подписать таблицу — компания, период, единицы. Пусто — родовое
   *  «Таблица».
   *
   *  ЗАЧЕМ (правка Nate 17.08). Заголовок прогона стоит в тексте ВЫШЕ, и при
   *  длинном ответе таблица уезжает от него на экран-другой: видно сетку чисел,
   *  а чьи они и за какой период — нет. Подпись едет вместе с таблицей и
   *  остаётся на месте, пока таблицу прокручивают вбок. */
  caption?: string;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);

  async function copy() {
    // Читаем ОТРИСОВАННУЮ таблицу, а не исходный markdown: пользователь
    // копирует ровно то, что видит.
    const el = boxRef.current?.querySelector("table");
    if (!el) return;
    try {
      await navigator.clipboard.writeText(tableToTSV(el));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (e) {
      toast.error("Не удалось скопировать: " + (e as Error).message);
    }
  }

  return (
    // Без not-prose: на старом экране таблица оформлена классами prose-*
    // родителя, и обрубив их, мы бы разом сняли рамки и отступы ячеек.
    <div className="my-3 overflow-hidden rounded-md border">
      <div className="flex items-center justify-between gap-3 border-b bg-muted/40 px-3 py-1.5">
        <span
          className="inline-flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
          title={caption || undefined}
        >
          <TableIcon className="size-3.5 shrink-0" />
          <span className="truncate">{caption || "Таблица"}</span>
        </span>
        <button
          type="button"
          onClick={copy}
          aria-label="Копировать таблицу"
          title={copied ? "Скопировано" : "Копировать таблицу"}
          className="print:hidden shrink-0 rounded-md border p-1 text-muted-foreground transition-colors hover:bg-background hover:text-foreground"
        >
          {copied
            ? <Check className="size-3.5 text-emerald-600 dark:text-emerald-500" />
            : <Copy className="size-3.5" />}
        </button>
      </div>
      <div ref={boxRef} className="overflow-x-auto px-3 py-2">
        {children}
      </div>
    </div>
  );
}
