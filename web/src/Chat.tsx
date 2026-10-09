import { useEffect, useRef, useState } from "react";
import { api } from "./api";

type Msg = { role: "user" | "assistant"; text: string; escalated?: boolean };

const SUGGESTIONS = [
  "반차 2번 쓰면 하루로 쳐요?",
  "입사 1년 안 됐는데 연가 며칠이에요?",
  "결혼하면 휴가 며칠 받아요?",
  "출장 가면 숙박비는 어떻게 돼요?",
  "기획조정관은 무슨 일을 하나요?",
];

function sessionId() {
  let id = sessionStorage.getItem("sid");
  if (!id) { id = crypto.randomUUID(); sessionStorage.setItem("sid", id); }
  return id;
}

function Rich({ text }: { text: string }) {
  // **굵게** 만 지원하는 가벼운 렌더링
  return (
    <>
      {text.split(/(\*\*[^*]+\*\*)/g).map((p, i) =>
        p.startsWith("**") && p.endsWith("**") ? <strong key={i}>{p.slice(2, -2)}</strong> : <span key={i}>{p}</span>)}
    </>
  );
}

export default function Chat({ onActivity }: { onActivity: () => void }) {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState("");
  const end = useRef<HTMLDivElement>(null);

  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [msgs, busy]);

  async function send(text: string) {
    const t = text.trim();
    if (!t || busy) return;
    setInput("");
    setMsgs((m) => [...m, { role: "user", text: t }]);
    setBusy(true);
    try {
      const r = await api.chat(sessionId(), t);
      setMode(r.mode);
      setMsgs((m) => [...m, { role: "assistant", text: r.reply, escalated: r.escalated }]);
      onActivity();
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", text: `⚠️ ${(e as Error).message}` }]);
    } finally { setBusy(false); }
  }

  async function reset() {
    await api.reset(sessionId()).catch(() => {});
    setMsgs([]);
  }

  return (
    <section className="panel chat">
      <header className="panel-head">
        <div>
          <h2>규정 질문하기</h2>
          <p className="muted">복무규정·여비·보수·직제 규정을 근거로 쉽게 풀어 답해요.</p>
        </div>
        <div className="head-actions">
          {mode && <span className="badge">{mode}</span>}
          {msgs.length > 0 && <button className="ghost" onClick={reset}>대화 초기화</button>}
        </div>
      </header>

      <div className="messages">
        {msgs.length === 0 && (
          <div className="empty">
            <p>무엇이 궁금하세요? 아래 예시를 눌러 보세요.</p>
            <div className="chips">
              {SUGGESTIONS.map((s) => <button key={s} className="chip" onClick={() => send(s)}>{s}</button>)}
            </div>
          </div>
        )}
        {msgs.map((m, i) => (
          <div key={i} className={`bubble ${m.role}`}>
            <div className="text"><Rich text={m.text} /></div>
            {m.escalated && <div className="note">🙋 인사담당관실 담당자에게 전달했어요</div>}
          </div>
        ))}
        {busy && <div className="bubble assistant"><div className="typing"><i /><i /><i /></div></div>}
        <div ref={end} />
      </div>

      <form className="composer" onSubmit={(e) => { e.preventDefault(); send(input); }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} placeholder="예: 병가는 며칠까지 쓸 수 있어요?" disabled={busy} />
        <button className="primary" disabled={busy || !input.trim()}>보내기</button>
      </form>
    </section>
  );
}
