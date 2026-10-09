"""규정 RAG: 법제처 조문 → 조·항 단위 청킹 → OpenAI 임베딩 → 하이브리드(벡터 + 키워드) 검색.
인덱스 구축: python -m backend.rag.build"""
import json
import re
from functools import lru_cache

import numpy as np

from backend import config
from backend.law import client as law

# (이름, 종류): law=법령, admrul=행정규칙(지침·예규)
SOURCES = [
    ("국가공무원 복무규정", "law"), ("공무원 여비 규정", "law"), ("공무원보수규정", "law"),
    ("국가데이터처와 그 소속기관 직제 시행규칙", "law"),
    ("공무원 후생복지에 관한 규정", "law"), ("공무원수당 등에 관한 규정", "law"),
    ("국가공무원법", "law"), ("공무원임용령", "law"), ("공무원 행동강령", "law"),
    ("국가데이터처 맞춤형 복지제도 운영지침", "admrul"),
    # --- 신입이 자주 묻는 주제 확장 (3번째 값: 조문 제목 필터 — 너무 방대한 법령은 관련 조문만)
    ("부정청탁 및 금품등 수수의 금지에 관한 법률", "law"), ("부정청탁 및 금품등 수수의 금지에 관한 법률 시행령", "law"),
    ("공무원연금법", "law"), ("공무원 재해보상법", "law"),
    ("공무원 인재개발법", "law"), ("공무원 인재개발법 시행령", "law"),
    ("공무원 성과평가 등에 관한 규정", "law"),
    ("통계법", "law"), ("통계법 시행령", "law"),
    ("소득세법", "law"),
    ("소득세법 시행령", "law", r"근로소득|공제|세액|연말정산|원천징수|부양|비과세|과세표준"),
    ("조세특례제한법", "law", r"월세|신용카드|현금영수증|소득공제|세액공제|근로|주택마련|청약|연금계좌|기부금"),
]
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


def chunk_admrul(name: str, body: dict) -> list[dict]:
    """행정규칙: 조문내용이 문자열 목록이다. '제N조'로 시작하는 항목이 조, 장 제목은 건너뛴다. 긴 조는 항(①②…) 경계로 나눈다."""
    out = []
    for item in body["조문내용"]:
        m = re.match(r"(제\d+조(?:의\d+)?\([^)]*\))\s*(.*)", item, re.S)
        if not m:
            continue  # '제2장 …' 같은 장 제목
        title, rest = m.group(1), _clean(item)
        if len(rest) <= MAX_CHARS:
            out.append({"law": name, "article": title, "para": "", "text": rest})
            continue
        parts = re.split(r"(?=[①-⑳])", rest)
        cur = ""
        for pt in parts:
            if len(cur) + len(pt) > MAX_CHARS and cur:
                out.append({"law": name, "article": title, "para": cur.lstrip()[:1] if cur.lstrip()[:1] in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳" else "", "text": cur.strip()})
                cur = ""
            cur += pt
        if cur.strip():
            out.append({"law": name, "article": title, "para": "", "text": cur.strip()})
    return out


def chunk_tables(name: str, body: dict) -> list[dict]:
    """별표(표)는 휴가 일수·여비 금액 같은 숫자가 있어 따로 색인한다. 길면 줄 단위로 나눈다."""
    out = []
    root = body.get("법령", body)
    units = (root.get("별표") or {}).get("별표단위", [])
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


ATT_DIR = config.DATA_DIR / "attachments"
_CH = re.compile(r"^\s*(제\s*\d+\s*장)\s*/?\s*([^\n●]{2,40})", re.M)


def ensure_attachment_text(att: dict) -> str | None:
    """첨부 PDF → 텍스트. 없으면 법제처에서 내려받아 pdftotext로 변환한다 (poppler 필요)."""
    import subprocess
    ATT_DIR.mkdir(exist_ok=True)
    pdf, txt = ATT_DIR / f'{att["file"]}.pdf', ATT_DIR / f'{att["file"]}.txt'
    if not txt.exists():
        if not pdf.exists():
            import requests
            r = requests.get(att["url"], timeout=120)
            r.raise_for_status()
            pdf.write_bytes(r.content)
        subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), str(txt)], check=True)
    return txt.read_text()


