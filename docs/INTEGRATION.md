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

## 시뮬레이션과 실제 전송
`ACTIONS_DRY_RUN=true`(기본)면 Calendar·Slack을 부르지 않고 보낼 내용만 화면에 보여 줍니다. 실제 전송에 필요한 값은 `.env.example`의 Google·Slack 항목이며, 설정 방법은 `docs/meeting/SETUP.md`를 따릅니다.

## 아직 연결하지 않은 것
- 팀원이 만든 규정 Q&A(`src/onboarding_agent/rag`)는 웹앱에 연결하지 않았습니다. 웹앱의 규정 질의응답은 `backend/rag`를 사용합니다.
- 팀원 Streamlit 화면(`app/`)은 참고용으로 남겨 두었고 배포 이미지에는 넣지 않습니다.
