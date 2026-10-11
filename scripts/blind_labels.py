"""Turn blind-test labels written in plain YAML into the evaluation JSONL.

A blind test uses fictional team minutes written by someone who has not read
the rules. Each minutes file `NAME.txt` sits next to a label file `NAME.yaml`
in data/external/blind/ (git-ignored). Run with:
    uv run python scripts/blind_labels.py --init    # write the label template
    uv run python scripts/blind_labels.py           # check labels, write blind_gold.jsonl
then evaluate with
    uv run python scripts/eval_meeting.py --plan --gold data/external/blind/blind_gold.jsonl \
        --samples data/external/blind
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.loader import decode_bytes, detect_meeting_date, normalize_newlines
from onboarding_agent.meeting.roster import compact

DEFAULT_DIR = REPO_ROOT / "data" / "external" / "blind"
OUTPUT = "blind_gold.jsonl"
TEMPLATE = "_양식.yaml"
RULES_FROZEN_AT = "5f550913574c283dc4e833a0a9265e65e67dba03"
YES = {"예", "네", "true", "yes"}
NO = {"아니오", "아니요", "false", "no"}

TEMPLATE_TEXT = """\
# 블라인드 평가 라벨 양식
# 1) 회의록을 이 폴더에 '이름.txt'로 저장합니다. 규칙 문서와 코드는 보지 않고 씁니다.
# 2) 이 파일을 '이름.yaml'로 복사해, 회의에서 정해진 할 일을 항목마다 적습니다. 아래 예시는 지우고 씁니다.
# 3) uv run python scripts/blind_labels.py 로 평가용 파일(blind_gold.jsonl)을 만듭니다.

회의날짜: 2026-10-08   # 기한 계산의 기준. 비우면 회의록 머리의 날짜를 씁니다.
항목:
  - 할일: 오리엔테이션 자료 보완   # 무엇을 하는지 짧게
    낱말: [오리엔테이션, 자료]    # 이 항목을 알아볼 낱말 1~3개. 회의록에 나온 그대로 씁니다.
    담당자: 김민준                # 회의록만 보고 한 사람으로 정할 수 있을 때만. 아니면 비웁니다.
    기한: 2026-10-14             # 날짜로 정할 수 있을 때만(시각까지면 2026-10-14 15:00). 아니면 비웁니다.
    필수: 예                     # 꼭 뽑아야 하면 예, 뽑아도 되고 안 뽑아도 되면 아니오
