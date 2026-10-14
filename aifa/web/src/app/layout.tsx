import type { Metadata } from "next";
import {
  Geist,
  Geist_Mono,
  Lora,
  Cormorant_Garamond,
  Roboto,
  Roboto_Condensed,
  IBM_Plex_Mono,
  Inter,
} from "next/font/google";
import "./globals.css";
import { Toaster } from "@/components/ui/sonner";
import { AuthProvider } from "@/components/auth-context";
import { ThemeBridge } from "@/components/theme-bridge";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin", "cyrillic"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin", "cyrillic"],
});

// Шрифты трёх тем редизайна (см. блоки [data-theme] в globals.css).
// Классика: Cormorant Garamond (заголовки) + Lora (текст).
const lora = Lora({
  variable: "--font-lora",
  subsets: ["latin", "cyrillic"],
  display: "swap",
});

const cormorant = Cormorant_Garamond({
  variable: "--font-cormorant",
  subsets: ["latin", "cyrillic"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
});

// Индастри: в эталоне Barlow / Barlow Condensed, но у них НЕТ кириллицы
// (Google отдаёт только latin/latin-ext/vietnamese) — русский текст падал бы
// на системный фоллбэк. Ближайшие технические гротески с кириллицей.
const roboto = Roboto({
  variable: "--font-roboto",
  subsets: ["latin", "cyrillic"],
  display: "swap",
});

const robotoCondensed = Roboto_Condensed({
  variable: "--font-roboto-condensed",
  subsets: ["latin", "cyrillic"],
  display: "swap",
});

// Терминал: IBM Plex Mono (заголовки) + Inter (текст).
const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin", "cyrillic"],
  weight: ["400", "500", "600"],
  display: "swap",
});

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin", "cyrillic"],
  display: "swap",
});

export const metadata: Metadata = {
  title: "AI Financial Analyst",
  description: "Financial extraction agent",
  icons: {
    // Multi-res .ico is the legacy fallback every browser requests at /favicon.ico.
    // PNG override gives modern browsers a crisper render in tab strips.
    icon: [
      { url: "/favicon.png", type: "image/png" },
    ],
    shortcut: "/favicon.png",
    apple: "/favicon.png",
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="ru"
      suppressHydrationWarning
      className={`${geistSans.variable} ${geistMono.variable} ${lora.variable} ${cormorant.variable} ${roboto.variable} ${robotoCondensed.variable} ${plexMono.variable} ${inter.variable} h-full antialiased`}
    >
      <body className="h-full bg-background text-foreground overflow-hidden">
        <ThemeBridge>
          <AuthProvider>{children}</AuthProvider>
        </ThemeBridge>
        <Toaster
          position="bottom-right"
          richColors
          duration={2500}
          closeButton
          offset={{ bottom: 96 }}  /* keep above the composer */
          toastOptions={{ style: { maxWidth: 360 } }}
        />
      </body>
    </html>
  );
}
