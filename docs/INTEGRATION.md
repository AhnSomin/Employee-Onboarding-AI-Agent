# 통합 메모: 웹앱 + 회의록 엔진

웹앱(FastAPI + React)이 팀원이 만든 회의록 엔진(`src/onboarding_agent`)을 그대로 사용합니다. 엔진의 업무 규칙은 바꾸지 않았습니다.

| 계층 | 위치 | 역할 |
|---|---|---|
| 화면 | `web/src/Meeting.tsx` | 불러오기 → 검토·수정 → 승인·실행 → 현황 |
| API | `backend/meeting_api.py` | JSON 변환만 담당. 추출 결과는 승인 전까지 서버 메모리에 보관 |
| 엔진 | `src/onboarding_agent/meeting`, `actions`, `store`, `integrations` | 추출, 검증, 담당자 규칙, 승인, 실행(멱등), 리마인더 |
| LLM | `src/onboarding_agent/llm` | `LLMClient`(Gemini)와 `OpenAIClient`(OpenAI). `get_client()`가 `LLM_PROVIDER`로 고름 |

## 바뀐 것 (엔진 쪽)
- `llm/openai_client.py` 추가: `run_tool_loop`, `generate_structured`를 OpenAI로 구현. 모델 체인·재시도·서킷브레이커는 `LLMClient`를 그대로 물려받음.
- `config.py`: `LLM_PROVIDER`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_MODEL_FALLBACKS` 추가 (기본 auto: OpenAI 키가 있으면 OpenAI).
- `llm/client.py`: `get_client()`가 provider에 맞는 클라이언트를 돌려줌.

## 확인한 것
- 엔진의 기존 테스트 전부 통과 (실제 Google Sheets 계약 테스트 11개는 자격 증명이 없어 건너뜀).
- OpenAI(gpt-4.1-mini)로 `scripts/eval_meeting.py --repeat 2` 실행: 항목 재현율·정밀도·담당자·기한 정확도 30/30, 거짓 확정 0/20.
  가상 샘플은 개발용이라 낙관적인 세트입니다. 공개 회의록 시험 세트(Gemini 기준 공식 수치는 `docs/meeting/EVALUATION.md`)는 저장소에 없어서 OpenAI로 다시 재지 못했습니다.

## Slack, 캘린더 동작
- **Slack:** `ACTIONS_DRY_RUN=false`이고 `SLACK_BOT_TOKEN`·`SLACK_CHANNEL_ID`가 있으면 실제로 보냅니다. `true`면 보낼 내용만 화면에 보여 줍니다(결과 화면에만 표시).
- **캘린더:** Google 서비스 계정과 캘린더 ID가 설정돼 있으면 Google Calendar에 만들고, 없으면 승인한 항목을 **.ics 파일**로 내려받게 합니다. 파일을 열면 Google·Outlook·Apple 캘린더에 등록됩니다. (`meeting/ics.py`, 설정과 관계없이 항상 내려받을 수 있음)
- 멘션은 `data/roster.yaml`의 `slack_user_id`가 있을 때만 걸립니다. 지금은 모두 비어 있어 이름만 표시합니다. Slack 멤버 ID는 워크스페이스마다 달라서 다른 곳의 ID를 넣으면 멘션이 깨집니다.

## 재알림 연결 (웹 승인 → Google Sheets → GitHub Actions)
웹은 기본으로 회의 정리를 컨테이너 안 SQLite(`data/meetings.db`, 재시작 때 지워짐)에 저장하고, 재알림 배치(`.github/workflows/reminders.yml`)는 Google Sheets만 읽습니다. 웹에서 승인한 항목에도 재알림이 가게 하려면 웹도 같은 시트를 쓰게 설정합니다. 웹은 엔진의 `SheetsStore`를 그대로 씁니다.

1. 새 스프레드시트를 만들어 서비스 계정 이메일(키 JSON의 `client_email`)에 **편집자**로 공유합니다. 서비스 계정이 속한 GCP 프로젝트에서 Google Sheets API를 켭니다.
   - 키는 "API 키" 문자열이 아니라 **서비스 계정 키(JSON)**여야 시트에 쓸 수 있습니다.
   - 예전에 쓰던 시트를 그대로 쓰면 남아 있는 승인 항목에도 재알림이 갑니다. 그 시트를 쓰려면 먼저 `scripts/reset_demo.py`로 비웁니다. 기본 실행은 지울 목록만 보여 주고 `--yes`일 때만 지웁니다. 시트 행과 함께 이 앱이 올린 Slack 메시지와 캘린더 일정도 지우니 목록을 먼저 확인합니다.
2. Render 환경변수에 `STATE_BACKEND=sheets`, `GSHEETS_SPREADSHEET_ID`(시트 URL의 `/d/`와 `/edit` 사이 값), `GOOGLE_SERVICE_ACCOUNT_JSON`을 넣습니다.
   - `GCAL_CALENDAR_ID`는 넣지 않습니다. 넣으면 일정이 .ics 다운로드 대신 Google Calendar 등록으로 바뀌는데, 이건 재알림과 별개로 정할 문제입니다.
   - `STATE_BACKEND`를 비우면 지금처럼 sqlite입니다. sheets인데 시트 값이 빠졌거나 공유가 안 돼 있으면 승인이 503(저장소를 열 수 없음)으로 막힙니다.
3. GitHub 저장소 Secrets 4개를 넣습니다: `GOOGLE_SERVICE_ACCOUNT_JSON`, `GSHEETS_SPREADSHEET_ID`, `SLACK_BOT_TOKEN`, `SLACK_CHANNEL_ID`. Slack 값은 Render와 같아야 합니다. 재알림은 웹이 올린 요약 메시지에 스레드 답글로 달리기 때문입니다.
4. Actions → reminders → Run workflow를 `dry_run`으로 돌려 보낼 내용을 확인합니다. 시연 때는 `now` 입력(예: `2026-10-15T09:00:00+09:00`)으로 기준 시각을 정해 돌립니다.

주의
- 예약 실행은 평일 09:00 KST(UTC cron `0 0 * * 1-5`)이고, GitHub 사정으로 늦을 수 있습니다(2026-10-12에는 13:17에 돌아 4시간 넘게 늦었습니다). 시연은 수동 실행으로 합니다. 예약 실행은 기본 브랜치(main)의 워크플로와 코드로 돕니다.
- sheets로 바꿔도 체크리스트(`data/state.db`)는 컨테이너 안 파일이라 Render가 재시작하면 지워집니다. 시트로 옮겨 가는 것은 회의 정리뿐입니다.
- Google Sheets API 한도는 분당 읽기·쓰기 각 60회입니다. 가짜 시트로 잰 승인·실행 1회의 호출 수는 4건에 읽기 17·쓰기 11, 13건에 읽기 35·쓰기 29입니다. 현황(`/meeting/list`)은 회의 수와 관계없이 읽기 2회입니다.
- 배치는 `requirements-batch.txt`만 설치합니다. 배치가 불러오는 엔진 코드에 외부 패키지를 새로 쓰면 이 파일에도 넣어야 합니다(`tests/meeting/test_batch_requirements.py`가 빠진 것을 잡습니다).

## 데모 데이터 (SSG 랜더스 선수 이름)
웹앱은 `data/demo/`의 명단과 샘플 회의록을 씁니다. 팀원이 만든 원본(`data/roster.yaml`, `data/samples`, `data/eval`)과 테스트는 그대로 두고, `python scripts/make_demo_data.py`가 인물 이름만 바꿔 복사본을 만듭니다(성이 같은 사람끼리의 구분 사례도 최정·최지훈으로 유지). 정답 라벨도 같은 규칙으로 변환돼 `scripts/eval_meeting.py --gold data/demo/meeting_gold.jsonl --samples data/demo/samples`로 같은 평가를 돌릴 수 있습니다. 이름만 바꾼 같은 문제이므로 담당자·기한 정확도(100%)와 거짓 확정(0)은 그대로이고, 모델이 경계 사례("담당자 미정 작업")를 할 일로 뽑을지가 실행마다 달라 재현율은 76~100% 사이에서 흔들립니다(원본 데이터도 93~100%).

## 아직 연결하지 않은 것
- 팀원이 만든 규정 Q&A(`src/onboarding_agent/rag`)는 웹앱에 연결하지 않았습니다. 웹앱의 규정 질의응답은 `backend/rag`를 사용합니다.
- 팀원 Streamlit 화면(`app/`)은 참고용으로 남겨 두었고 배포 이미지에는 넣지 않습니다.
