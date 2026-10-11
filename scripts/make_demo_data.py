"""웹앱 데모용 데이터를 만든다: 팀원의 가상 명단·샘플·정답 라벨의 인물 이름을 SSG 랜더스 선수 이름으로 바꿔 data/demo/에 쓴다.
원본(data/roster.yaml, data/samples, data/eval)과 테스트는 건드리지 않는다. 실행: python scripts/make_demo_data.py

이름은 성이 같은 사람끼리의 구분(원래 박지훈·박소연)이 유지되도록 성을 맞춰 바꾼다.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL = {"김민준": "김재환", "이서연": "이지영", "박지훈": "최정", "박소연": "최지훈", "최유진": "오태곤", "정하은": "정준재"}
SURNAME = {"김": "김", "이": "이", "박": "최", "최": "오", "정": "정"}   # 최 → 오: 성 "한"은 "한 주무관"이 "한(하나의) 주무관"으로 읽혀 피한다    # '박 주무관'(박지훈·박소연 중 누구?) → '최 주무관'(최정·최지훈 중 누구?)
GIVEN = {"민준": "재환"}                                                   # 이름만 쓴 별칭
_PATTERN = re.compile(
    "|".join(FULL) + r"|(?<![가-힣])([김이박최정]) ?(주무관|사무관|서기관)(님)?|(?<![가-힣])(민준)(?![가-힣])"
)


def convert(text: str) -> str:
    def sub(m: re.Match) -> str:
        if m.group(0) in FULL:
            return FULL[m.group(0)]
        if m.group(4):
            return GIVEN[m.group(4)]
        return SURNAME[m.group(1)] + m.group(0)[1:]       # 성만 바꾸고 직함·공백·님은 그대로
    return _PATTERN.sub(sub, text)


def main() -> None:
    out = ROOT / "data" / "demo"
    (out / "samples").mkdir(parents=True, exist_ok=True)
    for src in sorted((ROOT / "data" / "samples").glob("*.txt")):
        if src.name.startswith("03_"):     # 예외 사례 샘플은 데모에서 뺀다 (원본·평가용으로만 유지)
            continue
        (out / "samples" / src.name).write_text(convert(src.read_text(encoding="utf-8")), encoding="utf-8")
    (out / "roster.yaml").write_text(convert((ROOT / "data" / "roster.yaml").read_text(encoding="utf-8")), encoding="utf-8")
    gold = (ROOT / "data" / "eval" / "meeting_gold.jsonl").read_text(encoding="utf-8")
    (out / "meeting_gold.jsonl").write_text(convert(gold), encoding="utf-8")
    print("data/demo 생성:", sorted(p.name for p in out.rglob("*") if p.is_file()))


if __name__ == "__main__":
    main()
