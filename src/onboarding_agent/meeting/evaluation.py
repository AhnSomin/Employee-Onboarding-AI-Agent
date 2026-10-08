"""Extraction evaluation against hand-checked labels (spec 14).

A predicted item matches a gold item when every gold keyword appears in the
item's task or evidence quote. Owners and due dates count only when the
system confirmed them; an unconfirmed field reads as "미확정". The false
confirmation rate is the share of gold "미확정" fields the system confirmed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .models import ActionItem, ExtractionResult
from .roster import compact


@dataclass(frozen=True)
class GoldItem:
    id: str
    keywords: tuple[str, ...]
    owner: str | None
    due: str | None  # "YYYY-MM-DD" or "YYYY-MM-DD HH:MM"
    optional: bool = False  # may be extracted or not; never counted against precision


@dataclass(frozen=True)
class GoldSample:
    sample: str
    meeting_date: date
    items: tuple[GoldItem, ...]
    must_not_extract: tuple[str, ...] = ()


@dataclass
class SampleScore:
    sample: str
    path: str
    model: str | None
    turns: int | None
    gold_required: int
    predicted: int
    matched_required: int
    matched_predictions: int
    owner_correct: int
    due_correct: int
    false_confirms: int
    gold_unconfirmed_fields: int
    injection_blocked: int  # proposals the model made from AI-directed lines, dropped by code
    injection_leaks: int  # such content left in the final result
    latency_ms: int
    details: list[dict] = field(default_factory=list)


def load_gold(path: Path) -> list[GoldSample]:
    samples = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        samples.append(
            GoldSample(
                sample=raw["sample"],
                meeting_date=date.fromisoformat(raw["meeting_date"]),
                items=tuple(
                    GoldItem(
                        id=i["id"],
                        keywords=tuple(i["keywords"]),
                        owner=i.get("owner"),
                        due=i.get("due"),
                        optional=i.get("optional", False),
                    )
                    for i in raw["items"]
                ),
                must_not_extract=tuple(raw.get("must_not_extract", [])),
            )
        )
    return samples


def predicted_owner(item: ActionItem) -> str | None:
    return item.owner_name if item.owner_status == "confirmed" else None


def predicted_due(item: ActionItem) -> str | None:
    if item.due_status != "confirmed" or item.due_date is None:
        return None
    text = item.due_date.isoformat()
    return f"{text} {item.due_time:%H:%M}" if item.due_time else text


def _matches(gold: GoldItem, item: ActionItem) -> bool:
    haystack = compact(item.task + " " + item.evidence_quote)
    return all(compact(keyword) in haystack for keyword in gold.keywords)


def match_items(
    gold_items: tuple[GoldItem, ...], predicted: list[ActionItem]
) -> list[tuple[GoldItem, ActionItem]]:
    """One-to-one matching; required labels pick first, in label order."""
    pairs: list[tuple[GoldItem, ActionItem]] = []
    used: set[int] = set()
    ordered = [g for g in gold_items if not g.optional] + [g for g in gold_items if g.optional]
    for gold in ordered:
        for index, item in enumerate(predicted):
            if index not in used and _matches(gold, item):
                pairs.append((gold, item))
                used.add(index)
                break
    return pairs


def score_sample(gold: GoldSample, result: ExtractionResult, latency_ms: int) -> SampleScore:
    pairs = match_items(gold.items, result.action_items)
    matched = {g.id for g, _ in pairs}
    score = SampleScore(
        sample=gold.sample,
        path=result.extraction_path,
        model=result.model_used,
        turns=result.tool_calls.turns if result.tool_calls else None,
        gold_required=sum(1 for g in gold.items if not g.optional),
        predicted=len(result.action_items),
        matched_required=sum(1 for g, _ in pairs if not g.optional),
        matched_predictions=len(pairs),
        owner_correct=0,
        due_correct=0,
        false_confirms=0,
        gold_unconfirmed_fields=0,
        injection_blocked=result.injection_blocked,
        injection_leaks=0,
        latency_ms=latency_ms,
    )
    for gold_item, item in pairs:
        owner, due = predicted_owner(item), predicted_due(item)
        owner_ok, due_ok = owner == gold_item.owner, due == gold_item.due
        if not gold_item.optional:
            score.owner_correct += owner_ok
            score.due_correct += due_ok
        for expected, actual in ((gold_item.owner, owner), (gold_item.due, due)):
            if expected is None:
                score.gold_unconfirmed_fields += 1
                score.false_confirms += actual is not None
        score.details.append(
            {
                "id": gold_item.id,
                "matched": True,
                "owner": owner or "미확정",
                "owner_ok": owner_ok,
                "due": due or "미확정",
                "due_ok": due_ok,
            }
        )
    for gold_item in gold.items:
        if gold_item.id not in matched and not gold_item.optional:
            score.details.append({"id": gold_item.id, "matched": False})

    texts = [i.task + " " + i.evidence_quote for i in result.action_items]
    texts += [d.text + " " + d.evidence_quote for d in result.meeting.decisions]
    score.injection_leaks = sum(
        1 for text in texts for phrase in gold.must_not_extract if compact(phrase) in compact(text)
    )
    return score


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "해당 없음"
    return f"{numerator}/{denominator} ({numerator / denominator:.0%})"


def render_markdown(scores: list[SampleScore], *, details: bool = False) -> str:
    lines = [
        "| 샘플 | 경로 | 모델 | 턴 | 정답 | 추출 | 매칭 | 담당자 정확 | 기한 정확 | 거짓 확정 | 지연(ms) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in scores:
        lines.append(
            f"| {s.sample} | {s.path} | {s.model or '-'} | {s.turns if s.turns is not None else '-'} "
            f"| {s.gold_required} | {s.predicted} | {s.matched_required} "
            f"| {s.owner_correct}/{s.matched_required} | {s.due_correct}/{s.matched_required} "
            f"| {s.false_confirms}/{s.gold_unconfirmed_fields} | {s.latency_ms:,} |"
        )

    def total(attr: str) -> int:
        return sum(getattr(s, attr) for s in scores)

    avg_latency = round(total("latency_ms") / len(scores)) if scores else 0
    lines += [
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 항목 재현율 | {_ratio(total('matched_required'), total('gold_required'))} |",
        f"| 항목 정밀도 | {_ratio(total('matched_predictions'), total('predicted'))} |",
        f"| 담당자 정확도 | {_ratio(total('owner_correct'), total('matched_required'))} |",
        f"| 기한 정확도 | {_ratio(total('due_correct'), total('matched_required'))} |",
        f"| **거짓 확정률** (목표 0) | **{_ratio(total('false_confirms'), total('gold_unconfirmed_fields'))}** |",
        f"| 평균 추출 지연 | {avg_latency:,} ms |",
        f"| 인젝션 문장을 모델이 제안한 횟수 (코드가 차단) | {total('injection_blocked')}건 |",
        f"| 인젝션 문장 유출 (최종 결과) | {total('injection_leaks')}건 |",
    ]
    if details:
        lines += ["", "| 라벨 | 매칭 | 담당자(예측) | 맞음 | 기한(예측) | 맞음 |", "|---|---|---|---|---|---|"]
        for s in scores:
            for d in sorted(s.details, key=lambda d: d["id"]):
                if d["matched"]:
                    lines.append(
                        f"| {d['id']} | O | {d['owner']} | {'O' if d['owner_ok'] else 'X'} "
                        f"| {d['due']} | {'O' if d['due_ok'] else 'X'} |"
                    )
                else:
                    lines.append(f"| {d['id']} | X | - | - | - | - |")
    return "\n".join(lines)
