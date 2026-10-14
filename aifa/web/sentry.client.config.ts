import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN_FRONTEND;

if (dsn) {
  Sentry.init({
    dsn,
    environment: process.env.NEXT_PUBLIC_SENTRY_ENVIRONMENT ?? "production",
    tracesSampleRate: 0.1,
    // No session replay yet — costs quota and PII risk.
    replaysSessionSampleRate: 0,
    replaysOnErrorSampleRate: 0,
  });
}
