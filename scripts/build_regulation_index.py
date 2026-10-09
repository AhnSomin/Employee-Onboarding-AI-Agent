"""Build or update the regulation index (the only way to build it; the app only reads).

Run with:
    uv run python scripts/build_regulation_index.py inventory --source <folder>
    uv run python scripts/build_regulation_index.py plan --source <folder>      # parse and chunk, no calls
    uv run python scripts/build_regulation_index.py build --source <folder>     # embeds (counted), then swaps CURRENT
    uv run python scripts/build_regulation_index.py status

--source can be given more than once; data/manual/ (files downloaded by hand from
국가법령정보센터) is always included when it exists. Originals are only read.
`build` embeds only chunks that are not in the embedding cache, so running it
again on the same sources makes no embedding call. It writes
data/regulations/inventory.json and manifest_summary.json (metadata only).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from onboarding_agent.config import REPO_ROOT, get_settings
from onboarding_agent.rag.build import BuildPlan, load_targets, prepare
from onboarding_agent.rag.embed import EmbeddingError
from onboarding_agent.rag.index import IndexUnavailable, build_index, load_index
from onboarding_agent.rag.service import USAGE_LOG, counting_client, embedding_config, make_embedder
from onboarding_agent.rag.sources import write_inventory
from onboarding_agent.rag.usage import BudgetExceeded, UsageMeter

REG_DATA = REPO_ROOT / "data" / "regulations"


def source_dirs(sources: list[str]) -> dict[str, Path]:
    dirs = {Path(s).expanduser().name: Path(s).expanduser() for s in sources}
    manual = REPO_ROOT / "data" / "manual"
    if manual.is_dir():
        dirs.setdefault("data/manual", manual)
    return dirs


def print_plan(plan: BuildPlan) -> None:
    print("자료 목록:")
    for e in plan.inventory["entries"]:
        print(
            f"- [{e['category']}] {e['path']} · {e['format']} · {e['size']:,}B · 추출 {e['text_extractable']} "
            f"· 외부 전송 {e['external_send']} · {'색인' if e['indexed'] else '제외'} — {e['reason']}"
        )
    print("\n색인 대상:")
    for d in plan.docs:
        count = sum(c.doc_id == d.doc_id for c in plan.chunks)
        when = d.effective_date.isoformat() if d.effective_date else "미확인"
        print(f"- {d.doc_id} {d.doc_title} · {d.version_label} · 시행 {when} · 청크 {count}")
    for u in plan.unindexed:
        print(f"- 미색인: {u['title']} — {u['reason']}")
    if plan.missing_required:
        print("- 필수 문서 없음: " + ", ".join(plan.missing_required))
    print(f"\n청크 {len(plan.chunks)}개 · 추정 입력 토큰(글자 수 기준 상한) {plan.est_tokens:,}")
    kinds: dict[str, int] = {}
    for item in plan.excluded:
        kinds[item["kind"]] = kinds.get(item["kind"], 0) + 1
    print("제외: " + ", ".join(f"{k} {v}" for k, v in kinds.items()))
    for warning in plan.warnings:
        print(f"경고: {warning}")


def write_summary(plan: BuildPlan, manifest: dict | None) -> None:
    summary = {
        "documents": manifest["documents"] if manifest else [],
        "index_version": manifest["index_version"] if manifest else None,
        "embedding": manifest["embedding"] if manifest else None,
        "chunk_count": len(plan.chunks),
        "est_input_tokens_upper_bound": plan.est_tokens,
        "excluded": plan.excluded,
        "unindexed": plan.unindexed,
        "usage": manifest.get("usage") if manifest else None,
    }
    (REG_DATA / "manifest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="규정 색인을 만들거나 갱신합니다.")
    parser.add_argument("command", choices=["inventory", "plan", "build", "status"])
    parser.add_argument("--source", action="append", default=[], help="규정 원문 폴더(읽기만 함)")
    parser.add_argument("--fixture", type=Path, help="첫 임베딩 응답 형태를 저장할 경로(값은 앞 3개만)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    settings = get_settings()

    if args.command == "status":
        try:
            index = load_index(settings.reg_index_dir, embedding_config(settings))
        except IndexUnavailable as exc:
            print(f"색인 없음 또는 사용 불가: {exc}")
            return 1
        print(f"색인 {index.version} · 청크 {len(index.chunks)} · 문서 {len(index.docs)}")
        print(json.dumps(index.manifest["embedding"], ensure_ascii=False))
        print("사용량:", json.dumps(UsageMeter(USAGE_LOG).totals(), ensure_ascii=False))
        return 0

    plan = prepare(source_dirs(args.source), REPO_ROOT, load_targets(REG_DATA / "targets.yaml"))
    write_inventory(plan.inventory, REG_DATA / "inventory.json")
    print_plan(plan)
    if plan.missing_required:
        print("필수 문서가 없어 멈춥니다. 원문 폴더(--source)나 data/manual/을 확인하세요.")
        return 1
    if args.command in ("inventory", "plan"):
        return 0

    meter = UsageMeter(USAGE_LOG)
    counter = counting_client(settings, meter, purpose="document")
    embedder = make_embedder(settings, counter)
    if embedder is None:
        print("GEMINI_API_KEY가 없어 임베딩할 수 없습니다.")
        return 1
    before = meter.totals()
    try:
        version = build_index(
            settings.reg_index_dir,
            plan.docs,
            plan.chunks,
            embedder,
            excluded=plan.excluded,
            unindexed=plan.unindexed,
        )
    except (EmbeddingError, BudgetExceeded, IndexUnavailable, ValueError) as exc:
        print(f"[FAIL] 색인 구축 실패 — 기존 색인은 그대로입니다: {exc}")
        return 1
    after = meter.totals()
    usage = {
        "document_requests": after.get("embed_doc_requests", 0) - before.get("embed_doc_requests", 0),
        "document_est_tokens": after.get("embed_doc_tokens", 0) - before.get("embed_doc_tokens", 0),
        "embedded_now": embedder.embedded,
        "from_cache": embedder.cached,
    }
    folder = settings.reg_index_dir / version
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["usage"] = usage
    (folder / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    write_summary(plan, manifest)
    if args.fixture and embedder.last_response_shape:
        args.fixture.parent.mkdir(parents=True, exist_ok=True)
        args.fixture.write_text(
            json.dumps(embedder.last_response_shape, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(
        f"\n[OK] 색인 {version} — 이번 임베딩 요청 {usage['document_requests']}회, "
        f"새로 임베딩 {usage['embedded_now']}개, "
        f"캐시 사용 {usage['from_cache']}개, 추정 토큰 {usage['document_est_tokens']:,}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