def chunk_attachment(name: str, text: str) -> list[dict]:
    """PDF 한 쪽 = 한 청크(길면 문단 경계로 분할). 목차·거의 빈 쪽은 건너뛰고, 장 제목을 앞에 붙인다.
    출처 표기는 'PDF N쪽' (문서 안 인쇄 쪽수와 다를 수 있어 PDF 기준임을 밝힌다)."""
    out, chapter = [], ""
    for n, page in enumerate(text.split("\f"), 1):
        if m := _CH.search(page):
            chapter = f"{m.group(1).replace(' ', '')} {m.group(2).strip()}"
        body = re.sub(r"●\s*\d+\s*●", "", page)
        body = re.sub(r"[ \t]+", " ", body)
        body = re.sub(r"\n{3,}", "\n\n", body).strip()
        if len(body) < 80 or body.count("···") > 5:
            continue
        pieces, cur = [], ""
        for para in body.split("\n\n"):
            if len(cur) + len(para) > MAX_CHARS and cur:
                pieces.append(cur); cur = ""
            cur += para + "\n\n"
        pieces.append(cur)
        for i, pc in enumerate(x.strip() for x in pieces if x.strip()):
            out.append({"law": name, "article": f"PDF {n}쪽", "para": f"({i + 1}/{len(pieces)})" if len(pieces) > 1 else "",
                        "text": (f"[{chapter}] " if chapter else "") + pc})
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
    for name, kind, *flt in SOURCES:
        body = law.fetch_law(name) if kind == "law" else law.fetch_admrul(name)
        if not body:
            print(f"[skip] {name}: 가져오지 못함")
            continue
        cs = (chunk_law(name, body) if kind == "law" else chunk_admrul(name, body)) + chunk_tables(name, body)
        if flt:
            cs = [c for c in cs if re.search(flt[0], c["article"])]
        print(f"{name}: {len(cs)} chunks")
        chunks += cs
    for att in json.loads((config.DATA_DIR / "attachment_sources.json").read_text()):
        try:
            cs = chunk_attachment(att["name"], ensure_attachment_text(att))
        except Exception as e:
            print(f"[skip] {att['name']}: {type(e).__name__}")
            continue
        print(f'{att["name"]}: {len(cs)} chunks (첨부 PDF)')
        chunks += cs
    vecs = _embed([f'{label(c)}\n{c["text"][:MAX_CHARS]}' for c in chunks])
    INDEX_DIR.mkdir(exist_ok=True)
    np.save(INDEX_DIR / "vectors.npy", vecs)
    (INDEX_DIR / "chunks.json").write_text(json.dumps(chunks, ensure_ascii=False))
    load_index.cache_clear()
    _bm25_stats.cache_clear()
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


@lru_cache(maxsize=1)
def _bm25_stats():
    """글자 2-gram BM25 통계 (조문 제목 글자는 3배 가중). 긴 청크가 점수를 독식하지 않도록 길이 정규화."""
    chunks, _ = load_index()
    docs = []
    for c in chunks:
        tf = {}
        for g in _gram_list(label(c) + c["text"]):
            tf[g] = tf.get(g, 0) + 1
        for g in _gram_list(c["article"]):
            tf[g] = tf.get(g, 0) + 3
        docs.append(tf)
    df = {}
    for tf in docs:
        for g in tf:
            df[g] = df.get(g, 0) + 1
    lens = np.array([sum(tf.values()) for tf in docs], dtype=float)
    return docs, df, lens, lens.mean()


def _gram_list(t: str) -> list[str]:
    t = re.sub(r"\W+", "", t)
    return [t[i:i + 2] for i in range(len(t) - 1)]


def _lexical(query: str, chunks: list[dict]) -> np.ndarray:
    docs, df, lens, avg = _bm25_stats()
    n, k1, b = len(docs), 1.2, 0.75
    scores = np.zeros(n)
    for g in set(_gram_list(query)):
        if g not in df:
            continue
        idf = np.log(1 + (n - df[g] + 0.5) / (df[g] + 0.5))
        for i, tf in enumerate(docs):
            f = tf.get(g)
            if f:
                scores[i] += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * lens[i] / avg))
    return scores


@lru_cache(maxsize=512)
def _embed_query(q: str) -> np.ndarray:
    return _embed([q])[0]


RERANK_MODELS = ["gpt-4.1-mini", "gpt-4o-mini"]
_RERANK_PROMPT = """공무원 규정 검색 결과를 재정렬한다. 사용자 질문에 답하는 데 가장 직접적으로 쓰이는 조문부터, 최대 {k}개의 번호를 고른다.
- 질문이 묻는 내용(일수·금액·절차·정의·대상 등)이 실제로 적힌 조문을 우선한다. 제목만 비슷한 조문은 뒤로.
- 같은 내용이 법령·규정 조문과 예규(PDF 쪽)에 모두 있으면 법령·규정 조문을 앞에 두고, 예규는 세부 기준·사례가 필요할 때 뒤에 함께 고른다.
- 질문과 무관한 조문은 고르지 않는다. 모두 무관하면 빈 목록.
JSON만 출력: {{"ranked": [번호, ...]}}"""


@lru_cache(maxsize=512)
def _rerank_ids(query: str, cands: tuple, k: int) -> tuple:
    """cands: (번호, 설명) 튜플들. LLM이 고른 번호를 순서대로 반환."""
    from openai import OpenAI
    cl = OpenAI(api_key=config.OPENAI_API_KEY, timeout=30, max_retries=1)
    listing = "\n".join(f"[{i}] {d}" for i, d in cands)
    last = None
    for model in RERANK_MODELS:
        try:
            r = cl.chat.completions.create(
                model=model, temperature=0, seed=7,
                messages=[{"role": "system", "content": _RERANK_PROMPT.format(k=k)},
                          {"role": "user", "content": f"질문: {query}\n\n후보:\n{listing}"}],
                response_format={"type": "json_object"})
            ids = json.loads(r.choices[0].message.content).get("ranked", [])
            valid = {i for i, _ in cands}
            return tuple(int(i) for i in ids if int(i) in valid)[:k]
        except Exception as e:
            last = e
    raise RuntimeError(type(last).__name__)


