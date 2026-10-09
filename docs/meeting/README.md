# 기능 2 — 회의록 → 액션

회의록 텍스트를 올리면 요약, 결정사항, 액션 아이템(할 일·담당자·기한·근거)을 뽑습니다. 사람이 확인·수정해 승인하면 Google Calendar 일정과 Slack 알림을 만들고, 승인한 항목은 기한 전후로 Slack 스레드에서 다시 알립니다.

- 작업 저장소: [psgg123/onboarding-agent-claude](https://github.com/psgg123/onboarding-agent-claude) — 기본 브랜치 `main`(리마인더 예약 실행 기준)
- 설정: [SETUP.md](SETUP.md) — Google 서비스 계정·캘린더·시트 공유, Slack 앱과 스코프, `.env`, GitHub Secrets
- 시연: [../DEMO.md](../DEMO.md)의 기능 2
- 적용 전 측정 절차: [MEASUREMENT.md](MEASUREMENT.md)
- 설계 결정과 규칙 동결 지점: [../DECISIONS.md](../DECISIONS.md)

## 흐름

1. **입력**: `.txt` 회의록(UTF-8·CP949). 제목과 회의 날짜를 찾아 채웁니다.
2. **추출**: Gemini function calling(기록 전용 도구 3개, 최대 4턴) → 실패하면 구조화 출력 1회 → 그래도 안 되면 규칙 기반. 화면에 경로와 이유(예: "Gemini 사용 한도를 넘었습니다")를 보여 줍니다.
3. **결정적 검증**: 근거 인용 원문 대조, 명단 매칭, 한국어 날짜 해석(회의 날짜 기준·월요일 시작), 회의록 속 AI 지시문 차단, 담당자 확정 기준(기관·집단·약한 약속은 미확정).
4. **확인·수정·승인**: 미확정·검토 필요 표시를 보고 사람이 채운 뒤 승인합니다.
5. **실행**: 항목마다 캘린더 일정 1개, 회의마다 Slack 요약 1개. 다시 눌러도, 새로고침해도 중복이 생기지 않고 실패한 것만 다시 시도합니다.
6. **리마인더**: GitHub Actions가 평일 09:00(KST)에 D-1·D-day·기한 지남을 각각 한 번씩 회의 요약 스레드에 답글로 보냅니다.

## 실행 명령

| 목적 | 명령 |
|---|---|
| 설치 | `uv sync` |
| 설정 점검 (Gemini 호출 3회) | `uv run python scripts/smoke_test.py` |
| 앱 | `uv run streamlit run app/main.py` |
| 테스트 | `uv run pytest` |
| 추출 평가 — 예상 호출 수만 | `uv run python scripts/eval_meeting.py --plan` |
| 추출 평가 (원 출력 저장) | `uv run python scripts/eval_meeting.py --details --save-raw logs/eval_raw.jsonl` |
| 저장된 출력 다시 채점 (호출 0회) | `uv run python scripts/eval_meeting.py --rescore logs/eval_raw.jsonl --details` |
| 리마인더 수동 실행 | `uv run python scripts/run_reminders.py --now 2026-10-16T09:00:00+09:00 [--dry-run]` |
| 지표 | `uv run python scripts/report_metrics.py --since 2026-10-12` |
| 데모 데이터 목록 / 삭제 | `uv run python scripts/reset_demo.py` / `uv run python scripts/reset_demo.py --yes` |

리마인더 배치만 쓰는 환경(GitHub Actions)은 `requirements-batch.txt`만 설치하고 `PYTHONPATH=src`로 실행합니다.

## 한계

- 입력은 텍스트 회의록만 받습니다(음성·PDF·docx 없음).
- 담당자는 명단(`data/roster.yaml`)으로 맞춥니다. 명단에 없는 사람은 Slack 멘션 없이 이름만 씁니다. 후보가 여럿이면 미확정입니다.
- 기한은 한국어 날짜 표현 규칙으로 해석합니다. 공휴일은 고려하지 않고, "내년"·"월말"·"다음 회의 전" 같은 모호한 표현은 미확정입니다.
- 담당자 확정 기준은 코드 규칙과 설정 파일(`data/meeting_owner_rules.yaml`)입니다. 기관장급 화자는 '제가 직접 ~하겠다'처럼 '직접'이 있을 때만 개인으로 확정합니다. 목록에 없는 직함(예: 정부 위원회 위원장, 대사)은 개인으로 볼 수 있습니다.
- 근거 인용을 회의록에서 확인하지 못하면 담당자·기한을 확정하지 않습니다. "…"로 줄인 인용은 같은 발언 안에서만 인정합니다.
- 서비스 계정은 참석자를 초대하지 않습니다(일정에 참석자 없음).
- 완료·취소는 상태만 바꾸고, 이미 만든 캘린더 일정과 Slack 메시지는 그대로 둡니다.
- 예약 리마인더는 main에 병합된 뒤부터 돌고, 실행이 몇 분~수십 분 늦을 수 있습니다.
- Sheets 저장소는 잠금이 없어 여러 사람이 동시에 승인하면 충돌할 수 있습니다(데모 범위).
- Gemini 한도를 넘으면 규칙 기반으로만 추출합니다. 이때 모든 항목은 미확정이라 사람이 채워야 승인할 수 있습니다.

## 데이터 출처

- 데모·테스트용 회의록(`data/samples/`)과 명단은 모두 가상 데이터입니다.
- 추출 평가 일부(개발·시험 세트)에 아래 공개 데이터를 평가 입력으로만 썼습니다. 원본·발췌·라벨은 저장소에 넣지 않습니다(`data/external/`, git 제외).

> 이 프로젝트는 과학기술정보통신부의 재원으로 한국지능정보사회진흥원의 지원을 받아 구축된 '국회 회의록 기반 지식검색 데이터'를 활용했습니다. 데이터는 AI 허브(aihub.or.kr)에서 내려받을 수 있습니다.
>
> (AI Hub 페이지에서 문구 확인 필요)
