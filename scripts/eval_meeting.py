"""Evaluate meeting extraction on the labeled sample minutes and print Markdown tables.

Run with:
    uv run python scripts/eval_meeting.py [--details] [--repeat N] [--force-fallback]
        [--compare-owner-rules] [--gold FILE --samples DIR]

Models come from GEMINI_MODEL_PRIMARY / GEMINI_MODEL_FALLBACKS (environment
first, then .env). Extraction events are not written to the metrics log.
--compare-owner-rules also scores every extraction with the institutional and
weak-promise owner rules switched off, so their effect is measured on the same
model output.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from onboarding_agent import config
from onboarding_agent.meeting.evaluation import GoldSample, SampleScore, load_gold, render_markdown, score_sample
from onboarding_agent.meeting.extract import extract_meeting, load_prompt
from onboarding_agent.meeting.loader import load_meeting
from onboarding_agent.meeting.models import LLMExtraction
from onboarding_agent.meeting.owner_rules import OwnerRules
from onboarding_agent.meeting.roster import load_roster
from onboarding_agent.meeting.validate import validate_extraction

DEFAULT_GOLD = config.REPO_ROOT / "data" / "eval" / "meeting_gold.jsonl"
DEFAULT_SAMPLES = config.REPO_ROOT / "data" / "samples"


def false_confirms(gold: GoldSample, score: SampleScore) -> list[str]:
    """'a2 담당자=…' for every field the label leaves unconfirmed but the system confirmed."""
    labels = {g.id: g for g in gold.items}
    found = []
    for detail in score.details:
        if not detail["matched"]:
            continue
        label = labels[detail["id"]]
        if label.owner is None and detail["owner"] != "미확정":
            found.append(f"{detail['id']} 담당자={detail['owner']}")
        if label.due is None and detail["due"] != "미확정":
            found.append(f"{detail['id']} 기한={detail['due']}")
    return found


def owner_changes(gold: GoldSample, score: SampleScore, baseline: SampleScore) -> list[str]:
    """'s2-3 이서연→미확정' for every label whose owner the owner rules changed."""
    before = {d["id"]: d["owner"] for d in baseline.details if d["matched"]}
    return [
        f"{d['id']} {before[d['id']]}→{d['owner']}"
        for d in score.details
        if d["matched"] and d["id"] in before and before[d["id"]] != d["owner"]
    ]


def render_runs(runs: list[list[tuple[GoldSample, SampleScore, SampleScore | None]]]) -> str:
    compare = any(baseline for run in runs for _, _, baseline in run)
    head = "| 실행 | 필수 라벨 매칭 | 거짓 확정 | 거짓 확정 필드 |"
    rule = "|---|---|---|---|"
    if compare:
        head += " 규칙 끔(같은 출력) | 규칙 끔 필드 | 규칙이 바꾼 담당자 |"
        rule += "---|---|---|"
    lines = [head, rule]
    counts, baseline_counts = [], []
    for number, run in enumerate(runs, start=1):
        matched = sum(s.matched_required for _, s, _ in run)
        required = sum(s.gold_required for _, s, _ in run)
        fields = [f"{g.sample.removesuffix('.txt')} {f}" for g, s, _ in run for f in false_confirms(g, s)]
        total = sum(s.gold_unconfirmed_fields for _, s, _ in run)
        counts.append(len(fields))
        row = f"| {number} | {matched}/{required} | {len(fields)}/{total} | {', '.join(fields) or '-'} |"
        if compare:
            base = [f"{g.sample.removesuffix('.txt')} {f}" for g, _, b in run if b for f in false_confirms(g, b)]
            base_total = sum(b.gold_unconfirmed_fields for _, _, b in run if b)
            baseline_counts.append(len(base))
            changed = [f"{g.sample.removesuffix('.txt')} {c}" for g, s, b in run if b for c in owner_changes(g, s, b)]
            row += f" {len(base)}/{base_total} | {', '.join(base) or '-'} | {', '.join(changed) or '-'} |"
        lines.append(row)
    lines += ["", f"- 실행 간 거짓 확정 수: {min(counts)}~{max(counts)} (폭 {max(counts) - min(counts)})"]
    if compare:
        spread = max(baseline_counts) - min(baseline_counts)
        lines.append(f"- 규칙을 끈 경우(같은 출력): {min(baseline_counts)}~{max(baseline_counts)} (폭 {spread})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gold", type=Path, default=DEFAULT_GOLD)
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--repeat", type=int, default=1, help="run every sample N times")
    parser.add_argument("--details", action="store_true", help="per-label table")
    parser.add_argument("--force-fallback", action="store_true", help="rule-based only")
    parser.add_argument(
        "--compare-owner-rules", action="store_true", help="also score the same output with the owner rules off"
    )
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    settings = config.get_settings()
    roster = load_roster(settings.roster_path)

    runs: list[list[tuple[GoldSample, SampleScore, SampleScore | None]]] = [[] for _ in range(args.repeat)]
    for gold in load_gold(args.gold):
        loaded = load_meeting(data=(args.samples / gold.sample).read_bytes(), filename=gold.sample)
        for run in runs:
            raw: list[LLMExtraction] = []
            started = time.perf_counter()
            result = extract_meeting(
                loaded.text,
                title=loaded.title_suggestion,
                meeting_date=gold.meeting_date,
                source_filename=gold.sample,
                roster=roster,
                force_fallback=True if args.force_fallback else None,
                log_metrics=False,
                raw_extractions=raw,
            )
            latency = round((time.perf_counter() - started) * 1000)
            baseline = None
            if args.compare_owner_rules:
                without = validate_extraction(
                    raw[0],
                    meeting_text=loaded.text,
                    meeting_date=gold.meeting_date,
                    meeting_id=result.meeting.meeting_id,
                    roster=roster,
                    from_rules=result.extraction_path == "rule_based",
                    owner_rules=OwnerRules(),
                )
                baseline = score_sample(gold, result.model_copy(update={"action_items": without.items}), latency)
            run.append((gold, score_sample(gold, result, latency), baseline))

    chain = ", ".join(settings.gemini_models) or "(모델 미지정)"
    mode = "규칙 기반 강제" if args.force_fallback else f"모델 체인: {chain}"
    print(f"### 회의록 추출 평가 — {mode} · 프롬프트 {load_prompt().version} · 반복 {args.repeat}회\n")
    print(render_markdown([score for run in runs for _, score, _ in run], details=args.details))
    print("\n### 실행별 거짓 확정\n")
    print(render_runs(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
