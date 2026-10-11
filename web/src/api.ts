export type Task = { task_id: string; title: string; phase: string; due_date: string; done: number };
export type Me = { name: string; org: string; dept: string; done: number; total: number; tasks: Task[] };
export type ChatOut = { reply: string; mode: string; escalated: boolean };
export type Row = {
  item_id: string; include: boolean; task: string; owner: string; owner_ok: boolean;
  due_date: string | null; due_time: string | null; due_ok: boolean;
  due_text?: string; status?: string; evidence?: string; notes?: string; needs_review?: boolean;
};
export type Extraction = {
  extraction_id: string; title: string; meeting_date: string; summary: string[]; decisions: string[];
  open_issues: string[]; rows: Row[]; owner_options: string[]; path: string; model: string | null;
  fallback_used: boolean; warnings: string[]; injection_blocked: number; dry_run: boolean;
};
export type ExecResult = { item_id: string; calendar: string; slack: string; error: string | null };
export type Report = {
  dry_run: boolean; failed: boolean; warnings: string[]; slack_text: string | null;
  slack_thread_reply: boolean; calendar_payloads: Record<string, unknown>[]; results: ExecResult[];
};
export type Approved = { meeting_id: string; approved: number; excluded: number; report: Report | null };
export type MeetingConfig = { dry_run: boolean; provider: string; calendar_ready: boolean; slack_ready: boolean };
export type MeetingRow = {
  meeting_id: string; title: string; meeting_date: string;
  items: { item_id: string; task: string; owner: string; due: string; status: string; calendar: boolean; slack: boolean }[];
};

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
  meetingConfig: () => call<MeetingConfig>("/api/meeting/config"),
  samples: () => call<string[]>("/api/meeting/samples"),
  extract: (src: { file?: File; sample?: string }) => {
    const f = new FormData();
    if (src.file) f.append("file", src.file);
    if (src.sample) f.append("sample", src.sample);
    return call<Extraction>("/api/meeting/extract", { method: "POST", body: f });
  },
  approve: (body: {
    extraction_id: string; rows: Row[]; summary: string[]; decisions: string[]; exclude_unconfirmed: boolean;
  }) => call<Approved>("/api/meeting/approve", json({ ...body, execute: true })),
  execute: (id: string) => call<Report>(`/api/meeting/${id}/execute`, { method: "POST" }),
  meetings: () => call<MeetingRow[]>("/api/meeting/list"),
  complete: (item_ids: string[]) => call<{ updated: number }>("/api/meeting/items/complete", json({ item_ids })),
  cancel: (item_ids: string[]) => call<{ updated: number }>("/api/meeting/items/cancel", json({ item_ids })),
};
