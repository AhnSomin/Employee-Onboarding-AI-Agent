"""From source folders to the chunk set to index (everything before embedding).

Targets come from `data/regulations/targets.yaml` (priority order, required
titles) and are matched against the title printed in each source, so the
indexed name is the source's current name. Documents are taken in priority
order while the chunk and token caps hold; the rest are listed as unindexed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from .chunker import (
    annex_source_doc,
    attach_tables,
    chunk_annex,
    chunk_law,
    chunk_transcription,
    estimate_tokens,
    link_refs,
    parse_transcription,
    source_doc,
    transcription_source_doc,
)
from .models import RegChunk, SourceDoc
from .parse_law import ParsedLaw, parse_annex_pdf, parse_pdf
from .sources import CLASSES, build_inventory

MAX_CHUNKS = 200
MAX_DOC_TOKENS = 120_000  # estimated


@dataclass
class BuildPlan:
    inventory: dict
    docs: list[SourceDoc] = field(default_factory=list)
    chunks: list[RegChunk] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    unindexed: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)

    @property
    def est_tokens(self) -> int:
        return sum(estimate_tokens(c.embed_text) for c in self.chunks)


def load_targets(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}


def prepare(source_dirs: dict[str, Path], repo_root: Path, targets: dict) -> BuildPlan:
    inventory = build_inventory(source_dirs, repo_root)
    plan = BuildPlan(inventory)
    candidates: list[tuple[int, str, Path, dict]] = []
    annexes: list[tuple[Path, dict]] = []
    transcriptions: list[tuple[Path, dict]] = []
    priority = list(targets.get("priority", []))
    for entry in inventory["entries"]:
        if not entry["indexed"]:
            continue
        if entry["path"].startswith("data/manual/tables/"):
            transcriptions.append((repo_root / entry["path"], entry))
            continue
        if entry["category"] == CLASSES[2]:
            label, _, name = entry["path"].partition("/")
            annexes.append((source_dirs[label] / name, entry))
            continue
        label, _, name = entry["path"].partition("/")
        path = source_dirs[label] / name
        rank = priority.index(entry["title"]) if entry["title"] in priority else len(priority)
        candidates.append((rank, entry["title"] or name, path, entry))
    candidates.sort(key=lambda c: (c[0], c[1]))
    plan.missing_required = [t for t in targets.get("required", []) if t not in {c[1] for c in candidates}]

    parsed: list[tuple[ParsedLaw, SourceDoc, dict]] = []
    collected = datetime.now().astimezone()
    for _, _, path, entry in candidates[: targets.get("max_docs", 5)]:
        law = parse_pdf(path)
        doc = source_doc(law, sha256=entry["sha256"], source_uri=f"local:{entry['path']}", collected_at=collected)
        parsed.append((law, doc, entry))
    for _, title, _, _ in candidates[targets.get("max_docs", 5) :]:
        plan.unindexed.append({"title": title, "reason": "첫 구축 문서 수 상한(max_docs) 초과"})

    known = {doc.doc_title: doc.doc_id for _, doc, _ in parsed}
    for law, doc, _ in parsed:
        chunks, dropped = chunk_law(law, doc, known)
        tokens = sum(estimate_tokens(c.embed_text) for c in chunks)
        if len(plan.chunks) + len(chunks) > MAX_CHUNKS or plan.est_tokens + tokens > MAX_DOC_TOKENS:
            plan.unindexed.append(
                {"title": doc.doc_title, "reason": f"청크·토큰 상한 초과(청크 {len(chunks)}, 추정 {tokens})"}
            )
            continue
        plan.docs.append(doc)
        plan.chunks += chunks
        plan.excluded += [{"doc": doc.doc_title, **item} for item in law.excluded + dropped]
        plan.warnings += [f"{doc.doc_title}: {w}" for w in law.warnings]
    parents = {d.doc_title: d for d in plan.docs}
    for path, entry in annexes:
        annex = parse_annex_pdf(path)
        parent = parents.get(annex.law_title) if annex else None
        if annex is None or parent is None:
            plan.unindexed.append(
                {"title": entry["title"] or path.name, "reason": "본문 법령이 색인에 없거나 별표를 읽지 못함"}
            )
            continue
        doc = annex_source_doc(
            annex, parent, sha256=entry["sha256"], source_uri=f"local:{entry['path']}", collected_at=collected
        )
        plan.docs.append(doc)
        plan.chunks += chunk_annex(annex, doc)
    for path, entry in transcriptions:
        front, table = parse_transcription(path.read_text(encoding="utf-8"))
        parent = parents.get(str(front.get("법령명")))
        if parent is None or (front.get("버전") and front["버전"] != parent.version_label):
            plan.unindexed.append({"title": path.name, "reason": "전사본의 법령·버전이 색인된 본문과 맞지 않음"})
            continue
        doc = transcription_source_doc(
            front, parent, sha256=entry["sha256"], source_uri=f"local:{entry['path']}", collected_at=collected
        )
        plan.docs.append(doc)
        plan.chunks.append(chunk_transcription(front, table, doc))
    indexed_ids = {d.doc_id for d in plan.docs}
    plan.chunks = attach_tables(link_refs([c for c in plan.chunks if c.doc_id in indexed_ids]))
    plan.excluded = [item for item in plan.excluded if not _covered(item, plan.chunks)]
    return plan


def _covered(item: dict, chunks: list[RegChunk]) -> bool:
    """An image table that now has a transcription is no longer an exclusion."""
    if item.get("kind") != "이미지 표·그림":
        return False
    article = item["detail"].split("조")[0].removeprefix("제") if item["detail"].startswith("제") else None
    return any(c.kind == "table" and c.article_no == article for c in chunks)
