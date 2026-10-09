"""Pending inquiries for a person in charge (section 7.6). Drafts only: nothing is sent.

The business StateStore has no escalation table and its schema is not
changed, so drafts go to a git-ignored JSONL file.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

PENDING_NOTICE = "담당자 확인이 필요합니다. 아직 전달되지 않았습니다."


def save_escalation(path: Path, *, question: str, conditions: str, candidates: list[str], reason: str) -> str:
    record = {
        "escalation_id": uuid.uuid4().hex[:12],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "question": question,
        "conditions": conditions,
        "candidate_chunk_ids": candidates,
        "reason": reason,
        "status": "pending",  # never delivered by the app
        "delivered": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record["escalation_id"]


def load_escalations(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
