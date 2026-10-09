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

from .chunker import chunk_law, estimate_tokens, link_refs, source_doc
from .models import RegChunk, SourceDoc
from .parse_law import ParsedLaw, parse_pdf
from .sources import build_inventory

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
    priority = list(targets.get("priority", []))
    for entry in inventory["entries"]:
        if not entry["indexed"]:
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
    indexed_ids = {d.doc_id for d in plan.docs}
    plan.chunks = link_refs([c for c in plan.chunks if c.doc_id in indexed_ids])
    return plan
