"""Evaluate regulation Q&A (instruction v2, section 10).

Run with:
    uv run python scripts/eval_regulations.py plan  [--set FILE]   # expected calls only
    uv run python scripts/eval_regulations.py dev   [--set FILE]   # retrieval metrics, no generation
    uv run python scripts/eval_regulations.py final [--set FILE]   # one run with generation (budgeted)
    uv run python scripts/eval_regulations.py rescore RUN.jsonl    # re-check saved outputs, no calls

Retrieval metrics (hit@1/3/5, MRR) are measured for vector, BM25 and RRF on
the same questions; question embeddings are cached, so a rerun makes no
embedding call. The final run generates in file order until its share of the
generation budget is used; questions held before generation (gate, period)
and questions left without budget are counted separately. Raw model outputs
are saved under data/regulations/eval/runs/ (git-ignored) for `rescore`.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.rag.answer import ConversationState
from onboarding_agent.rag.index import IndexUnavailable
from onboarding_agent.rag.models import ModelAnswer
from onboarding_agent.rag.service import build_runtime
from onboarding_agent.rag.usage import GENERATE_PLAN
from onboarding_agent.rag.validate import CHECK_NAMES, validate_answer

EVAL_DIR = REPO_ROOT / "data" / "regulations" / "eval"
RUNS = EVAL_DIR / "runs"
MODES = ("vector", "bm25", "rrf")


def load_set(path: Path) -> tuple[list[dict], bool]:
    text = path.read_text(encoding="utf-8")
    draft = "초안" in text.splitlines()[0] if text else True
    return yaml.safe_load(text) or [], draft


def expected_pairs(item: dict) -> list[tuple[str, str]]:
    return [(e["doc_title"], str(e["article"])) for e in item.get("expected_evidence") or []]


def rank_of(hits, expected: list[tuple[str, str]], titles: dict[str, str]) -> int | None:
    for position, hit in enumerate(hits, start=1):
        if (titles.get(hit.chunk.doc_id), hit.chunk.article_no) in expected:
            return position
    return None


def retrieval_metrics(ranks: list[int | None]) -> dict[str, float]:
    n = len(ranks)
    if not n:
        return {}
    return {
        "hit@1": sum(1 for r in ranks if r and r <= 1) / n,
        "hit@3": sum(1 for r in ranks if r and r <= 3) / n,
        "hit@5": sum(1 for r in ranks if r and r <= 5) / n,
        "MRR": sum(1 / r for r in ranks if r) / n,
    }


def run_retrieval(runtime, items: list[dict]) -> dict:
    titles = {d.doc_id: d.doc_title for d in runtime.index.docs.values()}
    rows = []
    for item in items:
        row = {"id": item["id"], "question": item["question"], "expected": expected_pairs(item), "modes": {}}
        for mode in MODES:
            result = runtime.retriever.search(item["question"], top_k=10, mode=mode, context=item.get("context") or [])
            row["modes"][mode] = {
                "rank": rank_of(result.hits, row["expected"], titles) if row["expected"] else None,
                "top": result.ids[:5],
                "top_vector_score": result.top_vector_score,
                "gated": result.gated,
                "degraded": result.degraded,
            }
        rows.append(row)
    scored = [r for r in rows if r["expected"]]
    metrics = {mode: retrieval_metrics([r["modes"][mode]["rank"] for r in scored]) for mode in MODES}
    return {"rows": rows, "metrics": metrics, "scored": len(scored)}


def suggest_threshold(rows: list[dict]) -> dict:
    inside = [r["modes"]["vector"]["top_vector_score"] for r in rows if r["expected"]]
    outside = [r["modes"]["vector"]["top_vector_score"] for r in rows if not r["expected"]]
    inside = [s for s in inside if s is not None]
    outside = [s for s in outside if s is not None]
    if not inside or not outside:
        return {"inside": inside, "outside": outside, "suggested": None}
    low_in, high_out = min(inside), max(outside)
    suggested = round((low_in + high_out) / 2, 3) if low_in > high_out else None
    return {
        "inside_min": round(low_in, 4),
        "outside_max": round(high_out, 4),
        "suggested": suggested,
        "separable": low_in > high_out,
    }


def print_metrics(report: dict, title: str) -> None:
    print(f"\n### {title} — 검색 지표 (기대 근거가 있는 {report['scored']}문항)")
    print("| 방식 | hit@1 | hit@3 | hit@5 | MRR |\n|---|---|---|---|---|")
    for mode in MODES:
        m = report["metrics"][mode]
        print(f"| {mode} | {m['hit@1']:.2f} | {m['hit@3']:.2f} | {m['hit@5']:.2f} | {m['MRR']:.3f} |")
    print(
        "\n| 문항 | 기대 근거 | vector 순위 | bm25 순위 | rrf 순위 | 최고 코사인 | rrf 상위 3 |"
        "\n|---|---|---|---|---|---|---|"
    )
    for r in report["rows"]:
        expected = ", ".join(f"{t} 제{a}조" for t, a in r["expected"]) or "(없음)"
        score = r["modes"]["vector"]["top_vector_score"]
        print(
            f"| {r['id']} | {expected} | {r['modes']['vector']['rank']} | {r['modes']['bm25']['rank']} | "
            f"{r['modes']['rrf']['rank']} | {score:.3f} | {', '.join(r['modes']['rrf']['top'][:3])} |"
            if score is not None
            else f"| {r['id']} | {expected} | - | - | - | - | - |"
        )


def cmd_plan(items: list[dict], runtime) -> None:
    uncached = 0
    for item in items:
        query = " ".join([*(item.get("context") or []), item["question"]]).strip()
        if runtime.embedder and runtime.embedder.cache.get(runtime.embedder.config.query_task, query) is None:
            uncached += 1
    used = runtime.meter.by_purpose("generate")
    totals = runtime.meter.totals()
    print(f"문항 {len(items)} · 새 질문 임베딩 예상 {uncached}회 (지금까지 {totals.get('embed_query', 0)}/30)")
    print(
        f"생성: final_eval 몫 {GENERATE_PLAN['final_eval'] - used.get('final_eval', 0)}회 남음, "
        f"재생성 몫 {GENERATE_PLAN['regenerate'] - used.get('regenerate', 0)}회 남음 "
        f"(전체 {totals.get('generate', 0)}/12)"
    )


def cmd_final(items: list[dict], draft: bool, runtime, set_path: Path) -> int:
    runtime.embedder.query_purpose = "query:final_eval"
    runtime.service.escalations = RUNS / "eval_escalations.jsonl"
    titles = {d.doc_id: d.doc_title for d in runtime.index.docs.values()}
    before = runtime.meter.totals()
    retrieval = run_retrieval(runtime, items)
    rows = []
    for item in items:
        state = ConversationState()
        for previous in item.get("context") or []:
            state.add(previous)
        used = runtime.meter.by_purpose("generate").get("final_eval", 0)
        search = runtime.retriever.search(
            item["question"],
            top_k=runtime.service.top_k,
            mode=runtime.service.mode,
            context=(item.get("context") or [])[-1:],
        )
        held = search.gated or runtime.service._period_unavailable(item["question"], search)
        if not held and used >= GENERATE_PLAN["final_eval"]:
            rows.append({"id": item["id"], "outcome": "not_run_budget", "expected_status": item["expected_status"]})
            continue
        start = time.monotonic()
        result = runtime.service.answer(item["question"], state, purpose="final_eval")
        answer = result.answer
        cited_pairs = {(titles.get(c.split(":")[0]), _article(c)) for c in result.cited_ids}
        expected = expected_pairs(item)
        rows.append(
            {
                "id": item["id"],
                "outcome": "held" if held else "generated",
                "expected_status": item["expected_status"],
                "status": answer.status,
                "status_ok": answer.status == item["expected_status"],
                "expected_evidence": expected,
                "evidence_ok": (
                    all(e in cited_pairs for e in expected) if expected and answer.status == "answered" else None
                ),
                "cited": result.cited_ids,
                "retrieved": [h.chunk.chunk_id for h in (result.retrieval.hits if result.retrieval else [])],
                "answer": answer.model_dump(mode="json"),
                "attempts": result.attempts,
                "warnings": result.warnings,
                "seconds": round(time.monotonic() - start, 2),
                "generate_requests": result.generate_requests,
            }
        )
    after = runtime.meter.totals()
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_path = RUNS / f"final_{stamp}.jsonl"
    run_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    summary = summarize(rows, retrieval, before, after, draft, set_path)
    (RUNS / f"final_{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_final(summary, rows, retrieval)
    print(f"\n원 출력: {run_path.relative_to(REPO_ROOT)}")
    return 0


def summarize(rows, retrieval, before, after, draft, set_path) -> dict:
    generated = [r for r in rows if r["outcome"] == "generated"]
    held = [r for r in rows if r["outcome"] == "held"]
    checks: dict[str, Counter] = {name: Counter() for name in CHECK_NAMES}
    for r in generated:
        for name, value in (r["answer"].get("checks") or {}).items():
            checks.setdefault(name, Counter())[value] += 1
    evidence = [r["evidence_ok"] for r in generated if r["evidence_ok"] is not None]
    return {
        "label": "사용자 검수 전 — 공식 수치 아님" if draft else "사용자 검수 완료 세트",
        "set": str(set_path.relative_to(REPO_ROOT)),
        "questions": len(rows),
        "generated": len(generated),
        "held_before_generation": len(held),
        "not_run_budget": sum(r["outcome"] == "not_run_budget" for r in rows),
        "status_accuracy_generated": _ratio([r["status_ok"] for r in generated]),
        "status_accuracy_held": _ratio([r["status_ok"] for r in held]),
        "evidence_match_answered": _ratio(evidence),
        "checks": {name: dict(counter) for name, counter in checks.items()},
        "retrieval_metrics": retrieval["metrics"],
        "seconds_total": round(sum(r.get("seconds", 0) for r in rows), 2),
        "requests": {
            "generate": after.get("generate", 0) - before.get("generate", 0),
            "embed_query": after.get("embed_query", 0) - before.get("embed_query", 0),
        },
    }


def print_final(summary: dict, rows: list[dict], retrieval: dict) -> None:
    print(f"## 최종 세트 — {summary['label']}")
    print(
        f"문항 {summary['questions']} · 생성 {summary['generated']} · 생성 전 보류 {summary['held_before_generation']} "
        f"· 예산으로 미실행 {summary['not_run_budget']}"
    )
    print(f"상태 정확도: 생성 {summary['status_accuracy_generated']} · 보류 {summary['status_accuracy_held']}")
    print(f"근거 일치(answered): {summary['evidence_match_answered']}")
    print(
        f"요청: 생성 {summary['requests']['generate']}회 · 질문 임베딩 {summary['requests']['embed_query']}회 "
        f"· 처리 시간 합 {summary['seconds_total']}초"
    )
    print("검사: " + json.dumps(summary["checks"], ensure_ascii=False))
    print("\n| 문항 | 처리 | 기대 상태 | 실제 상태 | 맞음 | 인용 근거 |\n|---|---|---|---|---|---|")
    for r in rows:
        print(
            f"| {r['id']} | {r['outcome']} | {r['expected_status']} | {r.get('status', '-')} | "
            f"{'O' if r.get('status_ok') else ('X' if 'status_ok' in r else '-')} | {', '.join(r.get('cited', []))} |"
        )
    print_metrics(retrieval, "최종 세트")


def cmd_rescore(path: Path, runtime) -> int:
    titles = {d.doc_id: d.doc_title for d in runtime.index.docs.values()}
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        for attempt in row.get("attempts", []):
            if "output" not in attempt:
                continue
            retrieved = {cid: runtime.index.chunk(cid) for cid in row.get("retrieved", []) if runtime.index.chunk(cid)}
            validation = validate_answer(ModelAnswer.model_validate(attempt["output"]), retrieved, titles)
            print(row["id"], attempt["attempt"], validation.checks, validation.failures[:2])
    return 0


def _article(chunk_id: str) -> str:
    part = chunk_id.split(":")[1]
    return part[1:].replace("-", "의")


def _ratio(values: list[bool]) -> str:
    return f"{sum(values)}/{len(values)}" if values else "0/0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="규정 Q&A 평가")
    parser.add_argument("command", choices=["plan", "dev", "final", "rescore"])
    parser.add_argument("run", nargs="?", type=Path)
    parser.add_argument("--set", type=Path)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    settings = get_settings()
    try:
        runtime = build_runtime(settings)
    except IndexUnavailable as exc:
        print(f"색인이 없어 평가할 수 없습니다: {exc}")
        return 1
    if args.command == "rescore":
        return cmd_rescore(args.run, runtime)
    set_path = args.set or EVAL_DIR / ("dev.yaml" if args.command == "dev" else "final.yaml")
    items, draft = load_set(set_path)
    if args.command == "plan":
        cmd_plan(items, runtime)
        return 0
    if runtime.embedder is None:
        print("GEMINI_API_KEY가 없어 벡터 검색을 평가할 수 없습니다.")
        return 1
    if args.command == "dev":
        runtime.embedder.query_purpose = "query:dev_eval"
        report = run_retrieval(runtime, items)
        report["threshold"] = suggest_threshold(report["rows"])
        RUNS.mkdir(parents=True, exist_ok=True)
        (RUNS / f"dev_{datetime.now():%Y%m%dT%H%M%S}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print_metrics(report, "개발 세트")
        print("\n최소 점수 제안: " + json.dumps(report["threshold"], ensure_ascii=False))
        print("질문 임베딩 누계: " + str(runtime.meter.totals().get("embed_query", 0)))
        return 0
    return cmd_final(items, draft, runtime, set_path)


if __name__ == "__main__":
    raise SystemExit(main())
