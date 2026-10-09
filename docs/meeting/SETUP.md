# 기능 2 설정 가이드

순서: `.env` 준비 → Google → Slack → `smoke_test` → (배포) GitHub Secrets. 비밀값은 커밋하지 않습니다(`.env`, `secrets/`는 git 제외).

## 1. `.env`

`.env.example`을 `.env`로 복사해 채웁니다.

| 변수 | 설명 | 시연 권장값 |
|---|---|---|
| `GEMINI_API_KEY` | Gemini API 키 | — |
| `GEMINI_MODEL_PRIMARY` | 먼저 쓸 모델. 비우면 `smoke_test`가 쓸 수 있는 모델 목록을 보여 줌 | 예: `gemini-3.5-flash` |
| `GEMINI_MODEL_FALLBACKS` | 실패 시 차례로 쓸 모델(쉼표 구분) | 예: `gemini-3.5-flash-lite,gemini-3.8-flash` |
| `LLM_TIMEOUT_SEC` | 모델 호출 제한 시간(초) | `60` |
| `FORCE_FALLBACK` | `true`면 모델 없이 규칙 기반 추출 | `false` (화면 토글로도 켤 수 있음) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 서비스 계정 키: 파일 경로·JSON 문자열·base64 | `secrets/google-service-account.json` |
| `GCAL_CALENDAR_ID` | 일정을 만들 캘린더 ID | 데모 캘린더 ID |
| `GSHEETS_SPREADSHEET_ID` | 상태를 저장할 스프레드시트 ID | 데모 시트 ID |
| `SLACK_BOT_TOKEN` | Slack 봇 토큰(`xoxb-`) | — |
| `SLACK_CHANNEL_ID` | 알림 채널 ID(`C…`) | 데모 채널 ID |
| `STATE_BACKEND` | `sqlite`(로컬) 또는 `sheets` | `sheets` — 리마인더 배치가 같은 상태를 봐야 함 |
| `SQLITE_PATH` | SQLite 파일 위치 | `data/state.db` |
| `ACTIONS_DRY_RUN` | `true`면 Calendar·Slack 대신 미리보기만 | 리허설 `true`, 본 시연 `false` |
| `TIMEZONE` | 모든 날짜·시각의 기준 | `Asia/Seoul` |
| `REMINDER_STAGES` | 보낼 리마인더 단계 | `D-1,D-day,overdue` |
| `MEETING_MAX_CHARS` | 회의록 최대 글자 수 | `100000` |
| `ROSTER_PATH` | 명단 파일 | `data/roster.yaml` |

값을 바꾼 뒤에는 앱을 다시 시작합니다.

## 2. Google (서비스 계정·캘린더·시트)

1. Google Cloud 콘솔에서 프로젝트를 고르고 **Google Calendar API**와 **Google Sheets API**를 사용 설정합니다. Drive API는 필요 없습니다.
2. IAM 및 관리자 → 서비스 계정 → 서비스 계정을 만들고, 키(JSON)를 만들어 `secrets/google-service-account.json`에 저장합니다. 서비스 계정 이메일(`…@….iam.gserviceaccount.com`)을 적어 둡니다.
3. **캘린더**: Google Calendar에서 데모용 캘린더를 새로 만듭니다 → 설정 및 공유 → 특정 사용자 또는 그룹과 공유 → 서비스 계정 이메일을 **"일정 변경"** 권한으로 추가합니다. "캘린더 통합"의 캘린더 ID를 `GCAL_CALENDAR_ID`에 넣습니다. 서비스 계정 자신의 캘린더는 사람이 볼 수 없으니 쓰지 않습니다.
4. **시트**: 빈 스프레드시트를 만들고 서비스 계정 이메일을 **편집자**로 공유합니다. 주소의 `/d/<ID>/` 부분을 `GSHEETS_SPREADSHEET_ID`에 넣습니다. `meetings`·`action_items` 워크시트와 머리글은 처음 실행할 때 앱이 만듭니다.
5. 서비스 계정은 참석자를 초대할 수 없어 일정에 참석자를 넣지 않습니다. 담당자는 일정 제목·설명과 Slack 멘션으로 알립니다.

## 3. Slack

1. <https://api.slack.com/apps> → Create New App → From scratch → 데모 워크스페이스를 고릅니다.
2. OAuth & Permissions → **Bot Token Scopes**에 추가합니다.

   | 스코프 | 쓰는 곳 |
   |---|---|
   | `chat:write` | 회의 요약, 추가 승인 답글, 리마인더 발송, `reset_demo`의 봇 메시지 삭제 |
   | `channels:read` (공개 채널) 또는 `groups:read` (비공개 채널) | `smoke_test`의 채널 확인 |
   | `chat:write.public` (선택) | 봇을 초대하지 않은 공개 채널에도 발송 |

   채널 기록 조회 스코프(`channels:history` 등)는 필요 없습니다. `reset_demo`는 앱이 저장한 메시지 ts로만 지웁니다.
3. Install to Workspace → **Bot User OAuth Token**(`xoxb-…`)을 `SLACK_BOT_TOKEN`에 넣습니다.
4. 알림 채널에서 `/invite @앱이름`으로 봇을 초대하고, 채널 정보 맨 아래의 채널 ID(`C…`)를 `SLACK_CHANNEL_ID`에 넣습니다.
5. 멘션용으로 각 사람의 멤버 ID(프로필 → ⋮ → 멤버 ID 복사, `U…`)를 `data/roster.yaml`의 `slack_user_id`에 적습니다. 비워 두면 이름만 표시합니다.

## 4. 점검

```bash
uv run python scripts/smoke_test.py
```

Gemini·Google Calendar·Google Sheets·Slack이 모두 `[OK]`여야 합니다. `[SKIP]`은 설정이 비었다는 뜻이고, `[FAIL]`에는 고칠 곳이 한국어로 나옵니다. 일정을 만들거나 메시지를 보내지는 않습니다. 다만 Gemini는 모델마다 한 번씩 호출합니다.

## 5. GitHub Secrets (리마인더 배치)

작업 저장소는 [psgg123/onboarding-agent-claude](https://github.com/psgg123/onboarding-agent-claude)이고, 리마인더 예약 실행은 이 저장소의 기본 브랜치 `main`에서 돕니다. Secrets는 저장소 주인이 직접 등록합니다: [Settings → Secrets and variables → Actions](https://github.com/psgg123/onboarding-agent-claude/settings/secrets/actions) → New repository secret.

| 이름 | 값 |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 서비스 계정 JSON 파일 내용 전체 |
| `GSHEETS_SPREADSHEET_ID` | 앱과 같은 스프레드시트 ID |
| `SLACK_BOT_TOKEN` | `xoxb-…` |
| `SLACK_CHANNEL_ID` | `C…` |

- 배치는 Google Sheets에서 상태를 읽습니다. 앱도 `STATE_BACKEND=sheets`로 써야 리마인더가 앱에서 승인한 항목을 봅니다.
- Secrets가 비어 있으면 워크플로는 로그에 "설정 필요"와 빠진 이름을 남기고 성공으로 끝납니다.
- 예약 실행은 기본 브랜치(main)에 있는 워크플로만 돕니다(평일 09:00 KST = 00:00 UTC, 늦어질 수 있음).
- 수동 실행: [Actions → reminders](https://github.com/psgg123/onboarding-agent-claude/actions/workflows/reminders.yml) → Run workflow. `now`에 기준 시각(예: `2026-10-16T09:00:00+09:00`)을, `dry_run`에 발송 여부를 넣습니다.
