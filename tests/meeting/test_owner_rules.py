"""Owner rules: institutional and weak promises stay unconfirmed (DECISIONS.md, 담당자 확정 기준).

All names and minutes here are fictional.
"""

from datetime import date

import pytest

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.models import LLMActionItem, LLMExtraction
from onboarding_agent.meeting.owner_rules import (
    OwnerRules,
    OwnerRulesError,
    Speech,
    default_owner_rules,
    load_owner_rules,
    speaker_label,
)
from onboarding_agent.meeting.roster import load_roster
from onboarding_agent.meeting.validate import normalize_space, validate_extraction

ROSTER = load_roster(REPO_ROOT / "data" / "roster.yaml")
RULES = load_owner_rules()
MEETING_DATE = date(2026, 10, 8)
HEARING = """회의명: 가상위원회 제1차 회의 (가상 회의록)
일시: 2026.10.08(목)
한가람 위원: 차관님, 지난번 자료를 다음 주 금요일까지 보고해 주십시오. 제가 직접 확인하겠습니다.
가상부제1차관 오세린: 예, 그렇게 하도록 하겠습니다.
서도윤 위원: 현장 점검 결과는 제가 정리해서 10월 14일까지 위원님들께 공유하겠습니다.
서도윤 위원: 우리 국민이 쉽게 볼 수 있게 안내 자료도 다음 주까지 보내겠습니다.
서도윤 위원: 우리가 다음 주 금요일까지 일정을 정하게 되어 있습니다.
이 건은 한가람 위원님과 상의해서 정리하도록 하겠습니다.
서도윤 위원: 저희가 자료를 모으겠습니다.
정리본은 다음 주에 보내겠습니다.
가상부장관 문해솔: 저희가 관계 부처와 협의해서 개선안을 마련하겠습니다.
가상부장관 문해솔: 그 부분은 제가 직접 챙겨서 다음 주 금요일까지 보고드리겠습니다.
윤채원 위원: 제도 개선을 위해 최선을 다하겠습니다.
윤채원 위원: 검토해 보고 금요일까지 회신하겠습니다.
윤채원 위원: 예, 알겠습니다.
가상부제1차관 오세린: 예, 알겠습니다.

- 결과 보고서 제출: 오세린 차관, 10월 14일까지
"""


def check(text: str, owner: str, evidence: str, rules: OwnerRules | None = RULES):
    extraction = LLMExtraction(
        summary=["요약"],
        decisions=[],
        action_items=[LLMActionItem(task="할 일", owner_name=owner, evidence_quote=evidence)],
    )
    return validate_extraction(
        extraction,
        meeting_text=text,
        meeting_date=MEETING_DATE,
        meeting_id="m1",
        roster=ROSTER,
        owner_rules=rules,
    ).items[0]


# --- settings file ---------------------------------------------------------


def test_shipped_rules_load_without_warning():
    rules, warning = default_owner_rules()
    assert warning is None
    assert rules.institution_head_titles


@pytest.mark.parametrize("title", ["주무관", "사무관", "과장", "팀장"])
def test_team_meeting_titles_are_not_institution_heads(title):
    assert title not in RULES.institution_head_titles
    assert not RULES.is_institution_head(title)


@pytest.mark.parametrize(
    ("title", "head"),
    [
        ("가상부장관", True),
        ("가상부제1차관", True),
        ("부총리겸가상재정부장관", True),
        ("국무총리", True),
        ("공정거래위원장", True),
        ("가상재판소장후보자", True),  # a nominee speaks as the post they are named for
        ("위원장", False),  # a committee chair runs the meeting
        ("소위원장", False),
        ("위원", False),
        ("간사", False),
        ("기획조정실장", False),
    ],
)
def test_institution_head_titles(title, head):
    assert RULES.is_institution_head(title) is head


