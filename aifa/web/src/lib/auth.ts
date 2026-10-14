// Thin auth client. The session cookie is set by the backend; the browser
// sends it automatically because the frontend and backend share the origin
// homyakagent.ru in production.

const API = process.env.NEXT_PUBLIC_API_BASE_URL ?? "/api";

export type CurrentUser = {
  username: string;
  is_admin: boolean;
  /** Premium model keys (e.g. ["kimi", "glm"]) granted via invite. The base
   * model is always available regardless of this list. */
  allowed_models?: string[];
};

async function handleAuth<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    let msg = text;
    try {
      const j = JSON.parse(text);
      if (typeof j?.detail === "string") msg = j.detail;
    } catch {}
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export async function authMe(): Promise<CurrentUser | null> {
  const res = await fetch(`${API}/auth/me`, {
    credentials: "include",
    cache: "no-store",
  });
  if (res.status === 401) return null;
  return handleAuth<CurrentUser>(res);
}

export async function authLogin(username: string, password: string): Promise<CurrentUser> {
  return handleAuth<CurrentUser>(
    await fetch(`${API}/auth/login`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  );
}

export async function authRegister(
  username: string,
  password: string,
  invite_code: string,
): Promise<CurrentUser> {
  return handleAuth<CurrentUser>(
    await fetch(`${API}/auth/register`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, invite_code }),
    }),
  );
}

export async function authLogout(): Promise<void> {
  await fetch(`${API}/auth/logout`, {
    method: "POST",
    credentials: "include",
  });
}

export type Invite = {
  code: string;
  created_at: string;
  expires_at: string | null;
  used_by: string | null;
  allowed_models: string[];
};

export async function listInvites(): Promise<Invite[]> {
  return handleAuth<Invite[]>(
    await fetch(`${API}/auth/invites`, {
      credentials: "include",
      cache: "no-store",
    }),
  );
}

export async function createInvite(
  expires_in_days?: number,
  allowed_models: string[] = [],
): Promise<Invite> {
  return handleAuth<Invite>(
    await fetch(`${API}/auth/invites`, {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        expires_in_days: expires_in_days ?? null,
        allowed_models,
      }),
    }),
  );
}

export async function deleteInvite(code: string): Promise<void> {
  const res = await fetch(`${API}/auth/invites/${encodeURIComponent(code)}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!res.ok) throw new Error(`${res.status}`);
}

// ── Admin: extract-run log ────────────────────────────────────────────────
export type ExtractRun = {
  ts: string;
  git_ver?: string;       // human-readable deploy label, e.g. "v123"
  git_sha?: string;       // short-sha of the deploy (technical traceability)
  doc_id: string;
  filename: string;
  arch: string;
  model_id: string;
  company_type: string;
  company_name: string;
  found: number;
  total: number;
  elapsed_min: number;
  broken: boolean;
  broken_reason: string;
  idpocr_available: boolean;
  diag: Record<string, unknown>;       // includes `reranker`, ocr_s, etc.
  error: string | null;
};

export async function listExtractRuns(limit = 200): Promise<{ runs: ExtractRun[]; total: number }> {
  return handleAuth<{ runs: ExtractRun[]; total: number }>(
    await fetch(`${API}/admin/runs?limit=${limit}`, {
      credentials: "include",
      cache: "no-store",
    }),
  );
}

export function adminIdpOcrUrl(docId: string): string {
  return `${API}/admin/documents/${encodeURIComponent(docId)}/idpocr.json`;
}

export function adminExtractRunsJsonlUrl(): string {
  return `${API}/admin/extract-runs.jsonl`;
}
