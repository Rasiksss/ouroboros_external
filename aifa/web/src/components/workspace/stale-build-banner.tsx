"use client";

import { useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";

/**
 * Полоса «версия устарела».
 *
 * Вкладка, оставшаяся открытой через деплой, продолжает работать по СТАРОМУ
 * протоколу: пишет в общее хранилище своим форматом, шлёт на сервер снимки без
 * новых полей, не умеет догонять оборванные ходы. Именно так у нас терялись
 * документы и чаты — и заметить это было нечем, потому что внешне вкладка
 * выглядит рабочей.
 *
 * Отпечаток своей сборки вкладка знает (печётся в бандл), отпечаток живого
 * контейнера спрашивает у /build-id. Разошлись — говорим прямо и даём кнопку.
 * Автоматически не перезагружаем: у пользователя может быть недописанный текст.
 */
const MY_BUILD = process.env.NEXT_PUBLIC_BUILD_ID ?? "dev";
/** Не чаще раза в минуту: проверка дешёвая, но и повода частить нет. */
const CHECK_EVERY_MS = 60_000;

export function StaleBuildBanner() {
  const [serverBuild, setServerBuild] = useState<string | null>(null);

  useEffect(() => {
    // В локальной разработке отпечатка нет у обеих сторон — сравнивать нечего.
    if (MY_BUILD === "dev") return;
    let alive = true;
    let last = 0;

    const check = async () => {
      if (Date.now() - last < CHECK_EVERY_MS) return;
      last = Date.now();
      try {
        const r = await fetch("/build-id", { cache: "no-store" });
        if (!r.ok || !alive) return;
        const data = (await r.json()) as { build?: string };
        if (alive && data.build) setServerBuild(data.build);
      } catch {
        // Сеть моргнула — молчим: пугать «устаревшей версией» из-за обрыва нельзя.
      }
    };

    void check();
    const timer = setInterval(check, CHECK_EVERY_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void check();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      alive = false;
      clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, []);

  if (!serverBuild || serverBuild === MY_BUILD) return null;

  return (
    <div className="flex shrink-0 items-center gap-3 border-b border-[var(--color-accent)] bg-[var(--color-accent-100)] px-5 py-1.5">
      <span className="text-[12px] text-[var(--color-accent-700)]">
        Эта вкладка работает на устаревшей версии — она может терять документы и
        обрывать ходы. Обновите страницу.
      </span>
      <button
        type="button"
        onClick={() => location.reload()}
        className="ml-auto inline-flex shrink-0 items-center gap-1.5 rounded-[var(--radius-token-sm)] border border-[var(--color-accent)] px-2.5 py-1 text-[11.5px] text-[var(--color-accent-700)] transition-colors hover:bg-[var(--color-surface)]"
      >
        <RefreshCw className="size-3.5" />
        Обновить
      </button>
      <span className="shrink-0 text-[10px] tabular-nums text-[var(--color-neutral-500)]">
        {MY_BUILD} → {serverBuild}
      </span>
    </div>
  );
}
