"use client";

import { useState, type FormEvent } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { toast } from "sonner";
import { useAuth } from "@/components/auth-context";
import { AuthShell } from "@/components/auth-shell";

export default function RegisterPage() {
  const { register } = useAuth();
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [invite, setInvite] = useState("");
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password || !invite.trim()) return;
    if (password.length < 8) {
      toast.error("Пароль минимум 8 символов");
      return;
    }
    setBusy(true);
    try {
      await register(username.trim().toLowerCase(), password, invite.trim());
      toast.success("Аккаунт создан");
      router.replace("/");
    } catch (err) {
      toast.error((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthShell>
      <form onSubmit={onSubmit} id="registerForm">
        <h1>Регистрация</h1>
        <p className="aifa-subtitle">Только по приглашению.</p>

        <div className="aifa-field">
          <label htmlFor="reg-login">Логин</label>
          <input
            id="reg-login"
            type="text"
            placeholder="3–32 символа: a-z, цифры, . _ -"
            autoComplete="username"
            autoFocus
            value={username}
            onChange={(e) => setUsername(e.target.value)}
          />
        </div>

        <div className="aifa-field">
          <label htmlFor="reg-password">Пароль</label>
          <input
            id="reg-password"
            type="password"
            placeholder="минимум 8 символов"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <div className="aifa-field">
          <label htmlFor="reg-invite">Код приглашения</label>
          <input
            id="reg-invite"
            type="text"
            placeholder="буквы и цифры"
            autoComplete="off"
            className="aifa-mono"
            value={invite}
            onChange={(e) => setInvite(e.target.value)}
          />
        </div>

        <button
          className="aifa-button"
          type="submit"
          disabled={busy || !username.trim() || !password || !invite.trim()}
        >
          {busy ? "Создаём…" : "Зарегистрироваться"}
        </button>

        <div className="aifa-meta">
          Уже есть аккаунт? <Link href="/login">Войти</Link>
        </div>
      </form>
    </AuthShell>
  );
}
