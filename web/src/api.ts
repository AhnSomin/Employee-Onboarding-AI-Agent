export type Task = { task_id: string; title: string; phase: string; due_date: string; done: number };
export type Me = { name: string; org: string; dept: string; done: number; total: number; tasks: Task[] };
export type ChatOut = { reply: string; mode: string; escalated: boolean };
export type ActionItem = { task: string; owner: string | null; due: string | null; evidence: string };
export type Analysis = {
  title: string; summary: string[]; decisions: string[]; action_items: ActionItem[];
  open_questions: string[]; model: string; truncated: boolean;
};
export type ConfirmOut = { slack: { sent: boolean; reason?: string; preview: string }; ics: string; events: number };

export const getCode = () => localStorage.getItem("access_code") ?? "";
export const setCode = (c: string) => localStorage.setItem("access_code", c);
export const clearCode = () => localStorage.removeItem("access_code");

async function call<T>(url: string, init: RequestInit = {}): Promise<T> {
  const r = await fetch(url, { ...init, headers: { ...(init.headers ?? {}), "X-Access-Code": getCode() } });
  if (r.status === 401) { clearCode(); window.dispatchEvent(new Event("need-code")); }
  if (!r.ok) {
    let msg = `요청 실패 (${r.status})`;
    try { msg = (await r.json()).detail ?? msg; } catch { /* 본문 없음 */ }
    throw new Error(msg);
  }
  return r.json();
}

const json = (body: unknown): RequestInit => ({
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});

export const api = {
  me: () => call<Me>("/api/me"),
  health: () => call<{ ok: boolean; llm: boolean; needs_code: boolean; rag_chunks: number }>("/api/health"),
  chat: (session_id: string, message: string) => call<ChatOut>("/api/chat", json({ session_id, message })),
  reset: (session_id: string) => call<{ ok: boolean }>("/api/chat/reset", json({ session_id, message: "-" })),
  analyze: (file: File) => {
    const f = new FormData();
    f.append("file", file);
    return call<Analysis>("/api/meeting/analyze", { method: "POST", body: f });
  },
  confirm: (title: string, items: { task: string; owner: string | null; due: string | null }[], send_slack: boolean) =>
    call<ConfirmOut>("/api/meeting/confirm", json({ title, items, send_slack })),
};
