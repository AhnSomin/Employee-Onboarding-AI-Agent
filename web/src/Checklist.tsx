import { useState } from "react";
import { api, Me, Task } from "./api";
import Icon from "./Icons";

const today = () => new Date().toISOString().slice(0, 10);

function dueLabel(t: Task): { text: string; late: boolean } | null {
  if (!t.due_date) return null;
  const d = t.due_date;
  const diff = Math.round((Date.parse(d) - Date.parse(today())) / 86400000);
  const md = `${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}`;
  if (t.done) return { text: md, late: false };
  if (diff < 0) return { text: `${md} · ${-diff}일 지남`, late: true };
  if (diff === 0) return { text: `${md} · 오늘`, late: false };
  if (diff <= 3) return { text: `${md} · D-${diff}`, late: false };
  return { text: md, late: false };
}

export default function Checklist({ me, onChange }: { me: Me; onChange: () => void }) {
  const [title, setTitle] = useState("");
  const [due, setDue] = useState("");
  const [err, setErr] = useState("");
  const pct = me.total ? Math.round((me.done / me.total) * 100) : 0;

  // 안 한 일을 기한 순으로 위에, 끝낸 일은 아래에
  const tasks = [...me.tasks].sort((a, b) =>
    a.done - b.done || (a.due_date ?? "9999").localeCompare(b.due_date ?? "9999") || a.task_id.localeCompare(b.task_id));

  async function toggle(t: Task) {
    await api.todoDone(t.task_id, !t.done).catch((e) => setErr((e as Error).message));
    onChange();
  }
  async function add(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setErr("");
    try { await api.addTodo(title.trim(), due || null); setTitle(""); setDue(""); onChange(); }
    catch (e2) { setErr((e2 as Error).message); }
  }
  async function remove(t: Task) {
    await api.deleteTodo(t.task_id).catch((e) => setErr((e as Error).message));
    onChange();
  }

  return (
    <div className="todo">
      <div className="todo-head">
        <strong>할 일</strong>
        <span className="muted small">{me.done}/{me.total} 완료</span>
      </div>
      <div className="bar" aria-label={`진행률 ${pct}%`}><i style={{ width: `${pct}%` }} /></div>

      <ul className="todo-list">
        {tasks.map((t) => {
          const d = dueLabel(t);
          return (
            <li key={t.task_id} className={t.done ? "done" : ""}>
              <button className={`box ${t.done ? "on" : ""}`} onClick={() => toggle(t)}
                aria-label={t.done ? "완료 취소" : "완료로 표시"} aria-pressed={!!t.done}>{t.done && <Icon name="check" size={13} />}</button>
              <span className="todo-title">
                {t.title}
                {d && <small className={d.late ? "late" : ""}>{d.text}</small>}
              </span>
              {t.custom && <button className="x" onClick={() => remove(t)} aria-label="삭제" title="삭제"><Icon name="x" size={14} /></button>}
            </li>
          );
        })}
      </ul>

      <form className="todo-add" onSubmit={add}>
        <input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="할 일 추가" maxLength={120} />
        <input type="date" value={due} onChange={(e) => setDue(e.target.value)} aria-label="기한" />
        <button disabled={!title.trim()}>추가</button>
      </form>
      {err && <p className="error small">{err}</p>}
    </div>
  );
}