_EXPAND_PROMPT = """공무원 규정 검색을 돕는다. 사용자의 구어체 질문을 법령·규정에 실제로 쓰이는 용어로 바꾼 검색어 2개를 만든다.
예) "야근하면 수당 받아요?" → ["시간외근무수당 초과근무", "야간근무수당 지급"], "스트레스 상담" → ["공무원 상담센터 운영", "건강관리 지원"]
질문에 없는 사실을 만들지 말고, 같은 의미를 다른 표현으로만 쓴다. JSON만 출력: {"queries": ["...", "..."]}"""


@lru_cache(maxsize=512)
def _expand(query: str) -> tuple:
    from openai import OpenAI
    cl = OpenAI(api_key=config.OPENAI_API_KEY, timeout=20, max_retries=1)
    for model in RERANK_MODELS:
        try:
            r = cl.chat.completions.create(model=model, temperature=0, seed=7, response_format={"type": "json_object"},
                                           messages=[{"role": "system", "content": _EXPAND_PROMPT}, {"role": "user", "content": query}])
            qs = json.loads(r.choices[0].message.content).get("queries", [])
            return tuple(str(x) for x in qs[:2] if str(x).strip())
        except Exception:
            continue
    return ()


def _multi_pool(query: str, size: int) -> list[dict]:
    """원 질문 + 법령 용어로 바꾼 검색어들의 하이브리드 결과를 RRF로 합친다."""
    first = search(query, k=size, mode="hybrid")
    if not first:      # 원 질문부터 의미상 무관하면 질의 확장으로 억지 후보를 만들지 않는다
        return []
    lists = [first]
    if config.OPENAI_API_KEY:
        lists += [search(q, k=size, mode="hybrid") for q in _expand(query)]
    score, by_idx = {}, {}
    for hits in lists:
        for rank, h in enumerate(hits):
            score[h["idx"]] = score.get(h["idx"], 0) + 1 / (60 + rank)
            by_idx[h["idx"]] = h
    return [by_idx[i] for i in sorted(score, key=lambda i: -score[i])[:size]]


def _rerank(query: str, hits: list[dict], k: int) -> list[dict]:
    cands = tuple((i, f'{label(h)} — {h["text"][:220].replace(chr(10), " ")}') for i, h in enumerate(hits))
    ids = list(_rerank_ids(query, cands, k))
    # 재순위가 실행마다 달라질 수 있어, 검색 점수가 높은 상위 3개는 빠뜨리지 않고 뒤에 덧붙인다
    ids += [i for i in range(min(3, len(hits))) if i not in ids]
    return [hits[i] for i in ids]


def search(query: str, k: int = 5, mode: str = "rerank") -> list[dict]:
    """mode: 'vector' | 'keyword' | 'hybrid' | 'rerank'(기본: 하이브리드 상위 20 → LLM 재순위). 관련 없으면 빈 리스트."""
    if mode == "rerank":
        pool = _multi_pool(query, 40)
        if not pool or not config.OPENAI_API_KEY:
            return pool[:k]
        try:
            return _rerank(query, pool, k)
        except Exception:
            return pool[:k]  # 재순위 실패 시 하이브리드 결과로
    idx = load_index()
    if idx is None:
        return []
    chunks, vecs = idx
    lex = _lexical(query, chunks)
    if mode == "keyword" or not config.OPENAI_API_KEY:
        order = np.argsort(-lex)[:k]
        return [{**chunks[i], "score": float(lex[i])} for i in order if lex[i] > 3.0]
    try:
        cos = vecs @ _embed_query(query)
    except Exception:
        order = np.argsort(-lex)[:k]
        return [{**chunks[i], "score": float(lex[i])} for i in order if lex[i] > 3.0]
    if cos.max() < MIN_COSINE:   # 키워드는 '추천' 같은 흔한 글자쌍에 속으므로 판정에 쓰지 않음
        return []
    if mode == "vector":
        order = np.argsort(-cos)[:k]
        return [{**chunks[i], "score": float(cos[i]), "idx": int(i)} for i in order]
    rr = {}                                    # Reciprocal Rank Fusion
    for scores in (cos, lex):
        for rank, i in enumerate(np.argsort(-scores)[:40]):
            rr[int(i)] = rr.get(int(i), 0) + 1 / (60 + rank)
    # 한쪽 검색에서만 높게 나온 정답(예: 표)이 합산 점수에서 탈락하지 않도록 각 검색의 상위 10개는 반드시 포함
    must = {int(i) for scores in (cos, lex) for i in np.argsort(-scores)[:10]}
    order = sorted(rr, key=lambda i: (i not in must, -rr[i]))[:k]
    return [{**chunks[i], "score": float(cos[i]), "idx": int(i)} for i in order]


def format_hits(hits: list[dict]) -> list[str]:
    return [f'[{label(h)}]\n{h["text"][:MAX_CHARS]}' for h in hits]
