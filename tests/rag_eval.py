"""RAG 검색 정확도 평가: python -m tests.rag_eval
정답 = (법령, 조문 번호). hit@k = 상위 k개 안에 정답 조문이 있는 질문 비율."""
from backend.rag import index

V, J, Y, D = "국가공무원 복무규정", "공무원 여비 규정", "", "국가데이터처와 그 소속기관 직제 시행규칙"
CASES = [
    ("반차 2번 쓰면 하루로 쳐요?", V, "제16조"), ("입사 1년 안 됐는데 연가 며칠이에요?", V, "제15조"),
    ("병가는 며칠까지 쓸 수 있어요?", V, "제18조"), ("결혼하면 휴가 며칠 받아요?", V, "제20조"),
    ("근무시간이 어떻게 돼요?", V, "제9조"), ("야근하면 시간외근무 인정돼요?", V, "제11조"),
    ("연가 안 쓰면 보상받아요?", V, "제16조"), ("연가를 당겨서 미리 쓸 수 있어요?", V, "제16조"),
    ("지각이나 조퇴하면 연가에서 빠져요?", V, "제17조"), ("다른 일 겸직해도 되나요?", V, "제26조"),
    ("정치 활동 해도 돼요?", V, "제27조"), ("연가를 10일 연속으로 쓰고 싶어요", V, "제16조의4"),
    ("남은 연가 다음해로 넘길 수 있어요?", V, "제16조의3"), ("공가는 어떤 경우에 쓰나요?", V, "제19조"),
    ("출장 가면 숙박비 얼마 줘요?", J, "제16조"), ("기차 타고 출장 가면 운임 어떻게 지급돼요?", J, "제10조"),
    ("출장비 나중에 정산해야 하나요?", J, "제8조의2"), ("비행기 출장 항공료는 어떻게 돼요?", J, "제12조"),
    ("근무지 안에서 출장 가면 여비는 어떻게 돼요?", J, "제18조"),
    ("결혼 휴가는 며칠이에요?", V, "별표 2"), ("국내 출장 숙박비 금액은 얼마예요?", J, "별표 2"),
    ("지방데이터청 관할구역은 어디까지예요?", D, "별표 1"),
    ("기획조정관은 무슨 일을 하나요?", D, "제4조"), ("지방데이터청은 어떤 기관이에요?", D, "제15조"),
    ("운영지원과는 뭘 담당해요?", D, "제6조"), ("국가데이터인재개발원은 어떤 곳이에요?", D, "제13조"),
]
OFF_TOPIC = ["구내식당 메뉴 뭐야?", "오늘 날씨 어때?", "점심 뭐 먹지?", "주식 추천해줘"]


def hit_rank(hits, law, art):
    for r, h in enumerate(hits, 1):
        if h["law"] == law and h["article"].startswith(art + "("):
            return r
    return None


def main():
    print(f"질문 {len(CASES)}개, 청크 {len(index.load_index()[0])}개\n")
    res = {}
    for mode in ("keyword", "vector", "hybrid"):
        ranks = [hit_rank(index.search(q, k=5, mode=mode), law, art) for q, law, art in CASES]
        res[mode] = ranks
        at = lambda k: sum(1 for r in ranks if r and r <= k) / len(ranks)
        print(f"{mode:8s} hit@1 {at(1):.0%}   hit@3 {at(3):.0%}   hit@5 {at(5):.0%}")
    rej = sum(1 for q in OFF_TOPIC if not index.search(q, k=5))
    print(f"\n무관한 질문 거절(빈 결과): {rej}/{len(OFF_TOPIC)}")
    print("\n[하이브리드 5위 안에 못 찾은 질문]")
    for (q, law, art), r in zip(CASES, res["hybrid"]):
        if not r or r > 5:
            print(f"  - {q}  (정답 {law} {art})")


if __name__ == "__main__":
    main()
