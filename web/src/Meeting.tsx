import { useRef, useState } from "react";
import { api, Analysis, ConfirmOut } from "./api";

type Row = { task: string; owner: string; due: string; evidence: string; on: boolean };

function download(name: string, text: string, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

export default function Meeting() {
  const [fileName, setFileName] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [res, setRes] = useState<Analysis | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [title, setTitle] = useState("");
  const [slack, setSlack] = useState(true);
  const [done, setDone] = useState<ConfirmOut | null>(null);
  const [drag, setDrag] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  async function run(file: File) {
    setErr(""); setRes(null); setDone(null); setBusy(true); setFileName(file.name);
    try {
      const r = await api.analyze(file);
      setRes(r); setTitle(r.title);
      setRows(r.action_items.map((a) => ({ task: a.task, owner: a.owner ?? "", due: a.due ?? "", evidence: a.evidence, on: true })));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  async function sample() {
    const t = await (await fetch("/sample-meeting.txt")).text();
    run(new File([t], "sample-meeting.txt", { type: "text/plain" }));
  }

  const patch = (i: number, p: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...p } : r)));
  const chosen = rows.filter((r) => r.on && r.task.trim());
  const missing = chosen.filter((r) => !r.owner.trim() || !r.due).length;

  async function confirm() {
    setErr(""); setBusy(true);
    try {
      setDone(await api.confirm(title, chosen.map((r) => ({ task: r.task, owner: r.owner.trim() || null, due: r.due || null })), slack));
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>회의록 → 액션</h2>
          <p className="muted">회의 텍스트를 올리면 요약하고, 결정사항·담당자·기한을 뽑아 일정과 알림으로 만들어요.</p>
        </div>
      </header>

      <div
        className={`drop ${drag ? "over" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) run(f); }}
      >
        <p><strong>{fileName || "회의록 텍스트 파일(.txt)을 끌어다 놓으세요"}</strong></p>
        <div className="row">
          <button className="primary" onClick={() => input.current?.click()} disabled={busy}>파일 선택</button>
          <button className="ghost" onClick={sample} disabled={busy}>샘플 회의록으로 해보기</button>
        </div>
        <input ref={input} type="file" accept=".txt,.md,text/plain" hidden onChange={(e) => { const f = e.target.files?.[0]; if (f) run(f); e.target.value = ""; }} />
      </div>

      {busy && !res && <p className="status">회의록을 분석하는 중… (보통 5~15초)</p>}
      {err && <p className="error">⚠️ {err}</p>}

      {res && (
        <div className="result">
          <div className="grid2">
            <div className="card">
              <h3>요약</h3>
              <ul>{res.summary.map((s, i) => <li key={i}>{s}</li>)}</ul>
            </div>
            <div className="card">
              <h3>결정사항</h3>
              {res.decisions.length ? <ul>{res.decisions.map((s, i) => <li key={i}>{s}</li>)}</ul> : <p className="muted">확정된 결정사항이 없어요.</p>}
            </div>
          </div>

          <div className="card">
            <h3>액션 아이템 <span className="muted">· 내용을 확인하고 필요하면 고쳐 주세요</span></h3>
            <div className="table">
              <div className="tr th"><span /><span>할 일</span><span>담당자</span><span>기한</span></div>
              {rows.map((r, i) => (
                <div key={i} className={`tr ${r.on ? "" : "off"}`}>
                  <input type="checkbox" checked={r.on} onChange={(e) => patch(i, { on: e.target.checked })} aria-label="포함" />
                  <div>
                    <input value={r.task} onChange={(e) => patch(i, { task: e.target.value })} />
                    <small className="evidence">“{r.evidence}”</small>
                  </div>
                  <input className={r.owner ? "" : "warn"} value={r.owner} placeholder="미정" onChange={(e) => patch(i, { owner: e.target.value })} />
                  <input className={r.due ? "" : "warn"} type="date" value={r.due} onChange={(e) => patch(i, { due: e.target.value })} />
                </div>
              ))}
            </div>
            {rows.length === 0 && <p className="muted">추출된 액션 아이템이 없어요.</p>}
            {missing > 0 && <p className="hint">담당자나 기한이 비어 있는 항목이 {missing}건 있어요. 회의록에 없어서 추측하지 않고 비워 뒀어요. 기한이 없는 항목은 일정에 등록되지 않아요.</p>}
          </div>

          {res.open_questions.length > 0 && (
            <div className="card">
              <h3>확인이 필요한 사항</h3>
              <ul>{res.open_questions.map((s, i) => <li key={i}>{s}</li>)}</ul>
            </div>
          )}

          <div className="card confirm">
            <label className="check"><input type="checkbox" checked={slack} onChange={(e) => setSlack(e.target.checked)} /> Slack으로 담당자에게 알리기</label>
            <button className="primary" onClick={confirm} disabled={busy || chosen.length === 0}>
              {chosen.length}건 일정 만들고 알리기
            </button>
          </div>

          {done && (
            <div className="card ok">
              <h3>{done.slack.sent ? "✅ Slack 알림을 보냈어요" : "📝 Slack 알림 미리보기"}</h3>
              {!done.slack.sent && done.slack.reason && <p className="hint">{done.slack.reason}</p>}
              <pre>{done.slack.preview}</pre>
              <div className="row">
                <button className="primary" disabled={done.events === 0} onClick={() => download("action-items.ics", done.ics, "text/calendar")}>
                  📅 캘린더 파일 받기 ({done.events}건)
                </button>
                <span className="muted">내려받은 .ics를 Google Calendar·Outlook에서 열면 일정이 등록돼요.</span>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
