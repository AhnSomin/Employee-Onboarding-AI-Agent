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

## 데모 데이터 (SSG 랜더스 선수 이름)
웹앱은 `data/demo/`의 명단과 샘플 회의록을 씁니다. 팀원이 만든 원본(`data/roster.yaml`, `data/samples`, `data/eval`)과 테스트는 그대로 두고, `python scripts/make_demo_data.py`가 인물 이름만 바꿔 복사본을 만듭니다(성이 같은 사람끼리의 구분 사례도 최정·최지훈으로 유지). 정답 라벨도 같은 규칙으로 변환돼 `scripts/eval_meeting.py --gold data/demo/meeting_gold.jsonl --samples data/demo/samples`로 같은 평가를 돌릴 수 있습니다. 이름만 바꾼 같은 문제이므로 담당자·기한 정확도(100%)와 거짓 확정(0)은 그대로이고, 모델이 경계 사례("담당자 미정 작업")를 할 일로 뽑을지가 실행마다 달라 재현율은 76~100% 사이에서 흔들립니다(원본 데이터도 93~100%).

## 아직 연결하지 않은 것
- 팀원이 만든 규정 Q&A(`src/onboarding_agent/rag`)는 웹앱에 연결하지 않았습니다. 웹앱의 규정 질의응답은 `backend/rag`를 사용합니다.
- 팀원 Streamlit 화면(`app/`)은 참고용으로 남겨 두었고 배포 이미지에는 넣지 않습니다.
