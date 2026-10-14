"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ArrowLeft, Copy, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/components/auth-context";
import { createInvite, deleteInvite, listInvites, type Invite } from "@/lib/auth";

export default function InvitesPage() {
  const { user, loading } = useAuth();
  const [invites, setInvites] = useState<Invite[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [expiresIn, setExpiresIn] = useState<string>("30");
  // Premium model grants — keys come from web/src/lib/prefs.ts MODEL_OPTIONS.
  // The base model is always available for everyone, no checkbox needed.
  const [modelGrants, setModelGrants] = useState<Record<string, boolean>>({
    kimi: false,
    glm: false,
  });

  const refresh = useCallback(async () => {
    try {
      setInvites(await listInvites());
    } catch (e) {
      toast.error((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!loading && user?.is_admin) void refresh();
  }, [loading, user, refresh]);

  if (loading) return null;
  if (!user) return null;
  if (!user.is_admin) {
    return (
      <div className="h-full flex items-center justify-center text-sm text-muted-foreground">
        Доступ только для администратора.
      </div>
    );
  }

  async function generate() {
    setBusy(true);
    try {
      const days = expiresIn ? Number(expiresIn) : undefined;
      const grants = Object.entries(modelGrants)
        .filter(([, on]) => on)
        .map(([k]) => k);
      const inv = await createInvite(days && days > 0 ? days : undefined, grants);
      toast.success(`Создан код: ${inv.code}`);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(code: string) {
    if (!confirm(`Удалить код ${code}?`)) return;
    try {
      await deleteInvite(code);
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    }
  }

  return (
    <div className="h-full overflow-auto bg-background">
      <div className="max-w-3xl mx-auto px-6 py-8 space-y-6">
        <Link href="/" className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
          <ArrowLeft className="size-4" /> К чату
        </Link>

        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Коды приглашений</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Сгенерируй код, отправь тестеру. Он зайдёт на /register, придумает себе логин/пароль и активирует код.
          </p>
        </div>

        <div className="rounded-md border p-4 bg-card space-y-3">
          <h3 className="font-medium">Создать новый код</h3>
          <div className="flex items-end gap-2">
            <div className="space-y-1">
              <label className="text-xs text-muted-foreground">Срок действия (дней, пусто = бессрочный)</label>
              <input
                type="number"
                min={1}
                max={365}
                value={expiresIn}
                onChange={(e) => setExpiresIn(e.target.value)}
                className="w-32 rounded-md border bg-background px-2 py-1.5 text-sm"
              />
            </div>
            <Button size="sm" onClick={generate} disabled={busy}>
              {busy ? "…" : "+ Создать"}
            </Button>
          </div>
          <div className="space-y-1.5">
            <label className="text-xs text-muted-foreground">
              Дополнительные модели для пользователя (по умолчанию доступна только базовая)
            </label>
            <div className="flex flex-wrap gap-3">
              {[
                { k: "kimi", title: "Kimi K2.5" },
                { k: "glm", title: "GLM 5.1" },
              ].map(({ k, title }) => (
                <label key={k} className="inline-flex items-center gap-1.5 text-sm">
                  <input
                    type="checkbox"
                    checked={!!modelGrants[k]}
                    onChange={(e) => setModelGrants((g) => ({ ...g, [k]: e.target.checked }))}
                  />
                  <span>{title}</span>
                </label>
              ))}
            </div>
          </div>
        </div>

        <div className="rounded-md border p-4 bg-card">
          <h3 className="font-medium mb-3">Все коды ({invites?.length ?? "…"})</h3>
          {!invites && <p className="text-sm text-muted-foreground">Загружаю…</p>}
          {invites && invites.length === 0 && (
            <p className="text-sm text-muted-foreground">Пока нет ни одного кода.</p>
          )}
          <div className="space-y-2">
            {invites?.map((i) => {
              const used = !!i.used_by;
              const expired =
                i.expires_at && new Date(i.expires_at).getTime() < Date.now();
              return (
                <div
                  key={i.code}
                  className="flex items-center gap-3 rounded-md border bg-background px-3 py-2"
                >
                  <code className="font-mono text-sm flex-1 truncate">{i.code}</code>
                  <div className="text-xs text-muted-foreground min-w-0 truncate">
                    {used ? `использован: ${i.used_by}` : expired ? "истёк" : "доступен"}
                  </div>
                  <button
                    type="button"
                    onClick={() => {
                      void navigator.clipboard.writeText(i.code);
                      toast.success("Скопировано");
                    }}
                    className="p-1.5 rounded hover:bg-muted text-muted-foreground"
                    title="Скопировать"
                  >
                    <Copy className="size-4" />
                  </button>
                  {!used && (
                    <button
                      type="button"
                      onClick={() => remove(i.code)}
                      className="p-1.5 rounded hover:bg-muted text-muted-foreground hover:text-destructive"
                      title="Удалить"
                    >
                      <Trash2 className="size-4" />
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
