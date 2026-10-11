"""Review table and approval rules for the meeting page (spec 8.4), without Streamlit.

The page edits plain row dicts; these functions apply the edits, check the
approval conditions and turn rows back into domain objects. A person picking
an owner or a due date counts as confirming that field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Any

from .. import metrics
from ..store.base import StateStore
from .models import ActionItem, Decision, ExtractionResult, Meeting, new_id
from .roster import Roster
from .validate import is_weekend

OWNER_UNSET = "미확정"
EDITABLE = ("include", "task", "owner", "owner_ok", "due_date", "due_time", "due_ok")
CONTENT_FIELDS = ("task", "owner", "due_date", "due_time")  # counted for the "modified cells" metric
USER_ADDED_NOTE = "사용자가 직접 추가한 항목입니다."


def refresh_status(row: dict[str, Any]) -> dict[str, Any]:
    missing = [
        label
        for label, ok in (("담당", row["owner_ok"] and row["owner"] != OWNER_UNSET), ("기한", row["due_ok"] and row["due_date"]))
        if not ok
    ]
    parts = ["확정" if not missing else "미확정(" + "·".join(missing) + ")"]
    if row.get("needs_review"):
        parts.append("검토 필요")
    if is_weekend(row["due_date"]):
        parts.append("⚠ 주말")
    row["status"] = " · ".join(parts)
    return row


def row_from_item(item: ActionItem) -> dict[str, Any]:
    return refresh_status(
        {
            "include": True,
            "task": item.task,
            "owner": item.owner_name or OWNER_UNSET,
            "owner_ok": item.owner_status == "confirmed",
            "due_date": item.due_date,
            "due_time": item.due_time,
            "due_ok": item.due_status == "confirmed",
            "due_text": item.due_text or "",
            "status": "",
            "evidence": item.evidence_quote,
            "notes": " / ".join(item.review_notes),
            "item_id": item.item_id,
            "needs_review": item.needs_review,
        }
    )


def rows_from_result(result: ExtractionResult) -> list[dict[str, Any]]:
    return [row_from_item(item) for item in result.action_items]


def owner_options(roster: Roster, rows: list[dict[str, Any]]) -> list[str]:
    names = [m.name for m in roster.members]
    extra = [r["owner"] for r in rows if r["owner"] not in (OWNER_UNSET, *names)]
    return [OWNER_UNSET, *names, *dict.fromkeys(extra)]


def _coerce(column: str, value: Any) -> Any:
    """Editor deltas arrive JSON-encoded (dates and times as strings)."""
    if column in ("include", "owner_ok", "due_ok"):
        return bool(value)
    if column == "due_date":
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.date()
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if column == "due_time":
        if value in (None, ""):
            return None
        return value if isinstance(value, time) else time.fromisoformat(str(value))
    if column == "owner":
        return value or OWNER_UNSET
    return "" if value is None else str(value)


def apply_edits(
    rows: list[dict[str, Any]],
    edited_rows: dict[Any, dict[str, Any]],
    added_rows: list[dict[str, Any]],
    deleted_rows: list[int],
) -> list[dict[str, Any]]:
    """Apply one round of data_editor changes (positions refer to `rows`)."""
    rows = [dict(r) for r in rows]
    for position, changes in edited_rows.items():
        row = rows[int(position)]
        for column, value in changes.items():
            if column in EDITABLE:
                row[column] = _coerce(column, value)
        # A person choosing a value is the confirmation.
        if "owner" in changes and "owner_ok" not in changes:
            row["owner_ok"] = row["owner"] != OWNER_UNSET
        if ("due_date" in changes or "due_time" in changes) and "due_ok" not in changes:
            row["due_ok"] = row["due_date"] is not None
        if row["owner"] == OWNER_UNSET:
            row["owner_ok"] = False
        if row["due_date"] is None:
            row["due_ok"] = False
        refresh_status(row)
    for position in sorted((int(p) for p in deleted_rows), reverse=True):
        del rows[position]
    for added in added_rows:
        row = {
            "include": True,
            "task": "",
            "owner": OWNER_UNSET,
            "owner_ok": False,
            "due_date": None,
            "due_time": None,
            "due_ok": False,
            "due_text": "",
            "status": "",
            "evidence": "",
            "notes": USER_ADDED_NOTE,
            "item_id": new_id(),
            "needs_review": False,
        }
        for column, value in added.items():
            if column in EDITABLE:
                row[column] = _coerce(column, value)
        row["owner_ok"] = row["owner"] != OWNER_UNSET
        row["due_ok"] = row["due_date"] is not None
        rows.append(refresh_status(row))
    return rows


@dataclass
class ApprovalPlan:
    approve: list[dict[str, Any]] = field(default_factory=list)
    exclude: list[dict[str, Any]] = field(default_factory=list)  # included but unconfirmed -> draft
    problems: list[str] = field(default_factory=list)


def plan_approval(rows: list[dict[str, Any]], *, exclude_unconfirmed: bool) -> ApprovalPlan:
    """Included rows need a task, a confirmed owner and a confirmed due date."""
    plan = ApprovalPlan()
    for number, row in enumerate(rows, start=1):
        if not row["include"]:
            continue
        label = f"{number}행 '{row['task'][:20]}'"
        if not row["task"].strip():
            plan.problems.append(f"{number}행: 할 일이 비어 있습니다.")
            continue
        missing = [
            name
            for name, ok in (
                ("담당자", row["owner_ok"] and row["owner"] != OWNER_UNSET),
                ("기한", row["due_ok"] and row["due_date"] is not None),
            )
            if not ok
        ]
        if not missing:
            plan.approve.append(row)
        elif exclude_unconfirmed:
            plan.exclude.append(row)
        else:
            plan.problems.append(f"{label}: {'·'.join(missing)} 미확정")
    if not plan.approve and not plan.problems:
        plan.problems.append("승인할 항목이 없습니다. 담당자와 기한을 확정한 항목을 포함해 주세요.")
    return plan


def count_modified_cells(original: list[dict[str, Any]], final: list[dict[str, Any]]) -> tuple[int, int]:
    """(changed cells, editable cells of the original table) for the approval metric."""
    final_by_id = {r["item_id"]: r for r in final}
    original_ids = {r["item_id"] for r in original}
    changed = 0
    for row in original:
        after = final_by_id.get(row["item_id"])
        if after is None:
            changed += len(CONTENT_FIELDS)  # deleted row
            continue
        changed += sum(row[f] != after[f] for f in CONTENT_FIELDS)
    changed += len(CONTENT_FIELDS) * sum(1 for r in final if r["item_id"] not in original_ids)
    return changed, len(CONTENT_FIELDS) * len(original)


def _row_to_item(
    row: dict[str, Any],
    meeting_id: str,
    *,
    approved: bool,
    roster: Roster,
    original: ActionItem | None,
    now: datetime,
) -> ActionItem:
    owner = None if row["owner"] == OWNER_UNSET else row["owner"]
    owner_ok = bool(row["owner_ok"] and owner)
    due_ok = bool(row["due_ok"] and row["due_date"])
    member = roster.match(owner).member if owner else None
    slack_id = member.slack_user_id if member and member.name == owner else None
    return ActionItem(
        item_id=row["item_id"],
        meeting_id=meeting_id,
        task=row["task"].strip(),
        owner_name=owner,
        co_owners=original.co_owners if original else [],
        owner_slack_id=slack_id if owner_ok else None,
        owner_status="confirmed" if owner_ok else "unconfirmed",
        due_date=row["due_date"],
        due_time=row["due_time"] if row["due_date"] else None,
        due_text=row["due_text"] or None,
        due_status="confirmed" if due_ok else "unconfirmed",
        evidence_quote=row["evidence"],
        needs_review=bool(row.get("needs_review")),
        review_notes=original.review_notes if original else [USER_ADDED_NOTE],
        status="approved" if approved else "draft",
        approved_at=now if approved else None,
    )


def _decisions(lines: list[str], original: list[Decision]) -> list[Decision]:
    by_text = {d.text: d for d in original}
    return [
        by_text.get(line) or Decision(decision_id=new_id(), text=line, evidence_quote="")
        for line in (line.strip() for line in lines)
        if line
    ]


@dataclass
class Approval:
    plan: ApprovalPlan
    meeting: Meeting | None = None  # None while the plan has problems
    items: list[ActionItem] = field(default_factory=list)  # every row: approved or draft

    @property
    def approved_items(self) -> list[ActionItem]:
        return [i for i in self.items if i.status == "approved"]


def build_approval(
    result: ExtractionResult,
    rows: list[dict[str, Any]],
    *,
    summary: list[str],
    decisions: list[str],
    exclude_unconfirmed: bool,
    roster: Roster,
    now: datetime,
) -> Approval:
    """What approving would save, without saving it (also used for the preview)."""
    plan = plan_approval(rows, exclude_unconfirmed=exclude_unconfirmed)
    if plan.problems:
        return Approval(plan=plan)
    meeting = result.meeting.model_copy(
        update={
            "summary": [s.strip() for s in summary if s.strip()],
            "decisions": _decisions(decisions, result.meeting.decisions),
        }
    )
    originals = {item.item_id: item for item in result.action_items}
    approved_ids = {row["item_id"] for row in plan.approve}
    items = [
        _row_to_item(
            row,
            meeting.meeting_id,
            approved=row["item_id"] in approved_ids,
            roster=roster,
            original=originals.get(row["item_id"]),
            now=now,
        )
        for row in rows
    ]
    return Approval(plan=plan, meeting=meeting, items=items)


def approve_meeting(
    store: StateStore,
    result: ExtractionResult,
    rows: list[dict[str, Any]],
    *,
    summary: list[str],
    decisions: list[str],
    exclude_unconfirmed: bool,
    roster: Roster,
    now: datetime,
    log_metrics: bool = True,
) -> Approval:
    """Save the meeting and every row (approved or draft). Nothing is sent here."""
    approval = build_approval(
        result,
        rows,
        summary=summary,
        decisions=decisions,
        exclude_unconfirmed=exclude_unconfirmed,
        roster=roster,
        now=now,
    )
    if approval.meeting is None:
        return approval
    store.save_meeting(approval.meeting)
    store.upsert_items(approval.items)
    if log_metrics:
        changed, total = count_modified_cells(rows_from_result(result), rows)
        metrics.log_event(
            metrics.MEETING_APPROVED,
            approved=len(approval.plan.approve),
            excluded_unconfirmed=len(approval.plan.exclude),
            not_included=sum(1 for r in rows if not r["include"]),
            modified_cells=changed,
            modified_ratio=round(changed / total, 3) if total else 0.0,
        )
    return approval
