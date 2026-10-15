"use client";

import { type ReactNode } from "react";

/**
 * Two-column auth layout: PNG splash on the left, dark navy gradient + glass
 * login card on the right. Mobile (≤1100px) stacks the splash above the card.
 *
 * Design source: Downloads/aifa_login_final.html — preserved 1:1 except the
 * left-side base64 PNG was extracted to /public/login-hero.webp (1.4 MB → 41 KB).
 */
export function AuthShell({ children }: { children: ReactNode }) {
  return (
    <main className="aifa-page">
      <section className="aifa-left" aria-hidden="true" />
      <section className="aifa-right">
        <div className="aifa-card">{children}</div>
      </section>

      <style>{`
        :root {
          --aifa-navy-base: #071426;
          --aifa-navy-top: #0a1d33;
          --aifa-navy-bottom: #050d1b;
          --aifa-card-top: rgba(248, 252, 255, 0.985);
          --aifa-card-bottom: rgba(235, 242, 249, 0.975);
          --aifa-text-main: #1b2436;
          --aifa-text-muted: #778396;
          --aifa-input-border: #c8d2de;
          --aifa-input-border-focus: #95a6bc;
          --aifa-input-bg: rgba(247, 250, 254, 0.96);
        }

        .aifa-page {
          width: 100vw;
          height: 100vh;
          min-height: 760px;
          display: grid;
          grid-template-columns: 1fr 1fr;
          background: var(--aifa-navy-base);
          color: var(--aifa-text-main);
        }

        .aifa-left {
          min-height: 100%;
          background-image: url('/login-hero.webp');
          background-repeat: no-repeat;
          background-size: cover;
          background-position: center center;
          box-shadow: inset -22px 0 70px rgba(3, 8, 20, 0.20);
        }

        .aifa-right {
          min-height: 100%;
          position: relative;
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 48px;
          overflow: hidden;
          background:
            radial-gradient(circle at 42% 48%, rgba(53, 97, 154, 0.34) 0%, rgba(53, 97, 154, 0.15) 26%, rgba(53, 97, 154, 0.00) 55%),
            linear-gradient(180deg, var(--aifa-navy-top) 0%, var(--aifa-navy-base) 45%, var(--aifa-navy-bottom) 100%);
        }

        .aifa-right::before {
          content: "";
          position: absolute;
          inset: 0;
          pointer-events: none;
          background:
            linear-gradient(90deg, rgba(8, 20, 38, 0.40), rgba(7, 20, 38, 0.00) 48%),
            radial-gradient(circle at 50% 52%, rgba(112, 176, 246, 0.075), transparent 46%);
        }

        .aifa-card {
          position: relative;
          z-index: 1;
          width: min(650px, 72%);
          min-width: 520px;
          padding: 68px 60px 54px;
          border-radius: 16px;
          background: linear-gradient(180deg, var(--aifa-card-top), var(--aifa-card-bottom));
          box-shadow:
            0 25px 82px rgba(0, 0, 0, 0.34),
            0 0 54px rgba(89, 144, 216, 0.12),
            inset 0 1px 0 rgba(255, 255, 255, 0.90);
        }

        .aifa-card h1 {
          margin: 0 0 13px;
          color: var(--aifa-text-main);
          font-size: 33px;
          line-height: 1.05;
          font-weight: 500;
          letter-spacing: -0.03em;
        }

        .aifa-card .aifa-subtitle {
          margin: 0 0 45px;
          color: var(--aifa-text-muted);
          font-size: 17px;
          line-height: 1.35;
        }

        .aifa-card .aifa-field {
          margin-bottom: 27px;
        }

        .aifa-card .aifa-field label {
          display: block;
          margin: 0 0 8px;
          color: #687385;
          font-size: 15px;
          font-weight: 500;
        }

        .aifa-card .aifa-field input {
          width: 100%;
          height: 55px;
          padding: 0 17px;
          border: 1.5px solid var(--aifa-input-border);
          border-radius: 9px;
          outline: none;
          background: var(--aifa-input-bg);
          color: #1f2937;
          font-size: 17px;
          box-shadow: inset 0 1px 2px rgba(15, 23, 42, 0.04);
          transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
        }

        .aifa-card .aifa-field input::placeholder {
          color: #8c96a5;
        }

        .aifa-card .aifa-field input:focus {
          border-color: var(--aifa-input-border-focus);
          box-shadow: 0 0 0 3px rgba(120, 150, 200, 0.12);
          background: rgba(250, 253, 255, 0.98);
        }

        .aifa-card .aifa-field input.aifa-mono {
          font-family: var(--font-geist-mono), ui-monospace, "SF Mono", Menlo, monospace;
          letter-spacing: 0.02em;
        }

        .aifa-button {
          width: 100%;
          height: 51px;
          margin-top: 8px;
          border: 0;
          border-radius: 9px;
          color: #1d2738;
          font-size: 18px;
          font-weight: 600;
          cursor: pointer;
          background:
            radial-gradient(ellipse at center, rgba(255, 255, 255, 0.98) 0%, rgba(255, 255, 255, 0.50) 22%, rgba(166, 173, 185, 0.72) 48%, rgba(112, 119, 132, 0.88) 100%),
            linear-gradient(90deg, #7d8593, #d9dde4 50%, #7d8593);
          box-shadow:
            0 25px 28px rgba(47, 57, 74, 0.28),
            inset 0 1px 0 rgba(255, 255, 255, 0.70),
            inset 0 -1px 0 rgba(0, 0, 0, 0.16);
          transition: filter 0.15s ease, transform 0.08s ease, opacity 0.15s ease;
        }

        .aifa-button:hover:not(:disabled) {
          filter: brightness(1.035);
        }

        .aifa-button:active:not(:disabled) {
          transform: translateY(1px);
        }

        .aifa-button:disabled {
          opacity: 0.55;
          cursor: not-allowed;
        }

        .aifa-meta {
          margin-top: 36px;
          text-align: center;
          font-size: 15px;
          color: #667181;
        }

        .aifa-meta a {
          color: #263247;
          font-weight: 600;
          text-decoration: underline;
          text-underline-offset: 2px;
        }

        @media (max-width: 1100px) {
          body {
            overflow: auto !important;
          }
          .aifa-page {
            height: auto;
            min-height: 100vh;
            grid-template-columns: 1fr;
          }
          .aifa-left {
            min-height: 430px;
          }
          .aifa-right {
            min-height: 560px;
            padding: 36px 20px 80px;
          }
          .aifa-card {
            width: min(620px, 92%);
            min-width: 0;
            padding: 48px 42px 38px;
          }
        }

        @media (max-width: 640px) {
          .aifa-card {
            padding: 34px 22px 28px;
          }
          .aifa-card h1 {
            font-size: 28px;
          }
          .aifa-card .aifa-subtitle {
            margin-bottom: 30px;
          }
        }
      `}</style>
    </main>
  );
}
