"""Evaluate regulation Q&A (instruction v2, section 10).

Run with:
    uv run python scripts/eval_regulations.py plan  [--set FILE]   # expected calls only
    uv run python scripts/eval_regulations.py dev   [--set FILE]   # retrieval metrics, no generation
    uv run python scripts/eval_regulations.py final --save-raw [--set FILE]  # one run with generation (budgeted)
    uv run python scripts/eval_regulations.py final --rescore RUN.jsonl      # rescore saved outputs, no calls

Retrieval metrics (hit@1/3/5, MRR) are measured for vector, BM25 and RRF on
the same questions; question embeddings are cached, so a rerun makes no
embedding call. The final run generates in file order until its share of the
generation budget is used; questions held before generation (gate, period)
and questions left without budget are counted separately. With --save-raw the
raw model outputs are saved under data/regulations/eval/runs/ (git-ignored);
`--rescore` scores them again against the current set file (expected status
and evidence, by question id) and reruns the deterministic checks, with no
model or embedding call.
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


def evidence_key(chunk, titles: dict[str, str]) -> tuple[str | None, str | None]:
    """(law title, article number) — or (law title, "별표 n") for an annex chunk."""
    if chunk.kind == "annex" and chunk.heading:
        return titles.get(chunk.doc_id), chunk.heading.split("]")[0].strip("[")
    return titles.get(chunk.doc_id), chunk.article_no


def evidence_label(title: str, article: str) -> str:
    return f"{title} {article}" if article.startswith("별표") else f"{title} 제{article}조"


def rank_of(hits, expected: list[tuple[str, str]], titles: dict[str, str]) -> int | None:
    for position, hit in enumerate(hits, start=1):
        if evidence_key(hit.chunk, titles) in expected:
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
        row = {
            "id": item["id"],
            "question": item["question"],
            "expected": expected_pairs(item),
            "scope": scope_of(item),
            "modes": {},
        }
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
    in_scope = [r for r in scored if r["scope"] == "in"]
    metrics_in = {mode: retrieval_metrics([r["modes"][mode]["rank"] for r in in_scope]) for mode in MODES}
    return {
        "rows": rows,
        "metrics": metrics,
        "scored": len(scored),
        "metrics_in": metrics_in,
        "scored_in": len(in_scope),
    }


def scope_of(item_or_row: dict) -> str:
    """in | out | boundary. Without a `scope` field, a question with expected evidence is "in"."""
    return item_or_row.get("scope") or (
        "in" if item_or_row.get("expected") or item_or_row.get("expected_evidence") else "out"
    )


def suggest_threshold(rows: list[dict]) -> dict:
    """Highest vector score per question, split by scope; the suggestion is the middle of the gap
    between the lowest in-scope score and the highest out-of-scope score. Boundary questions are
    partly covered, so they are reported against the suggestion but do not set it."""
    scores: dict[str, list[tuple[str, float]]] = {"in": [], "out": [], "boundary": []}
    for r in rows:
        score = r["modes"]["vector"]["top_vector_score"]
        if score is not None:
            scores[r["scope"]].append((r["id"], score))
    if not scores["in"] or not scores["out"]:
        return {"suggested": None, **{k: v for k, v in scores.items()}}
    low_in = min(scores["in"], key=lambda x: x[1])
    high_out = max(scores["out"], key=lambda x: x[1])
    separable = low_in[1] > high_out[1]
    suggested = round((low_in[1] + high_out[1]) / 2, 3) if separable else None
    report = {
        "inside_min": [low_in[0], round(low_in[1], 4)],
        "outside_max": [high_out[0], round(high_out[1], 4)],
        "gap": round(low_in[1] - high_out[1], 4),
        "separable": separable,
        "suggested": suggested,
        "in": len(scores["in"]),
        "out": len(scores["out"]),
    }
    if scores["boundary"]:
        low_b = min(scores["boundary"], key=lambda x: x[1])
        report["boundary_min"] = [low_b[0], round(low_b[1], 4)]
        report["boundary_held_at_suggested"] = (
            [i for i, s in scores["boundary"] if s < suggested] if suggested is not None else None
        )
    return report


def print_metrics(report: dict, title: str) -> None:
    print(f"\n### {title} — 검색 지표 (기대 근거가 있는 {report['scored']}문항)")
    print("| 방식 | hit@1 | hit@3 | hit@5 | MRR |\n|---|---|---|---|---|")
    for mode in MODES:
        m = report["metrics"][mode]
        print(f"| {mode} | {m['hit@1']:.2f} | {m['hit@3']:.2f} | {m['hit@5']:.2f} | {m['MRR']:.3f} |")
    if report.get("scored_in") and report["scored_in"] != report["scored"]:
        print(f"\n범위 안(경계 제외) {report['scored_in']}문항:")
        print("| 방식 | hit@1 | hit@3 | hit@5 | MRR |\n|---|---|---|---|---|")
        for mode in MODES:
            m = report["metrics_in"][mode]
            print(f"| {mode} | {m['hit@1']:.2f} | {m['hit@3']:.2f} | {m['hit@5']:.2f} | {m['MRR']:.3f} |")
    print(
        "\n| 문항 | 범위 | 기대 근거 | vector 순위 | bm25 순위 | rrf 순위 | 최고 코사인 | 보류 | rrf 상위 3 |"
        "\n|---|---|---|---|---|---|---|---|---|"
    )
    for r in report["rows"]:
        expected = ", ".join(evidence_label(t, a) for t, a in r["expected"]) or "(없음)"
        score = r["modes"]["vector"]["top_vector_score"]
        held = "예" if r["modes"]["vector"]["gated"] else ""
        print(
            f"| {r['id']} | {r.get('scope', '-')} | {expected} | {r['modes']['vector']['rank']} | "
            f"{r['modes']['bm25']['rank']} | {r['modes']['rrf']['rank']} | {score:.3f} | {held} | "
            f"{', '.join(r['modes']['rrf']['top'][:3])} |"
            if score is not None
            else f"| {r['id']} | {r.get('scope', '-')} | {expected} | - | - | - | - | {held} | - |"
        )


def cmd_plan(items: list[dict], runtime) -> None:
    uncached = 0
    for item in items:
        query = " ".join([*(item.get("context") or []), item["question"]]).strip()
        if runtime.embedder and runtime.embedder.cache.get(runtime.embedder.config.query_task, query) is None:
            uncached += 1
    meter = runtime.meter
    used = meter.by_purpose("generate")
    totals = meter.totals()
    print(
        f"문항 {len(items)} · 새 질문 임베딩 예상 {uncached}회 "
        f"(이번 예산 {totals.get('embed_query', 0)}/{meter.caps['embed_query']})"
    )
    print(
        f"생성: final_eval 몫 {meter.plan.get('final_eval', 0) - used.get('final_eval', 0)}회 남음, "
        f"재생성 몫 {meter.plan.get('regenerate', 0) - used.get('regenerate', 0)}회 남음 "
        f"(이번 예산 {totals.get('generate', 0)}/{meter.caps['generate']})"
    )


def cmd_final(items: list[dict], draft: bool, runtime, set_path: Path, save_raw: bool) -> int:
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
        if not held and used >= runtime.meter.plan.get("final_eval", 0):
            rows.append({"id": item["id"], "outcome": "not_run_budget", "expected_status": item["expected_status"]})
            continue
        start = time.monotonic()
        result = runtime.service.answer(item["question"], state, purpose="final_eval")
        answer = result.answer
        cited_keys = [
            list(evidence_key(runtime.index.chunk(c), titles)) for c in result.cited_ids if runtime.index.chunk(c)
        ]
        rows.append(
            score_row(
                {
                    "id": item["id"],
                    "question": item["question"],
                    "outcome": "held" if held else "generated",
                    "status": answer.status,
                    "cited": result.cited_ids,
                    "cited_keys": cited_keys,
                    "retrieved": [h.chunk.chunk_id for h in (result.retrieval.hits if result.retrieval else [])],
                    "answer": answer.model_dump(mode="json"),
                    "attempts": result.attempts,
                    "warnings": result.warnings,
                    "seconds": round(time.monotonic() - start, 2),
                    "generate_requests": result.generate_requests,
                },
                item,
            )
        )
    after = runtime.meter.totals()
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    summary = summarize(rows, retrieval["metrics"], before, after, draft, set_path)
    (RUNS / f"final_{stamp}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_final(summary, rows)
    print_metrics(retrieval, "최종 세트")
    if save_raw:
        run_path = RUNS / f"final_{stamp}.jsonl"
        run_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
        print(f"\n원 출력: {run_path.relative_to(REPO_ROOT)} (다시 채점: final --rescore 이 파일)")
    else:
        print("\n--save-raw 없이 실행해 원 출력을 남기지 않았습니다. 다시 채점할 수 없습니다.")
    return 0


def score_row(row: dict, item: dict) -> dict:
    """Score one saved row against its set item: expected status, and expected evidence when answered."""
    expected = expected_pairs(item)
    cited = {tuple(k) for k in row.get("cited_keys", [])}
    row["expected_status"] = item["expected_status"]
    row["expected_evidence"] = expected
    if "status" in row:
        row["status_ok"] = row["status"] == item["expected_status"]
        row["evidence_ok"] = all(e in cited for e in expected) if expected and row["status"] == "answered" else None
    return row


def summarize(rows, retrieval_metrics_by_mode, before, after, draft, set_path) -> dict:
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
        "retrieval_metrics": retrieval_metrics_by_mode,
        "seconds_total": round(sum(r.get("seconds", 0) for r in rows), 2),
        "requests": {
            "generate": after.get("generate", 0) - before.get("generate", 0),
            "embed_query": after.get("embed_query", 0) - before.get("embed_query", 0),
        },
    }


def print_final(summary: dict, rows: list[dict]) -> None:
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


def cmd_rescore(path: Path, items: list[dict], draft: bool, runtime, set_path: Path) -> int:
    """Score saved outputs against the current set file and rerun the checks. No model or embedding call."""
    titles = {d.doc_id: d.doc_title for d in runtime.index.docs.values()}
    by_id = {item["id"]: item for item in items}
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row["id"] not in by_id:
            print(f"{row['id']}: 현재 세트에 없는 문항이라 건너뜀")
            continue
        if by_id[row["id"]]["question"] != row.get("question", by_id[row["id"]]["question"]):
            print(f"{row['id']}: 질문이 바뀌어 다시 실행해야 합니다(채점에서 뺌)")
            continue
        rows.append(score_row(row, by_id[row["id"]]))
        for attempt in row.get("attempts", []):
            if "output" not in attempt:
                continue
            retrieved = {cid: runtime.index.chunk(cid) for cid in row.get("retrieved", []) if runtime.index.chunk(cid)}
            user_text = " ".join([*(by_id[row["id"]].get("context") or []), row.get("question", "")])
            validation = validate_answer(ModelAnswer.model_validate(attempt["output"]), retrieved, titles, user_text)
            print(row["id"], attempt["attempt"], validation.checks, validation.failures[:2])
    saved = path.with_name(path.stem + "_summary.json")
    metrics = json.loads(saved.read_text(encoding="utf-8"))["retrieval_metrics"] if saved.is_file() else {}
    summary = summarize(rows, metrics, {}, {}, draft, set_path)
    summary["rescored_from"] = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
    print()
    print_final(summary, rows)
    return 0


def _ratio(values: list[bool]) -> str:
    return f"{sum(values)}/{len(values)}" if values else "0/0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="규정 Q&A 평가")
    parser.add_argument("command", choices=["plan", "dev", "final", "rescore"])
    parser.add_argument("run", nargs="?", type=Path)
    parser.add_argument("--set", type=Path)
    parser.add_argument("--save-raw", action="store_true", help="final: 원 출력을 runs/에 남긴다")
    parser.add_argument("--rescore", type=Path, metavar="RUN.jsonl", help="final: 저장된 원 출력만 다시 채점")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    settings = get_settings()
    try:
        runtime = build_runtime(settings)
    except IndexUnavailable as exc:
        print(f"색인이 없어 평가할 수 없습니다: {exc}")
        return 1
    set_path = args.set or EVAL_DIR / ("dev.yaml" if args.command == "dev" else "final.yaml")
    items, draft = load_set(set_path)
    rescore = args.rescore or (args.run if args.command == "rescore" else None)
    if rescore:
        return cmd_rescore(rescore, items, draft, runtime, set_path)
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
    return cmd_final(items, draft, runtime, set_path, args.save_raw)


if __name__ == "__main__":
    raise SystemExit(main())
