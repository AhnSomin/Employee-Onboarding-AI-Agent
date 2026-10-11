import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Approved, Extraction, MeetingConfig, MeetingRow, Row } from "./api";

const PATH_LABEL: Record<string, string> = {
  function_calling: "AI 추출 (함수 호출)", structured: "AI 추출 (구조화 출력)", rule_based: "규칙 기반 (AI 사용 불가)",
};
const STATE_LABEL: Record<string, string> = {
  created: "생성", sent: "발송", skipped_existing: "이미 처리됨", failed: "실패", dry_run: "시뮬레이션",
};
const STATUS_LABEL: Record<string, string> = { approved: "진행 중", done: "완료", cancelled: "취소", draft: "초안" };

type Stage = "input" | "review" | "done";

/** 내용 길이에 맞춰 높이가 자동으로 늘어나는 입력칸 (줄이 잘려 보이지 않게) */
function AutoTextarea({ value, onChange, min = 3 }: { value: string; onChange: (v: string) => void; min?: number }) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const fit = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${el.scrollHeight + 2}px`;
  };
  useLayoutEffect(fit, [value]);
  useEffect(() => {
    window.addEventListener("resize", fit);
    return () => window.removeEventListener("resize", fit);
  }, []);
  return <textarea ref={ref} rows={min} value={value} onChange={(e) => onChange(e.target.value)} style={{ overflow: "hidden" }} />;
}

export default function Meeting() {
  const [stage, setStage] = useState<Stage>("input");
  const [cfg, setCfg] = useState<MeetingConfig | null>(null);
  const [samples, setSamples] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [drag, setDrag] = useState(false);
  const [ex, setEx] = useState<Extraction | null>(null);
  const [rows, setRows] = useState<Row[]>([]);
  const [summary, setSummary] = useState("");
  const [decisions, setDecisions] = useState("");
  const [excludeUnconfirmed, setExcludeUnconfirmed] = useState(false);
  const [done, setDone] = useState<Approved | null>(null);
  const [board, setBoard] = useState<MeetingRow[]>([]);
  const input = useRef<HTMLInputElement>(null);

  const loadBoard = () => api.meetings().then(setBoard).catch(() => {});
  useEffect(() => {
    api.meetingConfig().then(setCfg).catch(() => {});
    api.samples().then(setSamples).catch(() => {});
    loadBoard();
  }, []);

  async function run(src: { file?: File; sample?: string }) {
    setErr(""); setBusy(true);
    try {
      const r = await api.extract(src);
      setEx(r); setRows(r.rows);
      setSummary(r.summary.join("\n")); setDecisions(r.decisions.join("\n"));
      setExcludeUnconfirmed(false); setStage("review");
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  const patch = (i: number, p: Partial<Row>) => setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...p } : r)));
  const unconfirmed = (r: Row) => !(r.owner_ok && r.owner !== "미확정") || !(r.due_ok && r.due_date);
  const included = rows.filter((r) => r.include && r.task.trim());
  const nOpen = included.filter(unconfirmed).length;
  const lines = (t: string) => t.split("\n").map((x) => x.trim()).filter(Boolean);

  async function approve() {
    if (!ex) return;
    setErr(""); setBusy(true);
    try {
      setDone(await api.approve({ extraction_id: ex.extraction_id, rows, summary: lines(summary), decisions: lines(decisions), exclude_unconfirmed: excludeUnconfirmed }));
      setStage("done"); loadBoard();
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  async function retry() {
    if (!done) return;
    setBusy(true); setErr("");
    try { setDone({ ...done, report: await api.execute(done.meeting_id) }); loadBoard(); }
    catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  }

  const reset = () => { setStage("input"); setEx(null); setDone(null); setErr(""); };
  const itemName = (id: string) => rows.find((r) => r.item_id === id)?.task ?? id;

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>회의록 → 액션</h2>
          <p className="muted">회의 텍스트에서 결정사항·담당자·기한을 뽑고, 사람이 확인한 뒤 일정과 Slack 알림으로 실행해요.</p>
        </div>
        {stage !== "input" && <button className="ghost" onClick={reset}>새 회의록</button>}
      </header>

      {cfg?.dry_run && (
        <p className="hint">시뮬레이션 모드예요. Google Calendar와 Slack으로 실제 전송하지 않고, 보낼 내용만 보여 줘요.</p>
      )}
      {err && <p className="error">⚠️ {err}</p>}

      {stage === "input" && (
        <>
          <div className={`drop ${drag ? "over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) run({ file: f }); }}>
            <p><strong>회의록 텍스트 파일(.txt)을 끌어다 놓으세요</strong></p>
            <div className="row">
              <button className="primary" onClick={() => input.current?.click()} disabled={busy}>파일 선택</button>
              {samples.map((s) => (
                <button key={s} className="ghost" onClick={() => run({ sample: s })} disabled={busy}>샘플: {s.replace(/\.txt$/, "")}</button>
              ))}
            </div>
            <input ref={input} type="file" accept=".txt,.md,text/plain" hidden
              onChange={(e) => { const f = e.target.files?.[0]; if (f) run({ file: f }); e.target.value = ""; }} />
          </div>
          {busy && <p className="status">회의록을 분석하는 중… (보통 5~15초)</p>}

          {board.length > 0 && (
            <div className="card" style={{ marginTop: 18 }}>
              <h3>액션 현황</h3>
              {board.map((m) => (
                <div key={m.meeting_id} className="meeting-block">
                  <strong>{m.title}</strong> <span className="muted">· {m.meeting_date}</span>
                  <ul className="board">
                    {m.items.map((i) => (
                      <li key={i.item_id}>
                        <span className={`pill ${i.status}`}>{STATUS_LABEL[i.status] ?? i.status}</span>
                        <span className="grow">{i.task} <span className="muted">· {i.owner} · {i.due || "기한 없음"}</span></span>
                        {i.status === "approved" && (
                          <span className="row-actions">
                            <button className="ghost small-btn" onClick={() => api.complete([i.item_id]).then(loadBoard)}>완료</button>
                            <button className="ghost small-btn" onClick={() => api.cancel([i.item_id]).then(loadBoard)}>취소</button>
                          </span>
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {stage === "review" && ex && (
        <div className="result">
          <div className="card meta">
            <div><strong>{ex.title}</strong> <span className="muted">· {ex.meeting_date}</span></div>
            <span className={`badge ${ex.fallback_used ? "warnb" : ""}`}>{PATH_LABEL[ex.path] ?? ex.path}{ex.model ? ` · ${ex.model}` : ""}</span>
          </div>
          {ex.warnings.length > 0 && <div className="hint">{ex.warnings.map((w, i) => <div key={i}>{w}</div>)}</div>}
          {ex.injection_blocked > 0 && <div className="hint">회의록 속에 AI를 향한 지시문이 있어 관련 항목 {ex.injection_blocked}건을 막았어요.</div>}

          <div className="grid2">
            <div className="card">
              <h3>요약 <span className="muted">· 한 줄에 하나, 고칠 수 있어요</span></h3>
              <AutoTextarea value={summary} onChange={setSummary} />
            </div>
            <div className="card">
              <h3>결정사항</h3>
              <AutoTextarea value={decisions} onChange={setDecisions} />
            </div>
          </div>

          <div className="card">
            <h3>액션 아이템 <span className="muted">· 담당자와 기한을 직접 고르면 확정으로 바뀌어요</span></h3>
            <div className="table">
              <div className="tr5 th"><span /><span>할 일</span><span>담당자</span><span>기한</span><span>상태</span></div>
              {rows.map((r, i) => (
                <div key={r.item_id} className={`tr5 ${r.include ? "" : "off"}`}>
                  <input type="checkbox" checked={r.include} onChange={(e) => patch(i, { include: e.target.checked })} aria-label="포함" />
                  <div>
                    <input value={r.task} onChange={(e) => patch(i, { task: e.target.value })} />
                    {r.evidence && <small className="evidence">“{r.evidence}”</small>}
                    {r.notes && <small className="evidence">🔎 {r.notes}</small>}
                  </div>
                  <select className={r.owner_ok ? "" : "warn"} value={r.owner}
                    onChange={(e) => patch(i, { owner: e.target.value, owner_ok: e.target.value !== "미확정" })}>
                    {[...new Set([...ex.owner_options, r.owner])].map((o) => <option key={o}>{o}</option>)}
                  </select>
                  <div className="due">
                    <input type="date" className={r.due_ok ? "" : "warn"} value={r.due_date ?? ""}
                      onChange={(e) => patch(i, { due_date: e.target.value || null, due_ok: !!e.target.value, due_time: e.target.value ? r.due_time : null })} />
                    <input type="time" value={r.due_time ?? ""} disabled={!r.due_date}
                      onChange={(e) => patch(i, { due_time: e.target.value || null })} />
                  </div>
                  <span className={`pill ${unconfirmed(r) ? "warnp" : "okp"}`}>{unconfirmed(r) ? "미확정" : "확정"}</span>
                </div>
              ))}
            </div>
            <div className="row" style={{ justifyContent: "flex-start", marginTop: 10 }}>
              <button className="ghost" onClick={() => setRows((rs) => [...rs, {
                item_id: crypto.randomUUID(), include: true, task: "", owner: "미확정", owner_ok: false,
                due_date: null, due_time: null, due_ok: false,
              }])}>+ 항목 추가</button>
            </div>
            {nOpen > 0 && <p className="hint">담당자나 기한이 미확정인 항목이 {nOpen}건 있어요. 회의록에 없어서 추측하지 않고 비워 뒀어요.</p>}
          </div>

          {ex.open_issues.length > 0 && (
            <div className="card"><h3>확인이 필요한 사항</h3><ul>{ex.open_issues.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
          )}

          <div className="card confirm">
            <label className="check">
              <input type="checkbox" checked={excludeUnconfirmed} onChange={(e) => setExcludeUnconfirmed(e.target.checked)} />
              미확정 항목은 빼고 확정된 것만 승인
            </label>
            <button className="primary" onClick={approve} disabled={busy || included.length === 0}>
              {busy ? "처리 중…" : "승인하고 실행"}
            </button>
          </div>
        </div>
      )}

      {stage === "done" && done && (
        <div className="result">
          <div className="card ok">
            <h3>{done.report?.dry_run ? "📝 시뮬레이션 결과" : "✅ 실행했어요"} — {done.approved}건 승인{done.excluded ? `, ${done.excluded}건 제외` : ""}</h3>
            {done.report?.warnings.map((w, i) => <p key={i} className="hint">{w}</p>)}
            <div className="table">
              <div className="tr3 th"><span>할 일</span><span>Calendar</span><span>Slack</span></div>
              {done.report?.results.map((r) => (
                <div key={r.item_id} className="tr3">
                  <span>{itemName(r.item_id)}{r.error && <small className="evidence error">{r.error}</small>}</span>
                  <span>{STATE_LABEL[r.calendar] ?? r.calendar}</span>
                  <span>{STATE_LABEL[r.slack] ?? r.slack}</span>
                </div>
              ))}
            </div>
            {done.report?.slack_text && (<><h3 style={{ marginTop: 14 }}>Slack 메시지{done.report.dry_run ? " (미리보기)" : ""}</h3><pre>{done.report.slack_text}</pre></>)}
            <div className="row" style={{ justifyContent: "flex-start" }}>
              {done.report?.failed && <button className="primary" onClick={retry} disabled={busy}>실패한 항목 다시 실행</button>}
              <button className="ghost" onClick={reset}>새 회의록</button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
