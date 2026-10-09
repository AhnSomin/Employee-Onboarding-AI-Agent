# 시연 대본

기능별로 절을 나눕니다.

## 기능 2 — 회의록 → 액션

### 리허설 체크리스트 (전날·당일 아침)

- [ ] `.env`
  - `GEMINI_MODEL_PRIMARY`·`GEMINI_MODEL_FALLBACKS` 설정
  - `STATE_BACKEND=sheets`
  - `ACTIONS_DRY_RUN`: 리허설 `true` → 본 시연 `false`
  - `FORCE_FALLBACK=false`
  - `data/roster.yaml`의 `slack_user_id` 채움
- [ ] `uv run python scripts/smoke_test.py` — Gemini·Calendar·Sheets·Slack 모두 `[OK]`. Gemini를 모델마다 한 번씩 호출합니다.
- [ ] `uv run python scripts/reset_demo.py`로 지난 데모 데이터 목록 확인. 지울지는 사람이 정하고, 지울 때만 `--yes`.
- [ ] 샘플 파일
  - `data/samples/03_edge_cases.txt`: 엣지케이스. 미확정·검토 필요·회의록 속 AI 지시문.
  - `data/samples/01_structured_minutes.txt`: 승인·실행용.
- [ ] 강제 폴백: 회의록 화면의 "LLM 없이 규칙 기반으로 추출 (시연용 강제 폴백)" 토글을 켜고 한 번 추출해 보고, 다시 끕니다.
- [ ] 리마인더 수동 실행 순서
  1. 앱에서 기한이 다음 근무일인 항목을 승인합니다.
  2. GitHub Actions([psgg123/onboarding-agent-claude](https://github.com/psgg123/onboarding-agent-claude/actions/workflows/reminders.yml)) → reminders → Run workflow: `now`에 그 전 근무일 09:00(예: `2026-10-15T09:00:00+09:00`)을 넣고, `dry_run`을 끕니다.
  3. Slack 회의 요약 스레드에 `[D-1]` 답글이 하나 왔는지 확인합니다.
  4. 같은 입력으로 한 번 더 실행해 "보낼 리마인더 없음"을 확인합니다.
- [ ] Gemini 한도: 시연 전날부터 평가 스크립트를 돌리지 않습니다. 필요하면 `--plan`으로 호출 수만 봅니다.
- [ ] 브라우저 탭: 앱, Slack 채널, 캘린더, GitHub Actions를 미리 열어 둡니다.

### 시연 대본 (스펙 15절 M4)

1. **엣지케이스 회의록 업로드** → 미확정·검토 필요 표시를 확인합니다. "박 주무관"은 후보가 둘이라 미확정이고, AI에게 지시하는 문장은 실행되지 않고 경고로만 보입니다.
2. **담당자·기한 채우기** → 미리보기(캘린더 일정·Slack 메시지) → 승인 → 캘린더와 Slack에서 확인합니다.
3. **중복 없음**: 승인 버튼을 다시 누르고 새로고침해도 일정과 메시지가 하나씩만 있음을 보여 줍니다.
4. **액션 현황 → 리마인더 미리보기**: 기준 날짜를 기한 전 근무일로 바꾸면 `[D-1]` 메시지가 보입니다. 보내지는 않습니다.
5. **GitHub Actions 수동 실행**(`now` 입력) → Slack 회의 요약 스레드에 재알림이 옵니다. 같은 입력으로 다시 돌리면 오지 않습니다.
6. **강제 폴백 토글** → LLM 없이 규칙 기반으로 추출합니다. 모든 항목이 미확정이라 사람이 채워야 승인할 수 있습니다.

### 장애 대응

| 상황 | 대응 |
|---|---|
| Gemini 한도 초과: 화면에 "Gemini 사용 한도를 넘었습니다" | 규칙 기반으로 시연을 이어갑니다. 미확정 항목을 사람이 채우는 흐름을 보여 주고 대본 6번을 앞당깁니다. |
| Slack·Calendar 실패 | `ACTIONS_DRY_RUN=true`로 바꿔 앱을 다시 시작하고, 미리보기와 DRY_RUN 결과로 시연합니다. 실패한 항목만 다시 시도하는 버튼도 보여 줄 수 있습니다. |
| Sheets 접근 실패 | `STATE_BACKEND=sqlite`로 로컬 시연합니다. 리마인더는 `uv run python scripts/run_reminders.py --now … --dry-run`으로 대신 보여 줍니다. |
| GitHub Actions 지연·실패 | 로컬에서 `run_reminders.py --now …`로 같은 결과를 보여 줍니다. 예약 실행은 몇 분~수십 분 늦을 수 있습니다. |
| 네트워크 끊김 | 강제 폴백 토글과 `ACTIONS_DRY_RUN=true`로 오프라인 시연을 합니다. |

### 대체 시연 대본 (연동 장애 시: DRY_RUN 미리보기 + 로컬 리마인더 CLI)

Calendar·Slack·Sheets 중 하나라도 안 되면 이 대본으로 바꿉니다. 실제로 만들거나 보내지는 않지만, 같은 화면 흐름과 같은 중복 방지를 보여 줍니다. `.env`는 고치지 않고 명령줄 환경변수로만 바꿉니다.

**준비**: 앱을 끄고, 터미널에서 DRY_RUN과 로컬 저장소로 다시 시작합니다.

```bash
ACTIONS_DRY_RUN=true STATE_BACKEND=sqlite uv run streamlit run app/main.py
```

Gemini도 안 되면 회의록 화면의 강제 폴백 토글을 켭니다.

**대본**

1. **엣지케이스 회의록 업로드** → 미확정·검토 필요 표시를 확인합니다(본 대본 1번과 같음).
2. **담당자·기한 채우기 → 미리보기 → 승인** → 화면의 "DRY_RUN — 실제 등록·발송 없음" 표시와 "DRY_RUN 페이로드"를 보여 줍니다. 만들 캘린더 일정과 보낼 Slack 메시지가 그대로 나옵니다.
3. **중복 없음**: 승인을 다시 누르면 일정·메시지가 "건너뜀(이미 …)"으로 나옵니다.
4. **액션 현황 → 리마인더 미리보기**: 기준 날짜를 기한 전 근무일로 바꾸면 `[D-1]` 메시지가 보입니다.
5. **로컬 리마인더 CLI**: 두 번째 터미널에서 GitHub Actions가 매일 아침 돌리는 것과 같은 명령을 실행합니다. `--now`에는 기한 전 근무일 09:00을 넣습니다.

   ```bash
   ACTIONS_DRY_RUN=true STATE_BACKEND=sqlite uv run python scripts/run_reminders.py --now 2026-10-15T09:00:00+09:00 --dry-run
   ```

   `[D-1]` 메시지 본문이 출력됩니다. 같은 명령을 한 번 더 실행하면 "보낼 리마인더 없음"이 나옵니다(중복 방지).
6. **강제 폴백 토글**(본 대본 6번).

할 말: "실제 연동에서는 같은 흐름으로 캘린더 일정과 Slack 메시지가 만들어지고, 리마인더는 GitHub Actions가 평일 09:00에 이 명령으로 보냅니다."
