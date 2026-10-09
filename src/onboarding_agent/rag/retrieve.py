"""Search the regulation index (instruction v2, section 7.2).

Paths, all over the same chunk list:
- vector: question embedding · normalised chunk vectors (cosine), top-k
- bm25: kiwipiepy terms, "제15조의2" kept whole, glossary terms added to the
  keyword query only (the vector query is the question as asked)
- rrf: reciprocal rank fusion of the two
- direct: a law name plus "제n조" in the question puts that article first
- refs: articles referenced by the top hits are added as "참조 조문"

If the question embedding fails (or its budget is spent), search falls back
to BM25 and reports degraded=True. The gate keeps the model out when nothing
relevant was found: the best cosine is below the minimum score, or (BM25
only) no content word of the question occurs in the top hits. Scores decide
ordering and the gate; they are never shown as accuracy or confidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import yaml

from ..retrieval import BM25Index, content_terms, reciprocal_rank_fusion, tokenize
from ..retrieval.tokenize import ARTICLE
from .embed import Embedder
from .index import LoadedIndex
from .models import RegChunk
from .usage import BudgetExceeded

Mode = Literal["vector", "bm25", "rrf"]
# Chosen on the development set (docs/DECISIONS.md, 2026-10-10 규정 RAG): vector had the best hit@1 and MRR.
# v2 floor (D3): the score gate only drops clear out-of-scope questions without a model call; the model's
# escalation and the checks decide the rest. 0.636 = the lower of (highest institution-internal out-of-scope
# score 0.6429 + 0.02) and (lowest in-scope score with right retrieval 0.6460 − 0.01). v1 used 0.705.
DEFAULT_MODE: Mode = "vector"
DEFAULT_MIN_SCORE: float | None = 0.636
CANDIDATES = 50  # per ranking before fusion
REF_EXPANSION_FROM = 3  # refs of the top hits
REF_EXPANSION_MAX = 4


@dataclass
class Hit:
    chunk: RegChunk
    via: str  # vector | bm25 | rrf | direct | ref
    vector_score: float | None = None
    bm25_score: float | None = None


@dataclass
class RetrievalResult:
    query: str
    mode: str
    hits: list[Hit]
    degraded: bool = False
    degraded_reason: str | None = None
    top_vector_score: float | None = None
    gated: bool = False
    gate_reason: str | None = None
    rankings: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ids(self) -> list[str]:
        return [hit.chunk.chunk_id for hit in self.hits]


def load_glossary(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): [str(v) for v in values] for k, values in data.items()}


class Retriever:
    def __init__(
        self,
        index: LoadedIndex,
        embedder: Embedder | None,
        *,
        glossary: dict[str, list[str]] | None = None,
        min_score: float | None = None,
    ) -> None:
        self.index = index
        self.embedder = embedder
        self.glossary = glossary or {}
        self.min_score = DEFAULT_MIN_SCORE if min_score is None else min_score
        self.terms = [tokenize(c.search_text + " " + (c.article_title or "")) for c in index.chunks]
        self.bm25 = BM25Index(self.terms)
        # Names point at the law itself; its annexes and table transcriptions share the title.
        self.aliases = _doc_aliases({d.doc_title: d.doc_id for d in index.docs.values() if not d.parent_doc_id})
        self.family = {d.doc_id: {d.doc_id} for d in index.docs.values() if not d.parent_doc_id}
        for d in index.docs.values():
            if d.parent_doc_id in self.family:
                self.family[d.parent_doc_id].add(d.doc_id)

    # --- public -------------------------------------------------------------------------------

    def search(
        self, question: str, *, top_k: int = 6, mode: Mode = DEFAULT_MODE, context: list[str] | None = None
    ) -> RetrievalResult:
        query = " ".join([*(context or []), question]).strip()
        result = RetrievalResult(query=query, mode=mode, hits=[])
        vector_scores: np.ndarray | None = None
        if mode in ("vector", "rrf"):
            vector_scores, error = self._vector_scores(query)
            if vector_scores is None:
                result.degraded, result.degraded_reason, result.mode = True, error, "bm25"
            else:
                result.top_vector_score = float(vector_scores.max()) if len(vector_scores) else None
        keyword_terms = self._keyword_terms(query)
        bm25_scores = self.bm25.scores(keyword_terms)

        vector_rank = list(np.argsort(-vector_scores)[:CANDIDATES]) if vector_scores is not None else []
        bm25_rank = [int(i) for i in np.argsort(-bm25_scores)[:CANDIDATES] if bm25_scores[i] > 0]
        result.rankings = {
            "vector": [self.index.chunks[i].chunk_id for i in vector_rank[:10]],
            "bm25": [self.index.chunks[i].chunk_id for i in bm25_rank[:10]],
        }
        if result.mode == "vector":
            order, via = vector_rank, "vector"
        elif result.mode == "bm25":
            order, via = bm25_rank, "bm25"
        else:
            order, via = reciprocal_rank_fusion([vector_rank, bm25_rank]), "rrf"

        direct = self._direct(question)
        seen: set[int] = set()
        for i in direct:
            seen.add(i)
            result.hits.append(self._hit(i, "direct", vector_scores, bm25_scores))
        for i in order:
            if len(result.hits) >= top_k + len(direct):
                break
            if i not in seen:
                seen.add(i)
                result.hits.append(self._hit(int(i), via, vector_scores, bm25_scores))
        result.hits += self._ref_hits(result.hits, seen, vector_scores, bm25_scores)
        self._gate(result, question, keyword_terms, bool(direct))
        return result

    def article(self, doc_title: str, article_no: str) -> list[RegChunk]:
        doc_id = self.aliases.get(doc_title.replace(" ", "")) or self.aliases.get(doc_title)
        number = article_no.replace("제", "").replace("조", "").replace(" ", "") or article_no
        docs = self.family.get(doc_id or "", set())
        return [c for c in self.index.chunks if c.doc_id in docs and c.article_no == number]

    # --- internals ----------------------------------------------------------------------------

    def _vector_scores(self, query: str) -> tuple[np.ndarray | None, str | None]:
        if self.embedder is None:
            return None, "임베딩 설정 없음"
        try:
            vector = self.embedder.embed_query(query)
        except BudgetExceeded as exc:
            return None, f"질문 임베딩 예산 소진: {exc}"
        except Exception as exc:  # network, quota, bad response
            return None, f"질문 임베딩 실패: {type(exc).__name__}"
        return self.index.vectors @ vector.astype(np.float32), None

    def _keyword_terms(self, query: str) -> list[str]:
        terms = tokenize(query)
        for word, extra in self.glossary.items():
            if word in query:
                terms += [t for term in extra for t in tokenize(term)]
        return terms

    def _direct(self, question: str) -> list[int]:
        compact = question.replace(" ", "")
        doc_ids = [
            doc_id for alias, doc_id in sorted(self.aliases.items(), key=lambda kv: -len(kv[0])) if alias in compact
        ]
        if not doc_ids:
            return []
        doc_id = doc_ids[0]
        hits: list[int] = []
        for match in ARTICLE.finditer(question):
            number = match.group(1) + (f"의{match.group(2)}" if match.group(2) else "")
            docs = self.family.get(doc_id, {doc_id})  # the article and its transcribed tables
            hits += [i for i, c in enumerate(self.index.chunks) if c.doc_id in docs and c.article_no == number]
        return list(dict.fromkeys(hits))

    def _ref_hits(self, hits: list[Hit], seen: set[int], vector_scores, bm25_scores) -> list[Hit]:
        extra: list[Hit] = []
        for hit in hits[:REF_EXPANSION_FROM]:
            for ref in hit.chunk.refs:
                position = self.index.position(ref)
                if position is None or position in seen or len(extra) >= REF_EXPANSION_MAX:
                    continue
                seen.add(position)
                extra.append(self._hit(position, "ref", vector_scores, bm25_scores))
        return extra

    def _hit(self, i: int, via: str, vector_scores, bm25_scores) -> Hit:
        return Hit(
            chunk=self.index.chunks[i],
            via=via,
            vector_score=float(vector_scores[i]) if vector_scores is not None else None,
            bm25_score=float(bm25_scores[i]),
        )

    def _gate(self, result: RetrievalResult, question: str, keyword_terms: list[str], direct: bool) -> None:
        if direct or not result.hits:
            if not result.hits:
                result.gated, result.gate_reason = True, "검색된 근거가 없습니다."
            return
        if result.top_vector_score is not None and self.min_score is not None:
            if result.top_vector_score < self.min_score:
                result.gated = True
                result.gate_reason = "질문과 충분히 가까운 규정 조문을 찾지 못했습니다."
            return
        if result.top_vector_score is None:  # keyword-only search
            wanted = content_terms(question)
            found = set().union(
                *(self.bm25.matched(list(wanted), self.index.position(h.chunk.chunk_id)) for h in result.hits[:3])
            )
            if not found:
                result.gated, result.gate_reason = (
                    True,
                    "질문의 핵심어가 들어 있는 조문을 찾지 못했습니다(키워드 검색).",
                )


def _doc_aliases(titles: dict[str, str]) -> dict[str, str]:
    """Names a question may use for a document: the title, without spaces, and its last word."""
    aliases: dict[str, str] = {}
    for title, doc_id in titles.items():
        aliases[title] = doc_id
        aliases[title.replace(" ", "")] = doc_id
        last = title.split()[-1]
        if len(last) >= 3:
            aliases.setdefault(last, doc_id)
    return aliases
