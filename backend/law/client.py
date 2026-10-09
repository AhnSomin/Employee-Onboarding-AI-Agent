"""법제처 국가법령정보센터 Open API 클라이언트 (+ 로컬 캐시, 키 없으면 요약 파일로 폴백).

NOTE: 엔드포인트/응답 필드는 공식 가이드(open.law.go.kr) 기준으로 작성했으며
실제 키로 응답 구조 검증이 필요하다 (TODO).
"""
import json
import re

import requests

from backend import config

BASE = "http://www.law.go.kr/DRF"
CACHE = config.DATA_DIR / "law_cache"
FALLBACK = config.DATA_DIR / "leave_policy.md"


def _cache_path(name: str):
    CACHE.mkdir(exist_ok=True)
    return CACHE / (re.sub(r"\W+", "_", name) + ".json")


def fetch_law(name: str = "국가공무원 복무규정") -> dict | None:
    """법령명으로 검색 → 본문(조문) 조회. 실패/키 없음이면 None."""
    cp = _cache_path(name)
    if cp.exists():
        return json.loads(cp.read_text())
    if not config.LAW_API_OC:
        return None
    try:
        s = requests.get(f"{BASE}/lawSearch.do", timeout=15,
                         params={"OC": config.LAW_API_OC, "target": "law", "type": "JSON", "query": name, "display": 10}).json()
        hits = s["LawSearch"]["law"]
        hits = hits if isinstance(hits, list) else [hits]
        norm = lambda x: re.sub(r"\s+", "", x)
        hit = next((h for h in hits if norm(h["법령명한글"]) == norm(name)), None)
        if hit is None:  # 이름이 정확히 일치하는 법령만 사용 (엉뚱한 법령 방지)
            return None
        body = requests.get(f"{BASE}/lawService.do", timeout=30,
                            params={"OC": config.LAW_API_OC, "target": "law", "type": "JSON",
                                    "MST": hit["법령일련번호"]}).json()
        cp.write_text(json.dumps(body, ensure_ascii=False))
        return body
    except Exception:
        return None


def _article_text(a: dict) -> str:
    """조문내용은 제목뿐이라 항·호 본문을 이어 붙이고, 개정이력/이미지 태그는 제거."""
    parts = [a.get("조문내용", "")]
    hangs = a.get("항", [])
    for h in hangs if isinstance(hangs, list) else [hangs]:
        parts.append(h.get("항내용", ""))
        hos = h.get("호", [])
        for ho in hos if isinstance(hos, list) else [hos]:
            parts.append("  " + ho.get("호내용", ""))
    text = "\n".join(p for p in parts if p)
    return re.sub(r"<img[^>]*>|</img>|<개정[^>]*>|<신설[^>]*>", "", text)


SYNONYMS = {"반차": "반일 연가", "휴가": "연가", "쉬": "연가", "쉬다": "연가", "아프": "병가", "경조사": "경조사 휴가"}


def _grams(text: str) -> set[str]:
    t = re.sub(r"\W+", "", text)
    return {t[i:i + 2] for i in range(len(t) - 1)}


def fetch_admrul(name: str) -> dict | None:
    """행정규칙(예규·훈령·지침) 본문. 이름이 정확히 일치하는 것만. 본문이 첨부파일뿐인 규칙은 None."""
    cp = _cache_path("admrul_" + name)
    if cp.exists():
        return json.loads(cp.read_text())
    if not config.LAW_API_OC:
        return None
    try:
        r = requests.get(f"{BASE}/lawSearch.do", timeout=15,
                         params={"OC": config.LAW_API_OC, "target": "admrul", "type": "JSON", "query": name, "display": 10}).json()
        hits = r["AdmRulSearch"]["admrul"]
        hits = hits if isinstance(hits, list) else [hits]
        norm = lambda x: re.sub(r"\s+", "", x)
        hit = next((h for h in hits if norm(h["행정규칙명"]) == norm(name)), None)
        if hit is None:
            return None
        body = requests.get(f"{BASE}/lawService.do", timeout=30,
                            params={"OC": config.LAW_API_OC, "target": "admrul", "type": "JSON",
                                    "ID": hit["행정규칙일련번호"]}).json()["AdmRulService"]
        if not isinstance(body.get("조문내용"), list):
            return None
        cp.write_text(json.dumps(body, ensure_ascii=False))
        return body
    except Exception:
        return None


def search_articles(query: str, law: str = "국가공무원 복무규정", k: int = 3) -> list[str]:
    """조문 단위로 쪼개 2-gram(글자 두 개) 겹침으로 순위를 매긴다 (임베딩 RAG로 교체 예정).
    조사·어미가 달라도 매칭되고, 흔한 글자쌍은 낮은 가중치(역문서빈도)를 받는다.
    관련 조문이 없으면 빈 리스트 (추측용 폴백 문서를 주지 않는다). 법령 API 자체를 못 쓸 때만 요약 파일을 준다."""
    body = fetch_law(law)
    if not body:
        return [FALLBACK.read_text()]
    arts = body["법령"]["조문"]["조문단위"]
    arts = [a for a in (arts if isinstance(arts, list) else [arts]) if a.get("조문여부") == "조문"]
    for src, dst in SYNONYMS.items():
        if src in query:
            query += " " + dst
    texts = [_article_text(a) for a in arts]
    grams = [_grams(a.get("조문내용", "")) for a in arts]
    bodies = [_grams(tx) for tx in texts]
    q = _grams(query)
    df = {g: sum(g in b for b in bodies) for g in q}
    idf = {g: 1.0 / df[g] for g in q if df[g]}
    scored = sorted(((sum(idf[g] * (3 if g in tg else 1) for g in idf if g in b), tx[:2500])
                     for b, tg, tx in zip(bodies, grams, texts)), key=lambda x: -x[0])
    return [tx for sc, tx in scored[:k] if sc > 0.3]
