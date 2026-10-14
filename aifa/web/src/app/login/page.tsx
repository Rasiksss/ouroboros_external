"use client";

import { Suspense, useState, type FormEvent } from "react";
import Link from "next/link";
import { useSearchParams, useRouter } from "next/navigation";
import { toast } from "sonner";
import { useAuth } from "@/components/auth-context";
import { AuthShell } from "@/components/auth-shell";

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <AuthShell>
        <LoginForm />
      </AuthShell>
    </Suspense>
  );
}

function LoginForm() {
  const { login } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/";
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true);
    try {
      await login(username.trim().toLowerCase(), password);
      router.replace(next);
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} id="loginForm">
      <h1>Вход</h1>
      <p className="aifa-subtitle">Войдите, чтобы продолжить.</p>

      <div className="aifa-field">
        <label htmlFor="login">Логин</label>
        <input
          id="login"
          name="login"
          type="text"
          placeholder="Логин"
          autoComplete="username"
          autoFocus
          value={username}
          onChange={(e) => setUsername(e.target.value)}
        />
      </div>

      <div className="aifa-field">
        <label htmlFor="password">Пароль</label>
        <input
          id="password"
          name="password"
          type="password"
          placeholder="Пароль"
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </div>

      <button
        className="aifa-button"
        type="submit"
        disabled={busy || !username.trim() || !password}
      >
        {busy ? "Входим…" : "Войти"}
      </button>

      <div className="aifa-meta">
        Нет учётной записи?{" "}
        <Link href="/register">Зарегистрироваться по инвайту</Link>
      </div>
    </form>
  );
}
