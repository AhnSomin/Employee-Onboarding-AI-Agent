"""Evaluate meeting extraction on the labeled sample minutes and print Markdown tables.

Run with:
    uv run python scripts/eval_meeting.py [--details] [--repeat N] [--force-fallback]
        [--compare-owner-rules] [--gold FILE --samples DIR] [--save-raw FILE] [--plan]
    uv run python scripts/eval_meeting.py --rescore FILE [--gold FILE --samples DIR]

Models come from GEMINI_MODEL_PRIMARY / GEMINI_MODEL_FALLBACKS (environment
first, then .env). Extraction events are not written to the metrics log.
Before calling the model it prints the expected number of Gemini calls;
--plan prints only that. --save-raw keeps every raw model output (JSONL) so
--rescore can score it again later with the current rules and labels and
without any model call. --compare-owner-rules also scores every extraction
with the institutional and weak-promise owner rules switched off, so their
effect is measured on the same model output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from onboarding_agent import config
from onboarding_agent.meeting.evaluation import GoldSample, SampleScore, load_gold, render_markdown, score_sample
from onboarding_agent.meeting.extract import extract_meeting, load_prompt
from onboarding_agent.meeting.loader import load_meeting
from onboarding_agent.meeting.models import ExtractionResult, LLMExtraction, Meeting
from onboarding_agent.meeting.owner_rules import OwnerRules
from onboarding_agent.meeting.roster import load_roster
from onboarding_agent.meeting.tools import MAX_TURNS
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


def call_estimate(samples: int, repeat: int, models: int) -> str:
    """Expected Gemini calls: one per tool-loop turn (usually 1-2), at most MAX_TURNS + 1 structured call."""
    runs = samples * repeat
    return (
        f"예상 Gemini 호출: 보통 {runs}~{runs * 2}회, 최대 {runs * (MAX_TURNS + 1)}회 "
        f"(샘플 {samples} × 반복 {repeat}; 오류 재시도와 대체 모델 {max(models - 1, 0)}개 호출은 별도)"
    )


def rescored_result(saved: dict, gold: GoldSample, text: str, roster, owner_rules: OwnerRules | None = None):
    """ExtractionResult for a saved raw output, validated with the current rules. No model call."""
    extraction = LLMExtraction.model_validate(saved["extraction"])
    validated = validate_extraction(
        extraction,
        meeting_text=text,
        meeting_date=gold.meeting_date,
        meeting_id="rescore",
        roster=roster,
        from_rules=saved.get("path") == "rule_based",
        owner_rules=owner_rules,
    )
    meeting = Meeting(
        meeting_id="rescore",
        title=extraction.title_suggestion or gold.sample,
        meeting_date=gold.meeting_date,
        summary=validated.summary,
        decisions=validated.decisions,
        created_at=datetime.now(config.get_settings().tz),
    )
    return ExtractionResult(
        meeting=meeting,
        action_items=validated.items,
        extraction_path=saved.get("path") or "function_calling",
        model_used=saved.get("model"),
        fallback_used=saved.get("path") != "function_calling",
        injection_sentences=validated.injection_sentences,
        injection_blocked=validated.dropped_by_injection,
        warnings=validated.warnings,
    )


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
    parser.add_argument("--save-raw", type=Path, help="write every raw model output to this JSONL file")
    parser.add_argument("--rescore", type=Path, help="score raw outputs saved with --save-raw (no model calls)")
    parser.add_argument("--plan", action="store_true", help="print the expected Gemini calls and stop")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    settings = config.get_settings()
    roster = load_roster(settings.roster_path)
    golds = load_gold(args.gold)

    if args.rescore:
        return rescore(args, golds, roster)
    calls = "예상 Gemini 호출: 0회 (규칙 기반 강제)" if args.force_fallback else call_estimate(
        len(golds), args.repeat, len(settings.gemini_models)
    )
    print(calls, file=sys.stderr)
    if args.plan:
        return 0

    saved: list[dict] = []
    runs: list[list[tuple[GoldSample, SampleScore, SampleScore | None]]] = [[] for _ in range(args.repeat)]
    for gold in golds:
        loaded = load_meeting(data=(args.samples / gold.sample).read_bytes(), filename=gold.sample)
        for number, run in enumerate(runs, start=1):
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
            saved.append(
                {
                    "sample": gold.sample,
                    "run": number,
                    "model": result.model_used,
                    "path": result.extraction_path,
                    "turns": result.tool_calls.turns if result.tool_calls else None,
                    "latency_ms": latency,
                    "prompt": load_prompt().version,
                    "extraction": raw[0].model_dump(mode="json"),
                }
            )
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

    if args.save_raw:
        args.save_raw.parent.mkdir(parents=True, exist_ok=True)
        args.save_raw.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in saved), encoding="utf-8")
    chain = ", ".join(settings.gemini_models) or "(모델 미지정)"
    mode = "규칙 기반 강제" if args.force_fallback else f"모델 체인: {chain}"
    print(f"### 회의록 추출 평가 — {mode} · 프롬프트 {load_prompt().version} · 반복 {args.repeat}회\n")
    print(render_markdown([score for run in runs for _, score, _ in run], details=args.details))
    print("\n### 실행별 거짓 확정\n")
    print(render_runs(runs))
    return 0


def rescore(args, golds: list[GoldSample], roster) -> int:
    """Score saved raw outputs with the current rules and labels. Calls no model."""
    by_sample = {g.sample: g for g in golds}
    rows = [json.loads(line) for line in args.rescore.read_text(encoding="utf-8").splitlines() if line.strip()]
    run_numbers = sorted({row.get("run", 1) for row in rows})
    runs: list[list[tuple[GoldSample, SampleScore, SampleScore | None]]] = [[] for _ in run_numbers]
    texts: dict[str, str] = {}
    for row in rows:
        gold = by_sample.get(row["sample"])
        if gold is None:
            continue
        if gold.sample not in texts:
            texts[gold.sample] = load_meeting(
                data=(args.samples / gold.sample).read_bytes(), filename=gold.sample
            ).text
        result = rescored_result(row, gold, texts[gold.sample], roster)
        baseline = None
        if args.compare_owner_rules:
            without = rescored_result(row, gold, texts[gold.sample], roster, owner_rules=OwnerRules())
            baseline = score_sample(gold, without, row.get("latency_ms", 0))
        runs[run_numbers.index(row.get("run", 1))].append(
            (gold, score_sample(gold, result, row.get("latency_ms", 0)), baseline)
        )
    print(f"### 저장된 출력 다시 채점 — {args.rescore.name} · 모델 호출 0회 · 실행 {len(runs)}회\n")
    print(render_markdown([score for run in runs for _, score, _ in run], details=args.details))
    print("\n### 실행별 거짓 확정\n")
    print(render_runs(runs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
