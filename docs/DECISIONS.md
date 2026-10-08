# 결정·가정 기록

한 줄에 하나씩, 날짜와 함께 적는다. "공유 모듈 — 팀원 확인 필요"는 두 기능이 같이 쓰는 파일이라 팀원 검토가 필요한 항목이다.

## 2026-10-08 (M0)

- 2026-10-08 작업 저장소는 팀 저장소 `AhnSomin/Employee-Onboarding-AI-Agent`, 작업 브랜치는 `feat/meeting`. main 병합은 사람이 지시할 때만 한다.
- 2026-10-08 기능 2 지시서를 `docs/AGENT_SPEC_MEETING.md`로 저장소에 추가했다.
- 2026-10-08 **기능 2의 LLM 호출 방식은 README대로 Gemini function calling (사람 결정).** 스펙 4·8.2·11절의 "구조화 출력 1회"를 대체한다. 승인 없는 실행 금지는 유지한다: 모델의 함수 호출은 제안으로만 기록하고, Calendar 등록·Slack 발송은 사용자가 승인한 뒤 코드가 실행한다. 도구 구성 세부안은 M1 계획에서 확인받는다.
- 2026-10-08 공유 모듈 — 팀원 확인 필요: 저장소에 공유 모듈이 없어 최소 버전을 만들었다 — `pyproject.toml`(uv, src 레이아웃), `uv.lock`, `.gitignore`, `.env.example`, `config.py`, `metrics.py`, `llm/client.py`, `app/main.py`, `scripts/smoke_test.py`, `tests/conftest.py`.
- 2026-10-08 공유 모듈 — 팀원 확인 필요: `llm/client.py`는 스펙의 `generate_structured`와 함께 function calling용 `generate_with_tools`를 제공한다. 모델이 돌려준 함수 호출은 실행하지 않고 반환만 한다 (SDK 자동 함수 호출 비활성화).
- 2026-10-08 LLM 재시도: 모델당 2회 시도(지수 백오프 1초부터 최대 8초), 429·408·5xx·타임아웃·출력 파싱 실패만 재시도하고 나머지 오류는 바로 다음 모델로 넘어간다. 실패한 모델은 5분간 건너뛴다. SDK 자체 재시도는 기본값(재시도 없음)으로 두어 이중 재시도를 피한다.
- 2026-10-08 공유 모듈 — 팀원 확인 필요: `app/main.py`의 Q&A 항목은 main.py 안의 자리표시 함수로 두었다. 팀원 담당 파일(Q&A 페이지)을 미리 만들지 않기 위해서이며, Q&A 페이지가 생기면 `st.Page` 한 줄만 바꾸면 된다.
- 2026-10-08 공유 모듈 — 팀원 확인 필요: 설정 로더는 pydantic-settings 없이 python-dotenv와 pydantic으로 구현했다 (의존성 최소화). `st.secrets`는 streamlit이 이미 import된 경우에만 조회해 배치 경로에서 streamlit을 불러오지 않는다.
- 2026-10-08 의존성 추가: `tzdata`(Windows 한정) — Windows에는 시스템 시간대 DB가 없어 `ZoneInfo("Asia/Seoul")`가 실패하기 때문이다.
- 2026-10-08 `ActionItem` 기본값은 `status=draft`, `owner_status`·`due_status=unconfirmed`이다. 값 없이 "confirmed"가 되지 않도록 모델 검증기를 둔다 (거짓 확정 방지).
- 2026-10-08 `StateStore.update_*`는 스펙 시그니처 `update_item(item_id, **fields)`를 그대로 따른다. 모르는 필드는 거부하고, 바꾼 값은 모델로 다시 검증한 뒤 저장한다.
- 2026-10-08 `SqliteStore`는 모델 전체를 JSON으로 저장하고 `meeting_id`·`status`만 필터용 컬럼으로 둔다. 목록은 삽입 순서로 반환해 Sheets 백엔드와 순서를 맞춘다.
- 2026-10-08 smoke test: 운영 설정값과 모델명은 그대로, 비밀값과 ID(캘린더·시트·채널)는 앞 4자리만 표시한다. 서비스 계정 이메일은 캘린더·시트 공유에 필요하므로 실패 안내에 그대로 보여준다.
- 2026-10-08 smoke test(Slack): 봇 스코프가 `chat:write`뿐이면 `conversations.info`가 `missing_scope`로 실패하므로, 채널 확인은 건너뛰고 `auth.test` 결과로 OK를 표시한다.
- 2026-10-08 smoke test(Gemini): 지정 모델마다 함수 호출 1회를 시험한다. 기능 2가 function calling에 의존하기 때문이다.
- 2026-10-08 가정: `.env`와 `secrets/`가 아직 없다. 모든 연동은 SKIP이며 DRY_RUN·mock으로 진행한다 — TODO(credential).
