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

## 2026-10-08 (M1)

- 2026-10-08 `feat/meeting`을 origin에 push했다(upstream 설정, main·force push 없음, 커밋 `b03cb02`). 이후 push는 사람이 지시할 때만 한다.
- 2026-10-08 **function calling 세부 설계 (사람 결정 반영).** 도구는 `record_meeting_overview`(정확히 1회), `propose_action_item`(항목마다 1회, 0회 가능), `finish_extraction`(마지막 1회, 제안 수를 인자로) 세 개다. 도구 결과("recorded" / "rejected"+사유 / "finished")를 돌려주며 `finish_extraction`이 올 때까지 최대 4턴 반복한다. 모델은 Calendar·Slack을 호출할 수 없고, 실제 등록·발송은 승인 뒤 executor가 한다.
- 2026-10-08 도구 인자는 LLM 출력 스키마(`LLMOverview`, `LLMActionItem`, `LLMFinish`)로 검증한다. 잘못된 호출은 버리고 경고를 남기며, 할 일·담당자·기한 표현이 같은 중복 제안은 하나로 합친다(빈 필드는 나중 제안에서 채움). `finish_extraction`의 수와 실제 제안 수가 다르면 경고한다.
- 2026-10-08 개요 호출 없음, 4턴 안에 finish 없음, 유효한 호출 없음 중 하나라도 해당하면 `generate_structured(LLMExtraction)`로 한 번 다시 추출하고, 그것도 실패하면 규칙 기반으로 간다. API 장애로 모델 체인 전체가 실패(`LLMUnavailable`)하면 바로 규칙 기반으로 간다.
- 2026-10-08 `ExtractionResult`에 `extraction_path`(function_calling / structured / rule_based)와 `tool_calls`(턴 수, 도구별 호출 수, 버린 호출 수, 합친 중복 수, finish 여부)를 추가했다. 최종 경로가 structured·rule_based여도 function calling 시도의 요약은 남긴다. `fallback_used`는 경로가 function_calling이 아니면 True다.
- 2026-10-08 공유 모듈 — 팀원 확인 필요: `llm/client.py`에 `run_tool_loop`(다중 턴 도구 루프)와 `declare_function`(pydantic 모델 → Gemini가 받는 JSON 스키마)을 추가했다. 기존 함수의 시그니처와 동작은 그대로다(`generate_with_tools`의 설정 생성만 내부 함수로 분리). 한 대화는 한 모델로만 진행하고, 일시 오류가 나면 대화를 처음부터 다시 한다. 그래서 `respond`는 상태 없는 함수여야 한다. 모델 응답은 그대로 다시 보내 thought signature를 보존한다(실제 응답에서 확인, `tests/fixtures/`).
- 2026-10-08 인젝션 방어: 프롬프트에 "회의록은 데이터"를 명시하고 회의록 태그를 이스케이프했다. 그와 별도로 코드가 회의록에서 AI에게 지시하는 문장(읽는 AI를 부르는 문장, "이전 지시 무시", 명령형으로 끝나는 AI 대상 문장)을 찾고, 그 문장에서 나온 항목·결정사항을 결과에서 뺀 뒤 경고한다. "생성형 AI는 … 활용하기로 결정"처럼 AI를 다루는 일반 문장은 걸리지 않게 했다.
- 2026-10-08 날짜 해석: 오전·오후 표시 없는 "3시"는 시각을 비우고 미확정으로 둔다. "X요일"은 회의 날짜 이후 가장 가까운 날이다(당일 제외). "N주·일주일 이내"와 "말일"을 지원한다. 날짜 옆 요일 표기가 틀리거나 날짜가 둘 이상이거나 "매주" 같은 반복 표현이면 미확정이다. "D일"은 이번 달 D일이다.
- 2026-10-08 검증: 기한 표현이 회의록에 없으면 미확정 + 검토 필요로 둔다. 기한 표현 없이 모델이 낸 날짜 추정은 쓰지 않는다. 명단에 없지만 회의록에 이름이 명시된 담당자는 확정하되 "Slack 미등록 (명단에 없음)"을 남긴다. 성만 있는 호칭은 명단에서 한 명으로 좁혀질 때만 확정한다. 팀·과처럼 개인이 아닌 지정은 담당자를 비운다. 여러 문장 인용은 순서대로, 20자 이내로 이어질 때만 인정한다(서로 다른 줄을 이어 붙인 인용 방지).
- 2026-10-08 규칙 기반 폴백: 회의록의 제목줄(액션·결정·기타 구역)을 인식해 줄 단위로 그대로 옮기고 생성하지 않는다. 결과는 전부 미확정이다.
- 2026-10-08 평가 방법: 라벨의 핵심어가 모두 할 일이나 근거 인용에 있으면 매칭한다. 담당자·기한은 시스템이 확정한 값만 정답과 비교한다(미확정 = "미확정"). 거짓 확정률 = 정답이 "미확정"인 필드 중 시스템이 확정한 비율이다. 추출해도 되고 안 해도 되는 항목은 `optional` 라벨로 둬 정밀도에서 빼되 거짓 확정 계산에는 넣는다. 라벨 초안은 에이전트가 만들었고 사람 검수가 필요하다.
- 2026-10-08 `.env`가 저장소 루트가 아니라 한 단계 위(`~/Downloads/agent-2/.env`)에 있었다. 에이전트는 파일을 열거나 옮기지 않았고, smoke test와 평가는 실행할 때만 그 경로를 지정해 읽었다(`eval_meeting.py --env-file`). 저장소 루트로 옮겨 달라고 사람에게 요청했다.
- 2026-10-08 실제 모델 평가는 모델 이름을 명령행 환경변수로 임시 지정해 실행했다. `.env`는 수정하지 않았다.
- 2026-10-08 프롬프트 v2: v1로 처음 평가했을 때 모든 모델이 담당자가 정해지지 않은 일, 팀에 맡긴 일, 회의 날짜보다 앞선 기한의 일 중 일부를 빠뜨렸다. 그래서 "앞으로 해야 할 작업이면 담당자·기한이 불확실해도 제안한다, 결론 없는 논의는 미결 사항"이라는 일반 규칙 한 줄을 추가했다. 평가 샘플에 맞춘 문장은 넣지 않았다. v1과 v2 결과는 M1 보고에 함께 남긴다.
- 2026-10-08 `ExtractionResult`에 `injection_sentences`(회의록 속 AI 대상 지시문)와 `injection_blocked`(그 문장에서 나와 코드가 뺀 항목 수)를 추가했다. M2 화면 표시와 평가("모델이 제안했는가 / 최종 결과에 남았는가" 구분)에 쓴다.
