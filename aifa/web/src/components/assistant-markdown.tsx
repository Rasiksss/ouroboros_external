/**
 * Отрисовка markdown-ответа агента. Вынесено из старого экрана «/» при
 * его сносе (Р20): карточкой пользуется /v2 через analysis-card.
 */
import { type ReactNode } from "react";
import { Calculator, ChevronDown } from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { CopyableTable } from "@/components/copyable-table";


// Render fenced code blocks tagged with `# code_execution:` as a collapsed
// "Показать вычисления" pill. Everything else stays as a regular code block.
function ComputationBlock({ title, code }: { title: string; code: string }) {
  return (
    <details className="my-2 rounded-md border bg-muted/30 not-prose">
      <summary className="flex items-center gap-2 px-3 py-1.5 cursor-pointer select-none text-xs text-muted-foreground hover:text-foreground">
        <Calculator className="size-3.5 shrink-0" />
        <span className="flex-1 truncate">{title || "Расчёт"}</span>
        <ChevronDown className="size-3.5 shrink-0 transition-transform group-open:rotate-180" />
      </summary>
      <pre className="mt-1 px-3 py-2 text-xs overflow-x-auto bg-transparent border-t font-mono leading-relaxed">
        <code>{code}</code>
      </pre>
    </details>
  );
}

const markdownComponents: Components = {
  // Open external links in a new tab + safe rel. Matters for IR Daily
  // where every fact ends with a source link to a regulator / company
  // IR page — clicking shouldn't navigate away from the chat.
  a({ href, children }) {
    const url = typeof href === "string" ? href : "";
    const isExternal = /^https?:\/\//.test(url);
    return (
      <a
        href={url}
        target={isExternal ? "_blank" : undefined}
        rel={isExternal ? "noopener noreferrer" : undefined}
        className="text-primary underline-offset-2 hover:underline"
      >
        {children}
      </a>
    );
  },
  pre({ children }) {
    // ReactMarkdown wraps fenced code in <pre><code>…</code></pre>. Inspect the
    // first line of the inner code text — when it's "# code_execution: <title>",
    // collapse the whole block into a clickable details element.
    const child = Array.isArray(children) ? children[0] : (children as ReactNode);
    let codeText: string | null = null;
    if (child && typeof child === "object" && "props" in child) {
      const inner = (child as { props?: { children?: ReactNode } }).props?.children;
      if (typeof inner === "string") codeText = inner;
      else if (Array.isArray(inner)) codeText = inner.map((c) => (typeof c === "string" ? c : "")).join("");
    }
    if (codeText) {
      const lines = codeText.replace(/\n+$/, "").split("\n");
      const first = lines[0] ?? "";
      const m = first.match(/^#\s*code_execution\s*:\s*(.*)$/i);
      if (m) {
        const title = m[1].trim();
        const body = lines.slice(1).join("\n").replace(/^\n+/, "");
        return <ComputationBlock title={title} code={body} />;
      }
    }
    return <pre className="my-2 px-3 py-2 rounded-md border bg-muted/40 overflow-x-auto text-xs">{children}</pre>;
  },
  // Wrap GFM tables (the extracted-metrics output) in a horizontally
  // scrollable box so many period columns can't overflow the chat bubble.
  // The table's own width is left to `prose-table:w-auto` so it hugs its
  // content instead of stretching full-width and pushing the numbers far
  // to the right. Обёртка ещё и отделяет таблицу от прозы и даёт копирование
  // в Excel — см. CopyableTable.
  table({ children }) {
    return (
      <CopyableTable>
        <table>{children}</table>
      </CopyableTable>
    );
  },
};


/**
 * Свернуть вырожденный повтор одного знака перед отрисовкой.
 *
 * Живой случай 17.08.2026: модель сорвалась на фразе «в собранном дайджесте за
 * по» и выдала 394 подряд «П» — строка распёрла верстку на всю ширину. В
 * бэкенде поток теперь обрывается на таком месте, но СОХРАНЁННЫЕ сообщения
 * останутся такими навсегда: этот ответ лежит в истории и будет рисоваться при
 * каждом открытии чата. Поэтому защита нужна и здесь.
 *
 * Два порога, как и на бэкенде: буквы и цифры подряд в осмысленном тексте не
 * идут никогда, а дефисы, решётки и пробелы идут законно — это разделители
 * таблиц, горизонтальные линии и выравнивание. Резать разметку нельзя.
 */
export function collapseRuns(text: string): string {
  if (!text) return text;
  return text.replace(/(.)\1{19,}/gu, (run, ch: string) => {
    const limit = /[\p{L}\p{N}]/u.test(ch) ? 30 : 120;
    if (run.length < limit) return run;
    return `${ch.repeat(8)}… _[повтор ${run.length} знаков свёрнут]_`;
  });
}

export function AssistantMarkdown({ text }: { text: string }) {
  return (
    <div className="prose prose-sm max-w-none text-sm leading-relaxed
                    prose-headings:mt-3 prose-headings:mb-1.5
                    prose-p:my-1.5 prose-ul:my-1.5 prose-ol:my-1.5
                    prose-table:my-2 prose-table:w-auto prose-th:px-2 prose-th:py-1
                    prose-td:px-2 prose-td:py-1 prose-th:border prose-td:border
                    [&_td:not(:first-child)]:text-right [&_th:not(:first-child)]:text-right
                    [&_td:not(:first-child)]:whitespace-nowrap [&_th:not(:first-child)]:whitespace-nowrap
                    [&_td:not(:first-child)]:tabular-nums
                    prose-code:px-1 prose-code:py-0.5 prose-code:rounded
                    prose-code:bg-muted prose-code:text-foreground
                    prose-code:before:content-none prose-code:after:content-none">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
        {collapseRuns(text)}
      </ReactMarkdown>
    </div>
  );
}