"""


def read_minutes(path: Path) -> str:
    text, _ = decode_bytes(path.read_bytes())
    return normalize_newlines(text)


def due_text(value: object, where: str, problems: list[str]) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            datetime.strptime(text, pattern)
            return text
        except ValueError:
            continue
    problems.append(f"{where}: 기한 '{text}'은 2026-10-14 또는 2026-10-14 15:00 꼴로 씁니다.")
    return None


def required(value: object, where: str, problems: list[str]) -> bool:
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in YES:
        return True
    if text in NO:
        return False
    problems.append(f"{where}: 필수는 '예' 또는 '아니오'로 씁니다.")
    return True


def convert_sample(index: int, label_path: Path, minutes_path: Path, problems: list[str]) -> dict | None:
    name = label_path.name
    try:
        raw = yaml.safe_load(label_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        problems.append(f"{name}: YAML 형식 오류 — {error}")
        return None
    text = read_minutes(minutes_path)
    haystack = compact(text)

    meeting_date = raw.get("회의날짜")
    if isinstance(meeting_date, str) and meeting_date.strip():
        try:
            meeting_date = date.fromisoformat(meeting_date.strip())
        except ValueError:
            problems.append(f"{name}: 회의날짜는 2026-10-08 꼴로 씁니다.")
            meeting_date = None
    if not isinstance(meeting_date, date):
        meeting_date = detect_meeting_date(text, date.today())
    if meeting_date is None:
        problems.append(f"{name}: 회의날짜를 적어 주세요(회의록 머리에서도 찾지 못했습니다).")
        return None

    entries = raw.get("항목")
    if not isinstance(entries, list) or not entries:
        problems.append(f"{name}: '항목' 아래에 할 일을 하나 이상 적어 주세요.")
        return None
    items = []
    for number, entry in enumerate(entries, start=1):
        where = f"{name} 항목 {number}"
        if not isinstance(entry, dict):
            problems.append(f"{where}: '할일·낱말·담당자·기한·필수'를 적은 항목이어야 합니다.")
            continue
        task = str(entry.get("할일") or "").strip()
        if not task:
            problems.append(f"{where}: 할일을 적어 주세요.")
        words = entry.get("낱말")
        words = [words] if isinstance(words, str) else words
        if not isinstance(words, list) or not 1 <= len(words) <= 3 or not all(str(w).strip() for w in words):
            problems.append(f"{where}: 낱말은 1~3개를 [낱말1, 낱말2]처럼 씁니다.")
            words = []
        words = [str(w).strip() for w in words]
        for word in words:
            if compact(word) not in haystack:
                problems.append(f"{where}: 낱말 '{word}'이 회의록에 없습니다. 회의록에 나온 그대로 씁니다.")
        owner = entry.get("담당자")
        owner = str(owner).strip() if owner not in (None, "") else None
        if owner and compact(owner) not in haystack:
            problems.append(f"{where}: 담당자 '{owner}'이 회의록에 없습니다.")
        items.append(
            {
                "id": f"b{index}-{number}",
                "keywords": words,
                "owner": owner,
                "due": due_text(entry.get("기한"), where, problems),
                "optional": not required(entry.get("필수"), where, problems),
                "note": task,
            }
        )
    return {
        "sample": minutes_path.name,
        "meeting_date": meeting_date.isoformat(),
        "rules_frozen_at": RULES_FROZEN_AT,
        "label_status": "블라인드 라벨(규칙을 보지 않고 작성)",
        "must_not_extract": [],
        "items": items,
    }


def convert(folder: Path) -> tuple[list[dict], list[str]]:
    """Rows for every labeled minutes file in the folder, and the problems found."""
    problems: list[str] = []
    labels = sorted(p for p in folder.glob("*.yaml") if not p.name.startswith("_"))
    minutes = {p.stem: p for p in folder.glob("*.txt")}
    rows = []
    for index, label in enumerate(labels, start=1):
        source = minutes.get(label.stem)
        if source is None:
            problems.append(f"{label.name}: 같은 이름의 회의록({label.stem}.txt)이 없습니다.")
            continue
        row = convert_sample(index, label, source, problems)
        if row is not None:
            rows.append(row)
    labeled = {p.stem for p in labels}
    for stem in sorted(set(minutes) - labeled):
        problems.append(f"{stem}.txt: 라벨 파일({stem}.yaml)이 없습니다.")
    return rows, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="블라인드 평가 라벨(YAML)을 평가용 jsonl로 바꿉니다.")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--init", action="store_true", help="라벨 양식 파일만 만듭니다")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    folder = args.dir
    if args.init:
        folder.mkdir(parents=True, exist_ok=True)
        template = folder / TEMPLATE
        if not template.exists():
            template.write_text(TEMPLATE_TEXT, encoding="utf-8")
        print(f"양식: {template}")
        return 0
    if not folder.is_dir():
        print(f"폴더가 없습니다: {folder} (--init으로 만듭니다)")
        return 1
    rows, problems = convert(folder)
    if problems:
        print("고칠 곳:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    if not rows:
        print(f"라벨이 있는 회의록이 없습니다: {folder}")
        return 1
    output = folder / OUTPUT
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    items = [item for row in rows for item in row["items"]]
    print(f"{output}: 회의록 {len(rows)}개, 항목 {len(items)}개(필수 {sum(not i['optional'] for i in items)}개)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
