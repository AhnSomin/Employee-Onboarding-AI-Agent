"""규정 RAG: 법제처 조문 → 조·항 단위 청킹 → OpenAI 임베딩 → 하이브리드(벡터 + 키워드) 검색.
인덱스 구축: python -m backend.rag.build"""
import json
import re
from functools import lru_cache

import numpy as np

from backend import config
from backend.law import client as law

LAWS = ["국가공무원 복무규정", "공무원 여비 규정", "공무원보수규정", "국가데이터처와 그 소속기관 직제 시행규칙"]
INDEX_DIR = config.DATA_DIR / "rag_index"
EMBED_MODEL = "text-embedding-3-small"
MAX_CHARS = 1800
MIN_COSINE = 0.28     # 의미 유사도가 이보다 낮으면 '관련 조문 없음' (관련 질문 최저 0.307, 무관 질문 대부분 0.2대)
_CLEAN = re.compile(r"<img[^>]*>|</img>|<(?:개정|신설|삭제|본조신설|전문개정|종전)[^>]*>")


def _clean(t: str) -> str:
    return re.sub(r"[ \t]+", " ", _CLEAN.sub("", t)).strip()


def chunk_law(name: str, body: dict) -> list[dict]:
    """조문 → 항 단위 청크 (항이 없으면 조 전체). 호는 항에 포함."""
    arts = body["법령"]["조문"]["조문단위"]
    out = []
    for a in arts if isinstance(arts, list) else [arts]:
        if a.get("조문여부") != "조문":
            continue
        title = a["조문내용"].split(")")[0] + ")" if ")" in a["조문내용"] else a["조문내용"]
        hangs = a.get("항")
        if not hangs:
            out.append({"law": name, "article": title, "para": "", "text": _clean(a["조문내용"])})
            continue
        for h in hangs if isinstance(hangs, list) else [hangs]:
            parts = [h.get("항내용", "")]
            hos = h.get("호", [])
            for ho in hos if isinstance(hos, list) else [hos]:
                parts.append(ho.get("호내용", ""))
            text = _clean("\n".join(p for p in parts if p))
            if text:
                out.append({"law": name, "article": title, "para": h.get("항번호", ""), "text": text})
    return out


def chunk_tables(name: str, body: dict) -> list[dict]:
    """별표(표)는 휴가 일수·여비 금액 같은 숫자가 있어 따로 색인한다. 길면 줄 단위로 나눈다."""
    out = []
    units = (body["법령"].get("별표") or {}).get("별표단위", [])
    for u in units if isinstance(units, list) else [units]:
        if u.get("별표구분") != "별표":
            continue
        lines = [re.sub(r"\s+", " ", ln).strip() for blk in u.get("별표내용", []) for ln in (blk if isinstance(blk, list) else [blk])]
        lines = [ln for ln in lines if ln]
        title = f'별표 {int(u["별표번호"])}({u["별표제목"]})'
        pieces, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) > MAX_CHARS and cur:
                pieces.append(cur); cur = ""
            cur += ln + "\n"
        if cur:
            pieces.append(cur)
        for i, pc in enumerate(pieces):
            out.append({"law": name, "article": title, "para": f"({i + 1}/{len(pieces)})" if len(pieces) > 1 else "", "text": pc.strip()})
    return out


def label(c: dict) -> str:
    return f'{c["law"]} {c["article"]} {c["para"]}'.strip()


def _embed(texts: list[str]) -> np.ndarray:
    from openai import OpenAI
    cl = OpenAI(api_key=config.OPENAI_API_KEY, timeout=60)
    vecs = []
    for i in range(0, len(texts), 64):
        r = cl.embeddings.create(model=EMBED_MODEL, input=texts[i:i + 64])
        vecs += [d.embedding for d in r.data]
    v = np.array(vecs, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def build() -> int:
    chunks = []
    for name in LAWS:
        body = law.fetch_law(name)
        if not body:
            print(f"[skip] {name}: 법령을 가져오지 못함")
            continue
        cs = chunk_law(name, body) + chunk_tables(name, body)
        print(f"{name}: {len(cs)} chunks")
        chunks += cs
    vecs = _embed([f'{label(c)}\n{c["text"][:MAX_CHARS]}' for c in chunks])
    INDEX_DIR.mkdir(exist_ok=True)
    np.save(INDEX_DIR / "vectors.npy", vecs)
    (INDEX_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False))
    load_index.cache_clear()
    return len(chunks)


@lru_cache(maxsize=1)
def load_index():
    f = INDEX_DIR / "chunks.json"
    if not f.exists():
        return None
    return json.loads(f.read_text()), np.load(INDEX_DIR / "vectors.npy")


def _grams(t: str) -> set[str]:
    t = re.sub(r"\W+", "", t)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def _lexical(query: str, chunks: list[dict]) -> np.ndarray:
    bodies = [_grams(label(c) + c["text"]) for c in chunks]
    titles = [_grams(c["article"]) for c in chunks]
    q = _grams(query)
    idf = {g: 1.0 / n for g in q if (n := sum(g in b for b in bodies))}
    return np.array([sum(w * (3 if g in t else 1) for g, w in idf.items() if g in b) for b, t in zip(bodies, titles)])


@lru_cache(maxsize=512)
def _embed_query(q: str) -> np.ndarray:
    return _embed([q])[0]


def search(query: str, k: int = 5, mode: str = "hybrid") -> list[dict]:
    """mode: 'vector' | 'keyword' | 'hybrid'. 관련 없으면 빈 리스트."""
    idx = load_index()
    if idx is None:
        return []
    chunks, vecs = idx
    lex = _lexical(query, chunks)
    if mode == "keyword" or not config.OPENAI_API_KEY:
        order = np.argsort(-lex)[:k]
        return [{**chunks[i], "score": float(lex[i])} for i in order if lex[i] > 0.3]
    try:
        cos = vecs @ _embed_query(query)
    except Exception:
        order = np.argsort(-lex)[:k]
        return [{**chunks[i], "score": float(lex[i])} for i in order if lex[i] > 0.3]
    if cos.max() < MIN_COSINE:   # 키워드는 '추천' 같은 흔한 글자쌍에 속으므로 판정에 쓰지 않음
        return []
    if mode == "vector":
        order = np.argsort(-cos)[:k]
        return [{**chunks[i], "score": float(cos[i])} for i in order]
    rr = {}                                    # Reciprocal Rank Fusion
    for scores in (cos, lex):
        for rank, i in enumerate(np.argsort(-scores)[:20]):
            rr[int(i)] = rr.get(int(i), 0) + 1 / (60 + rank)
    order = sorted(rr, key=lambda i: -rr[i])[:k]
    return [{**chunks[i], "score": float(cos[i])} for i in order]


def format_hits(hits: list[dict]) -> list[str]:
    return [f'[{label(h)}]\n{h["text"][:MAX_CHARS]}' for h in hits]
