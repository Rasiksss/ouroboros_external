"use client";

import { useEffect, type ReactNode } from "react";
import { ThemeProvider, useTheme } from "next-themes";
import { loadPrefs, THEME_OPTIONS, type AgentPrefs } from "@/lib/prefs";

/**
 * Тема продукта = одна из трёх именованных (Классика / Индастри / Терминал),
 * см. дизайн-хендофф. `next-themes` вешает на <html> атрибут data-theme (по
 * нему в globals.css выбираются токены) и, для «Терминала», класс `dark` —
 * у части shadcn-компонентов есть dark:-варианты.
 *
 * Источник правды — prefs.appearance.theme в localStorage; next-themes
 * используется как рантайм-зеркало (чтобы не заводить второй ключ хранения).
 */
const THEME_IDS = THEME_OPTIONS.map((t) => t.id);

export function ThemeBridge({ children }: { children: ReactNode }) {
  return (
    <ThemeProvider
      attribute="data-theme"
      themes={THEME_IDS}
      defaultTheme="classic"
      enableSystem={false}
      // Класс `dark` навешивается на «Терминал» — иначе dark:-варианты
      // компонентов остались бы в светлом виде поверх тёмных токенов.
      value={{ classic: "classic", industry: "industry", terminal: "terminal dark" }}
      disableTransitionOnChange={false}
    >
      <PrefsToThemeSync />
      {children}
    </ThemeProvider>
  );
}

function PrefsToThemeSync() {
  const { setTheme } = useTheme();

  // On mount: read saved prefs, push into next-themes once.
  useEffect(() => {
    try {
      const p = loadPrefs();
      if (p.appearance?.theme) setTheme(p.appearance.theme);
    } catch {
      /* localStorage may not be available — no-op */
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Listen for prefs writes from elsewhere (Settings page calls savePrefs()
  // via update()). Same-tab updates fire a "storage" event only across tabs,
  // so we also publish a custom event from savePrefs and listen here.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<AgentPrefs>).detail;
      if (detail?.appearance?.theme) {
        setTheme(detail.appearance.theme);
      }
    };
    window.addEventListener("finengine:prefs", handler as EventListener);
    return () =>
      window.removeEventListener("finengine:prefs", handler as EventListener);
  }, [setTheme]);

  // Размер текста и плотность — тоже атрибуты на <html>; ими правятся
  // корневой кегль и шкала отступов (globals.css).
  useEffect(() => {
    const apply = (p: AgentPrefs) => {
      const root = document.documentElement;
      root.dataset.textSize = p.appearance?.textSize ?? "normal";
      root.dataset.density = p.appearance?.density ?? "normal";
    };
    try {
      apply(loadPrefs());
    } catch {
      /* no-op */
    }
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<AgentPrefs>).detail;
      if (detail) apply(detail);
    };
    window.addEventListener("finengine:prefs", handler as EventListener);
    return () =>
      window.removeEventListener("finengine:prefs", handler as EventListener);
  }, []);

  return null;
}
