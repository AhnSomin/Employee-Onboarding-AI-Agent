import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, Approved, Extraction, MeetingConfig, MeetingRow, Row } from "./api";
import Icon from "./Icons";

const PATH_LABEL: Record<string, string> = {
  function_calling: "AI 추출 (함수 호출)", structured: "AI 추출 (구조화 출력)", rule_based: "규칙 기반 (AI 사용 불가)",
};
const STATE_LABEL: Record<string, string> = {
  created: "등록", sent: "발송", skipped_existing: "이미 처리됨", failed: "실패", dry_run: "보내지 않음", file: "파일(.ics)",
};
const SAMPLE_INFO: Record<string, { label: string; hint: string }> = {
  "01_structured_minutes": { label: "정리된 회의록", hint: "안건·결정사항·액션 아이템이 정리된 일반적인 회의록" },
  "02_transcript": { label: "대화 녹취록", hint: "참석자 발언이 시간순으로 이어진 녹취 형태" },
  "03_long_minutes": { label: "긴 회의록 (시연용)", hint: "안건 5개·액션 아이템 9개가 들어 있는 길이가 긴 회의록. 담당자 미정 항목 1건 포함" },
};

function download(name: string, text: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: "text/calendar;charset=utf-8" }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}
const icsName = (title: string) => `${title.replace(/[^\w가-힣]+/g, "-").replace(/^-|-$/g, "").slice(0, 40) || "회의"}-액션.ics`;
const STAGE_NAME: Record<string, string> = { "D-1": "마감 전날", "D-day": "마감 당일", overdue: "기한 지남" };
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
  const [asOf, setAsOf] = useState("");
  const [remind, setRemind] = useState<{ mode: "preview" | "sent"; data: Reminders } | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const runRemind = async (send: boolean) => {
    setErr(""); setBusy(true);
    try {
      const data = await (send ? api.remindSend : api.remindPreview)(asOf || null);
      setRemind({ mode: send ? "sent" : "preview", data });
    } catch (e) { setErr((e as Error).message); } finally { setBusy(false); }
  };
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
  const sentIds = new Set(done?.report?.results.map((r) => r.item_id) ?? []);
  const skipped = rows.filter((r) => r.task.trim() && !sentIds.has(r.item_id));
  const skipReason = (r: Row) => !r.include ? "직접 제외한 항목" : unconfirmed(r) ? "담당자·기한이 미확정이라 제외" : "승인되지 않음";
  const itemName = (id: string) => rows.find((r) => r.item_id === id)?.task ?? id;

  return (
    <section className="panel">
      <header className="panel-head">
        <div>
          <h2>회의 정리</h2>
          <p className="muted">회의록을 올리면 결정사항·담당자·기한을 정리해 주고, 확인하면 Slack 공유와 캘린더 일정까지 만들어요.</p>
        </div>
        {stage !== "input" && <button className="ghost" onClick={reset}>새 회의록</button>}
      </header>

      {err && <p className="error"><Icon name="alert" size={16} />{err}</p>}

      {stage === "input" && (
        <>
          <div className={`drop ${drag ? "over" : ""}`}
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) run({ file: f }); }}>
            <p><strong>회의록 텍스트 파일(.txt)을 끌어다 놓으세요</strong></p>
            <div className="row">
              <button className="primary btn-icon" onClick={() => input.current?.click()} disabled={busy}><Icon name="upload" size={16} />파일 선택</button>
            </div>
            {samples.length > 0 && (
              <div className="samples">
                <span className="muted small">회의록이 없다면 샘플로 체험해 보세요</span>
                <div className="row">
                  {samples.map((n) => {
                    const key = n.replace(/\.txt$/, "");
                    const info = SAMPLE_INFO[key];
                    return <button key={n} className="ghost small-btn" title={info?.hint} onClick={() => run({ sample: n })} disabled={busy}>{info?.label ?? key}</button>;
                  })}
                </div>
              </div>
            )}
            <input ref={input} type="file" accept=".txt,.md,text/plain" hidden
              onChange={(e) => { const f = e.target.files?.[0]; if (f) run({ file: f }); e.target.value = ""; }} />
          </div>
          {busy && <p className="status">회의록을 분석하는 중… (보통 5~15초)</p>}

          {board.length > 0 && (
            <div className="card" style={{ marginTop: 18 }}>
              <h3>액션 현황</h3>
              {board.map((m) => (
                <div key={m.meeting_id} className="meeting-block">
                  <div className="meeting-line">
                    <div className="meeting-title"><strong>{m.title}</strong><span className="muted small">{m.meeting_date}</span></div>
                    {m.items.some((i) => i.status === "approved") && (
                      <button className="ghost small-btn btn-icon" onClick={() => api.calendarFile(m.meeting_id).then((r) => download(icsName(m.title), r.ics)).catch((e) => setErr((e as Error).message))}>
                        <Icon name="calendar" size={14} />캘린더 파일
                      </button>
                    )}
                  </div>
                  <ul className="board">
                    {m.items.map((i) => (
                      <li key={i.item_id} className={`board-item ${i.status}`}>
                        <div className="board-main">
                          <span className="board-task">{i.task}</span>
                          <span className="board-meta muted small">{i.owner} · {i.due || "기한 없음"}</span>
                        </div>
                        <div className="board-side">
                          <span className={`pill ${i.status}`}>{STATUS_LABEL[i.status] ?? i.status}</span>
                          {i.status === "approved" && (
                            <span className="row-actions">
                              <button className="ghost small-btn" onClick={() => api.complete([i.item_id]).then(loadBoard)}>완료</button>
                              <button className="ghost small-btn" onClick={() => api.cancel([i.item_id]).then(loadBoard)}>취소</button>
                            </span>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          )}
          {board.length > 0 && (
            <div className="card remind" style={{ marginTop: 18 }}>
              <h3>재알림</h3>
              <p className="muted small">마감 전날·마감 당일·기한이 지난 일정에 담당자를 챙기는 알림을 보내요. 이미 보낸 단계는 다시 보내지 않아요. 기준 날짜를 바꾸면 그날 나갈 알림을 미리 볼 수 있어요.</p>
              <div className="remind-bar">
                <label className="field-inline">기준 날짜
                  <input type="date" value={asOf} onChange={(e) => { setAsOf(e.target.value); setRemind(null); }} />
                </label>
                <button className="ghost" onClick={() => runRemind(false)} disabled={busy}>미리보기</button>
                <button className="primary" onClick={() => runRemind(true)} disabled={busy}>
                  {cfg && !cfg.dry_run && cfg.slack_ready ? "Slack으로 보내기" : "보내기 (시뮬레이션)"}
                </button>
              </div>
              {remind && (
                <>
                  <p className="status small" style={{ margin: "12px 0 0" }}>
                    {remind.data.as_of} 기준 · {remind.mode === "preview"
                      ? `보낼 알림 ${remind.data.reminders.length}건 (아직 보내지 않았어요)`
                      : `${remind.data.sent ?? 0}건 ${remind.data.dry_run ? "처리 (Slack으로는 보내지 않음)" : "보냄"}${remind.data.failed ? `, 실패 ${remind.data.failed}건` : ""}`}
                  </p>
                  {remind.data.reminders.length === 0 && <p className="muted small">이 날짜에 보낼 알림이 없어요. (마감 전날·당일·기한 지남에 해당하는 일정이 없거나, 이미 보냈어요.)</p>}
                  <ul className="results">
                    {remind.data.reminders.map((r) => (
                      <li key={r.item_id} className="result-row">
                        <span className="result-task">{r.task}</span>
                        <span className="result-states">
                          <span className={`state stage-${r.stage}`}><b>{STAGE_NAME[r.stage]}</b>{r.owner} · {r.due}</span>
                          {remind.mode === "sent" && <span className={`state ${r.sent ? "sent" : "failed"}`}>{r.sent ? "처리됨" : "실패"}</span>}
                        </span>
                        <blockquote className="remind-msg">{r.message}</blockquote>
                        {r.error && <small className="evidence error">{r.error}</small>}
                      </li>
                    ))}
                  </ul>
                </>
              )}
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
            <div className="items">
              {rows.map((r, i) => (
                <div key={r.item_id} className={`item ${r.include ? "" : "off"} ${unconfirmed(r) ? "open" : ""}`}>
                  <div className="item-head">
                    <label className="check small"><input type="checkbox" checked={r.include} onChange={(e) => patch(i, { include: e.target.checked })} />이 항목 포함</label>
                    <span className={`pill ${unconfirmed(r) ? "warnp" : "okp"}`}>{unconfirmed(r) ? "미확정" : "확정"}</span>
                  </div>
                  <label className="field wide">
                    <span className="field-label">할 일</span>
                    <input value={r.task} onChange={(e) => patch(i, { task: e.target.value })} />
                  </label>
                  {r.evidence && <small className="evidence">회의록 원문: “{r.evidence}”</small>}
                  {r.notes && <small className="evidence">검토 메모: {r.notes}</small>}
                  <div className="item-fields">
                    <label className="field">
                      <span className="field-label">담당자</span>
                      <select className={r.owner_ok ? "" : "warn"} value={r.owner}
                        onChange={(e) => patch(i, { owner: e.target.value, owner_ok: e.target.value !== "미확정" })}>
                        {[...new Set([...ex.owner_options, r.owner])].map((o) => <option key={o}>{o}</option>)}
                      </select>
                    </label>
                    <label className="field">
                      <span className="field-label">기한 날짜</span>
                      <input type="date" className={r.due_ok ? "" : "warn"} value={r.due_date ?? ""}
                        onChange={(e) => patch(i, { due_date: e.target.value || null, due_ok: !!e.target.value, due_time: e.target.value ? r.due_time : null })} />
                    </label>
                    <label className="field">
                      <span className="field-label">시각 <span className="muted">(선택, 비우면 종일)</span></span>
                      <input type="time" value={r.due_time ?? ""} disabled={!r.due_date}
                        onChange={(e) => patch(i, { due_time: e.target.value || null })} />
                    </label>
                  </div>
                </div>
              ))}
            </div>
            <div className="row" style={{ justifyContent: "flex-start", marginTop: 10 }}>
              <button className="ghost btn-icon" onClick={() => setRows((rs) => [...rs, {
                item_id: crypto.randomUUID(), include: true, task: "", owner: "미확정", owner_ok: false,
                due_date: null, due_time: null, due_ok: false,
              }])}><Icon name="plus" size={15} />항목 추가</button>
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
              {busy ? "처리 중…" : cfg && !cfg.dry_run && cfg.slack_ready ? "승인하고 Slack으로 보내기" : "승인하고 실행"}
            </button>
          </div>
        </div>
      )}

      {stage === "done" && done && (
        <div className="result">
          <div className="card ok">
            <h3>{done.report?.dry_run ? "내용만 확인했어요 (Slack으로 보내지 않음)" : done.report?.failed ? "일부만 처리됐어요" : "Slack으로 보냈어요"} — {done.approved}건 승인{done.excluded ? `, ${done.excluded}건 제외` : ""}</h3>
            {done.report?.warnings.map((w, i) => <p key={i} className="hint">{w}</p>)}
            <ul className="results">
              {done.report?.results.map((r) => (
                <li key={r.item_id} className="result-row">
                  <span className="result-task">{itemName(r.item_id)}</span>
                  <span className="result-states">
                    <span className={`state ${r.calendar}`}><b>캘린더</b>{STATE_LABEL[r.calendar] ?? r.calendar}</span>
                    <span className={`state ${r.slack}`}><b>Slack</b>{STATE_LABEL[r.slack] ?? r.slack}</span>
                  </span>
                  {r.error && <small className="evidence error">{r.error}</small>}
                </li>
              ))}
              {skipped.map((r) => (
                <li key={r.item_id} className="result-row skipped">
                  <span className="result-task">{r.task}</span>
                  <span className="result-states">
                    <span className="state"><b>캘린더</b>등록 안 함</span>
                    <span className="state"><b>Slack</b>발송 안 함</span>
                  </span>
                  <small className="muted">{skipReason(r)}</small>
                </li>
              ))}
            </ul>
            {done.report?.slack_text && (<><h3 style={{ marginTop: 14 }}>Slack 메시지{done.report.dry_run ? " (미리보기)" : ""}</h3><pre>{done.report.slack_text}</pre></>)}
            {done.ics && (done.ics_events ?? 0) > 0 && (
              <div className="ics-box">
                <button className="primary btn-icon" onClick={() => download(icsName(ex?.title ?? "회의"), done.ics!)}><Icon name="calendar" size={16} />캘린더에 추가 (.ics 파일, {done.ics_events}건)</button>
                <span className="muted small">내려받은 파일을 열면 Google Calendar·Outlook·Apple 캘린더에 일정이 등록돼요.</span>
              </div>
            )}
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
