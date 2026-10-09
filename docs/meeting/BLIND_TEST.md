# 블라인드 테스트 작성 안내

1. 규칙 문서([RULES.md](RULES.md))와 코드를 보지 않은 사람이 가상 팀 회의록을 써서 `data/external/blind/이름.txt`로 저장합니다. 실제 인물과 실제 회의 내용은 쓰지 않습니다.
2. `uv run python scripts/blind_labels.py --init`으로 만든 `_양식.yaml`을 `이름.yaml`로 복사합니다. 회의에서 정해진 할 일마다 할일·낱말·담당자·기한·필수를 적습니다. 회의록만 보고 한 사람이나 한 날짜로 정할 수 없으면 담당자와 기한은 비웁니다.
3. `uv run python scripts/blind_labels.py`로 `blind_gold.jsonl`을 만듭니다. "고칠 곳"이 나오면 고칩니다. 그다음 `uv run python scripts/eval_meeting.py --plan --gold data/external/blind/blind_gold.jsonl --samples data/external/blind`로 호출 수를 확인하고, `--plan`을 `--details --compare-owner-rules --save-raw <파일>`로 바꿔 한 번 실행합니다.
4. 라벨은 실행 전에 확정하고, 결과를 본 뒤에는 라벨도 규칙도 바꾸지 않습니다. 이 폴더는 git에서 제외됩니다.
