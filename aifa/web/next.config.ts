import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";

const API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  // Старый корневой экран («/», app/page.tsx) выведен из употребления —
  // решение владельца 01.09.2026: дев-фронт живёт на /v2, корень не нужен.
  // Снос самого page.tsx — отдельная работа; пока корень уводит на /v2.
  // permanent: false (307) — браузеры не закешируют навсегда, откат дёшев.
  async redirects() {
    return [{ source: "/", destination: "/v2", permanent: false }];
  },
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${API_BASE}/:path*`,
      },
    ];
  },
};

export default withSentryConfig(nextConfig, {
  silent: true,
  // No org/project here — we are not uploading source maps yet, so
  // stack traces in Sentry will be minified. Adding source maps requires
  // SENTRY_AUTH_TOKEN at build time (deferred).
});
