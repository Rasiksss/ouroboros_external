/**
 * Ссылки «PDF стр. N» в тексте карточки анализа (#157).
 *
 * ЗАЧЕМ ОТДЕЛЬНЫМ МОДУЛЕМ. Кликабельной должна становиться РОВНО та запись,
 * которую бэкенд оставил после сверки со страницами, найденными движком
 * (`reasoner.runner.scrub_page_refs`). Номер, которого движок не показывал,
 * там теряет формат «PDF стр. N» и превращается в «стр. N (страница не
 * подтверждена движком)» — и вот эту запись фронт обязан НЕ распознать.
 * Поэтому разбор формата живёт в одной чистой функции: её можно проверить
 * тестом на данных, а не глазами по отрендеренной карточке.
 *
 * Формат ссылки — «PDF стр. N», а не «стр. N»: печатный колонтитул отчёта
 * почти никогда не совпадает с физической страницей файла, а прыгаем мы по
 * физической.
 */

/** Тот же формат, что пишет бэкенд. Глобальный флаг задаём на месте: у
 *  общего /g-объекта живёт lastIndex, и второй вызов начал бы с середины. */
const PAGE_REF = "PDF\\s*стр\\.\\s*(\\d+)";

/** Схема ссылки-заглушки. Настоящий переход делает обработчик карточки: href
 *  нужен только чтобы разметка осталась ссылкой (и в markdown, и в прозе),
 *  а клик по ней можно было перехватить одним делегатом. */
export const PAGE_HREF_PREFIX = "#pdf-page-";

/** Номер страницы из href нашей ссылки; 0 — ссылка чужая, не трогаем. */
export function pageFromHref(href: string | null | undefined): number {
  const m = new RegExp(`^${PAGE_HREF_PREFIX}(\\d+)$`).exec(href || "");
  return m ? Number(m[1]) : 0;
}

/** markdown-текст раздела: «PDF стр. 42» → markdown-ссылка на ту же подпись.
 *  Так ссылку рисует штатный рендерер markdown, а не вторая реализация. */
export function linkifyPageRefs(text: string): string {
  return (text || "").replace(
    new RegExp(PAGE_REF, "g"),
    (whole, n) => `[${whole}](${PAGE_HREF_PREFIX}${n})`,
  );
}

/** Простая строка (вердикт, вывод, пункт списка) → куски для рендера.
 *  `page > 0` — кусок должен стать ссылкой, иначе это обычный текст. */
export function splitPageRefs(text: string): { text: string; page: number }[] {
  const out: { text: string; page: number }[] = [];
  const re = new RegExp(PAGE_REF, "g");
  const src = text || "";
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(src)) !== null) {
    if (m.index > last) out.push({ text: src.slice(last, m.index), page: 0 });
    out.push({ text: m[0], page: Number(m[1]) });
    last = m.index + m[0].length;
  }
  if (last < src.length) out.push({ text: src.slice(last), page: 0 });
  return out;
}
