import { useCallback, useEffect, useState } from "react";
import { api, Me, setCode } from "./api";
import Chat from "./Chat";
import Checklist from "./Checklist";
import Icon from "./Icons";
import { getTheme, setTheme, Theme } from "./theme";
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
        <h2>온보딩 Agent</h2>
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
  const [theme, setThemeState] = useState<Theme>(getTheme());
  const [showTodo, setShowTodo] = useState(() => window.matchMedia("(min-width: 821px)").matches);   // 모바일에서는 접어 둔다
  const toggleTheme = () => { const t: Theme = theme === "dark" ? "light" : "dark"; setTheme(t); setThemeState(t); };
  const [me, setMe] = useState<Me | null>(null);
  const refresh = useCallback(() => { api.me().then(setMe).catch(() => {}); }, []);
  useEffect(refresh, [refresh]);

  return (
    <div className="shell">
      <aside className="side">
        <div className="brand">
          <div className="brand-mark"><span className="logo"><Icon name="compass" size={18} /></span>온보딩 Agent</div>
          <button className="theme-btn" onClick={toggleTheme} aria-label={theme === "dark" ? "라이트 모드로" : "다크 모드로"} title={theme === "dark" ? "라이트 모드" : "다크 모드"}>
            <Icon name={theme === "dark" ? "sun" : "moon"} size={16} />
          </button>
        </div>
        <nav>
          <button className={tab === "chat" ? "nav on" : "nav"} onClick={() => setTab("chat")}><Icon name="chat" />규정 질문하기</button>
          <button className={tab === "meeting" ? "nav on" : "nav"} onClick={() => setTab("meeting")}><Icon name="doc" />회의록 → 액션</button>
        </nav>
        {me && (
          <div className="me">
            <div className="who"><span className="avatar">{me.name.slice(0, 1)}</span><div className="who-text"><strong>{me.name}</strong><span>{me.org} · {me.dept}</span></div></div>
            <button className="todo-toggle" onClick={() => setShowTodo((v) => !v)} aria-expanded={showTodo}>
              <span>할 일 {me.done}/{me.total} 완료</span><span>{showTodo ? "접기" : "펼치기"}</span>
            </button>
            <div className={showTodo ? "todo-wrap" : "todo-wrap collapsed"}><Checklist me={me} onChange={refresh} /></div>
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
