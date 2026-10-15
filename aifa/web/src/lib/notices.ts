/**
 * Системные уведомления: всплывашка + ИСТОРИЯ.
 *
 * Всплывающее уведомление живёт секунды и исчезает — если пользователь отошёл,
 * он никогда не узнает, что случилось (ровно так потерялся разбор Мегафона:
 * красная плашка мелькнула и пропала). Поэтому каждое уведомление ещё и
 * ложится в историю на экране, а серьёзные — уходят на сервер, чтобы сбой
 * КЛИЕНТА был виден в логах: раньше их не было нигде.
 */
import { toast } from "sonner";

export type NoticeKind = "info" | "warn" | "error";

export type Notice = {
  id: string;
  kind: NoticeKind;
  text: string;
  at: number;
};

const KEY = "aifa:notices:v1";
const LIMIT = 200;

let items: Notice[] = load();
const listeners = new Set<(n: Notice[]) => void>();

function load(): Notice[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = localStorage.getItem(KEY);
    const parsed = raw ? (JSON.parse(raw) as Notice[]) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function persist() {
  try {
    localStorage.setItem(KEY, JSON.stringify(items.slice(0, LIMIT)));
  } catch {
    /* переполнено или недоступно — история не критична */
  }
}

function emit() {
  for (const fn of listeners) fn(items);
}

/** Показать уведомление И записать его в историю. */
export function notify(kind: NoticeKind, text: string): void {
  const n: Notice = { id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, kind, text, at: Date.now() };
  items = [n, ...items].slice(0, LIMIT);
  persist();
  emit();
  if (kind === "error") toast.error(text);
  else if (kind === "warn") toast.warning(text);
  else toast(text);
}

export function noticesSnapshot(): Notice[] {
  return items;
}

export function subscribeNotices(fn: (n: Notice[]) => void): () => void {
  listeners.add(fn);
  fn(items);
  return () => listeners.delete(fn);
}

export function clearNotices(): void {
  items = [];
  persist();
  emit();
}

/**
 * Отправить событие клиента на сервер. Нужен для диагностики: обрыв SSE,
 * упавший рендер и прочее происходят В БРАУЗЕРЕ и в серверных логах не видны
 * вообще — искать причину постфактум было нечем.
 */
export function reportClientEvent(payload: Record<string, unknown>): void {
  try {
    void fetch("/api/client-events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, at: Date.now(), url: location.pathname }),
      keepalive: true,
    }).catch(() => undefined);
  } catch {
    /* диагностика не должна ломать приложение */
  }
}