def test_rules_file_errors_are_reported(tmp_path):
    with pytest.raises(OwnerRulesError, match="없습니다"):
        load_owner_rules(tmp_path / "missing.yaml")
    typo = tmp_path / "typo.yaml"
    typo.write_text("institution_head_title: [장관]\n", encoding="utf-8")
    with pytest.raises(OwnerRulesError, match="형식"):
        load_owner_rules(typo)


# --- speakers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "label"),
    [
        ("한가람 위원: 질의하겠습니다.", "한가람 위원"),
        ("가상부제1차관 오세린: 답변드리겠습니다.", "가상부제1차관 오세린"),
        ("김민준 00:01:02 시작하겠습니다.", "김민준"),
        ("회의명: 주간 회의", None),
        ("참석자: 김민준 주무관, 이서연 사무관", None),
        ("- 오리엔테이션 자료 추가: 김민준 주무관, 10월 14일까지", None),
        ("일시: 2026.10.08(목) 14:00", None),
    ],
)
def test_speaker_label(line, label):
    assert speaker_label(line, RULES) == label


def test_speech_text_matches_normalized_minutes():
    speech = Speech.from_minutes(HEARING, RULES)
    assert speech.text == normalize_space(HEARING)
    assert speech.speakers[-1] is None  # the bullet after a blank line is narrative


# --- the label criteria ------------------------------------------------------


def test_head_promise_without_subject_is_institutional():
    action = check(HEARING, "가상부제1차관 오세린", "예, 그렇게 하도록 하겠습니다.")
    assert (action.owner_name, action.owner_status) == ("오세린", "unconfirmed")
    assert any("기관 대표 발언자(가상부제1차관)" in n for n in action.review_notes)


def test_request_to_a_head_is_not_an_assignment():
    # "제가" in the request is the member speaking, not the vice-minister.
    action = check(
        HEARING,
        "오세린",
        "차관님, 지난번 자료를 다음 주 금요일까지 보고해 주십시오. 제가 직접 확인하겠습니다.",
    )
    assert action.owner_status == "unconfirmed"
    assert any("요청" in n for n in action.review_notes)


def test_head_saying_first_person_is_personal():
    action = check(HEARING, "문해솔", "그 부분은 제가 직접 챙겨서 다음 주 금요일까지 보고드리겠습니다.")
    assert (action.owner_name, action.owner_status) == ("문해솔", "confirmed")


def test_we_as_subject_is_institutional():
    action = check(HEARING, "문해솔", "저희가 관계 부처와 협의해서 개선안을 마련하겠습니다.")
    assert action.owner_status == "unconfirmed"
    assert any("'저희가'" in n for n in action.review_notes)


@pytest.mark.parametrize(
    "evidence",
    [
        "현장 점검 결과는 제가 정리해서 10월 14일까지 위원님들께 공유하겠습니다.",
        "우리 국민이 쉽게 볼 수 있게 안내 자료도 다음 주까지 보내겠습니다.",  # '우리 국민' is not the subject
    ],
)
def test_member_self_assignment_is_personal(evidence):
    action = check(HEARING, "서도윤 위원", evidence)
    assert (action.owner_name, action.owner_status) == ("서도윤", "confirmed")


def test_we_in_another_sentence_is_not_the_promise_subject():
    evidence = (
        "우리가 다음 주 금요일까지 일정을 정하게 되어 있습니다. "
        "이 건은 한가람 위원님과 상의해서 정리하도록 하겠습니다."
    )
    assert check(HEARING, "서도윤 위원", evidence).owner_status == "confirmed"


def test_we_carries_over_to_the_same_speakers_next_promise():
    action = check(HEARING, "서도윤", "저희가 자료를 모으겠습니다. 정리본은 다음 주에 보내겠습니다.")
    assert action.owner_status == "unconfirmed"
    assert any("'저희가'" in n for n in action.review_notes)


def test_we_without_a_promise_is_still_the_subject():
    action = check(HEARING, "서도윤", "우리가 다음 주 금요일까지 일정을 정하게 되어 있습니다.")
    assert action.owner_status == "unconfirmed"


