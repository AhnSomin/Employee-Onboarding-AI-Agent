import { useCallback, useEffect, useState } from "react";
import { api, Me, setCode } from "./api";
import Chat from "./Chat";
import Meeting from "./Meeting";

type Tab = "chat" | "meeting";

function Gate({ onOk }: { onOk: () => void }) {
  const [v, setV] = useState("");
  const [err, setErr] = useState("");
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setCode(v.trim());
    try { await api.me(); onOk(); } catch { setErr("접근 코드가 맞지 않아요."); }
  }
  return (
    <div className="gate">
      <form className="panel" onSubmit={submit}>
        <h2>🧭 온보딩 Agent</h2>
        <p className="muted">팀에서 받은 접근 코드를 입력해 주세요.</p>
        <input type="password" value={v} onChange={(e) => setV(e.target.value)} placeholder="접근 코드" autoFocus />
        {err && <p className="error">{err}</p>}
        <button className="primary" disabled={!v.trim()}>입장</button>
      </form>
    </div>
  );
}

export default function App() {
  const [locked, setLocked] = useState(false);
  useEffect(() => {
    const on = () => setLocked(true);
    window.addEventListener("need-code", on);
    return () => window.removeEventListener("need-code", on);
  }, []);
  return locked ? <Gate onOk={() => setLocked(false)} /> : <Main key="main" />;
}

function Main() {
  const [tab, setTab] = useState<Tab>("chat");
  const [me, setMe] = useState<Me | null>(null);
  const refresh = useCallback(() => { api.me().then(setMe).catch(() => {}); }, []);
  useEffect(refresh, [refresh]);
  const pct = me && me.total ? Math.round((me.done / me.total) * 100) : 0;

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">🧭 온보딩 Agent</div>
        <nav>
          <button className={tab === "chat" ? "nav on" : "nav"} onClick={() => setTab("chat")}>💬 규정 질문하기</button>
          <button className={tab === "meeting" ? "nav on" : "nav"} onClick={() => setTab("meeting")}>📋 회의록 → 액션</button>
        </nav>
        {me && (
          <div className="me">
            <div className="who"><strong>{me.name}</strong><span className="muted">{me.org} · {me.dept}</span></div>
            <div className="bar" aria-label={`온보딩 진행률 ${pct}%`}><i style={{ width: `${pct}%` }} /></div>
            <div className="muted small">온보딩 진행률 {me.done}/{me.total}</div>
            <ul className="tasks">
              {me.tasks.map((t) => <li key={t.task_id} className={t.done ? "done" : ""}>{t.done ? "✅" : "⬜"} {t.title}</li>)}
            </ul>
          </div>
        )}
      </aside>
      <main>
        <div style={{ display: tab === "chat" ? "block" : "none" }}><Chat onActivity={refresh} /></div>
        <div style={{ display: tab === "meeting" ? "block" : "none" }}><Meeting /></div>
      </main>
    </div>
  );
}