def test_weak_promise_is_not_confirmed():
    action = check(HEARING, "윤채원", "제도 개선을 위해 최선을 다하겠습니다.")
    assert action.owner_status == "unconfirmed"
    assert any("약한 약속" in n for n in action.review_notes)


def test_weak_phrase_far_from_the_promise_does_not_count():
    action = check(HEARING, "윤채원", "검토해 보고 금요일까지 회신하겠습니다.")
    assert action.owner_status == "confirmed"


def test_same_words_from_two_speakers_use_the_owners_own_line():
    assert check(HEARING, "윤채원", "예, 알겠습니다.").owner_status == "confirmed"
    assert check(HEARING, "오세린", "예, 알겠습니다.").owner_status == "unconfirmed"


def test_assignment_written_in_the_minutes_confirms_a_head():
    action = check(HEARING, "오세린 차관", "결과 보고서 제출: 오세린 차관, 10월 14일까지")
    assert (action.owner_name, action.owner_status) == ("오세린", "confirmed")


def test_named_person_after_we_keeps_the_assignment():
    text = "- 교육 자료는 저희 팀에서 김민준 주무관이 맡기로 함.\n- 안내문은 저희 팀에서 준비하기로 함.\n"
    named = check(text, "김민준 주무관", "교육 자료는 저희 팀에서 김민준 주무관이 맡기로 함.")
    assert named.owner_status == "confirmed"
    assert check(text, "김민준", "안내문은 저희 팀에서 준비하기로 함.").owner_status == "unconfirmed"


def test_rules_can_be_switched_off():
    action = check(HEARING, "오세린", "예, 그렇게 하도록 하겠습니다.", rules=OwnerRules())
    assert action.owner_status == "confirmed"


# --- the fictional samples: team members' own tasks stay confirmed ----------------

S1, S2, S3 = "01_structured_minutes.txt", "02_transcript.txt", "03_edge_cases.txt"
SAMPLE_OWNERS = [
    (S1, "김민준 주무관", "오리엔테이션 자료에 연가·병가 신청 절차 추가: 김민준 주무관, 10월 14일(수)까지"),
    (S1, "이서연 사무관", "복무 규정 FAQ 초안 작성: 이서연 사무관, 다음 주 금요일까지"),
    (S1, "박지훈 주무관", "비품 온라인 신청서 양식 제작: 박지훈 주무관, 10/12(월) 오후 3시까지"),
    (S1, "최유진 주무관", "오리엔테이션 대회의실 예약 및 장비 점검: 최유진 주무관, 내일까지"),
    (S2, "최유진", "멘토 배정표는 제가 만들게요. 월요일까지 공유하겠습니다."),
    (S2, "정하은", "멘토용 안내 메일 문구는 제가 다음 주 화요일까지 초안 잡아 볼게요."),
    (S2, "이서연", "설문 결과 정리 보고서는 이서연 사무관님이 맡아 주시겠어요?"),
    (S2, "이서연", "네, 제가 할게요."),
    (
        S2,
        "이서연 사무관",
        "설문 결과 정리 보고서는 이서연 사무관님이 맡아 주시겠어요? "
        "이번 달 20일까지면 될 것 같습니다. 네, 제가 할게요.",
    ),
    (S3, "정하은 주무관", "출입증 발급 신청서는 정하은 주무관이 3일 이내에 취합하기로 함."),
    (S3, "김민준 주무관", "사무실 자리 배치도는 김민준 주무관이 10월 5일까지 공유하기로 했음."),
    (S3, "최 주무관", "복지 포인트 안내문은 최 주무관이 다음 주 월요일 오전 10시까지 올리기로 함."),
]


@pytest.mark.parametrize(("sample", "owner", "evidence"), SAMPLE_OWNERS)
def test_fictional_sample_owners_stay_confirmed(sample, owner, evidence):
    text = (REPO_ROOT / "data" / "samples" / sample).read_text(encoding="utf-8")
    action = check(text, owner, evidence)
    assert action.owner_status == "confirmed", action.review_notes
