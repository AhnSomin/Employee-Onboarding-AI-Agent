"""Cases found by the adversarial review of the 2026-10-09 rule changes (all fictional).

Each case is (minutes, owner, evidence quote, expected owner status).
"""

from datetime import date

import pytest

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.models import LLMActionItem, LLMExtraction
from onboarding_agent.meeting.owner_rules import load_owner_rules, speaker_label
from onboarding_agent.meeting.roster import load_roster
from onboarding_agent.meeting.validate import validate_extraction

ROSTER = load_roster(REPO_ROOT / "data" / "roster.yaml")
H = "회의명: 가상위원회 회의\n한가람 위원: 자료를 보고해 주십시오.\n"


def status(minutes: str, owner: str, evidence: str, task: str = "할 일") -> tuple[str | None, str]:
    item = LLMActionItem(task=task, owner_name=owner, evidence_quote=evidence)
    out = validate_extraction(
        LLMExtraction(summary=["s"], decisions=[], action_items=[item]),
        meeting_text=minutes, meeting_date=date(2026, 10, 8), meeting_id="m", roster=ROSTER,
    )
    return out.items[0].owner_name, out.items[0].owner_status


def said(label: str, line: str) -> tuple[str, str]:
    """Minutes with one utterance, and that utterance as the quote."""
    return f"{H}{label}: {line}\n", line


HEAD_DIRECT = [
    # '직접' must be in the head's own (non-weak) promise sentence
    said(
        "가상부장관 문해솔",
        "지난달 현장은 제가 직접 가 보기로 했던 곳입니다. 결과는 다음 주 금요일까지 보고드리겠습니다.",
    ),
    said(
        "가상부장관 문해솔",
        "제가 직접 방문할 예정이었는데 일정이 맞지 않았습니다. 결과는 다음 주 금요일까지 보고드리겠습니다.",
    ),
    said("가상부장관 문해솔", "제가 직접 검토해 보겠습니다. 결과는 금요일까지 알려 드리겠습니다."),
    said("가상부장관 문해솔", "현장은 제가 직접 보겠습니다. 결과 보고서는 다음 주까지 제출하겠습니다."),
    # '직접' that is someone else's, negated, or part of a noun
    said("가상부장관 문해솔", "그 건은 차관이 직접 챙겨서 금요일까지 보고드리겠습니다."),
    said("가상부장관 문해솔", "그 건은 제가 직접 챙기기는 어렵겠습니다만 담당 실장이 금요일까지 보고드리겠습니다."),
    said("가상부장관 문해솔", "위원님께서 직접 보시겠다고 하셔서 자료는 다음 주 금요일까지 보내 드리겠습니다."),
    said("가상부장관 문해솔", "제가 직접 하겠다는 건 아니고, 실무진이 다음 주까지 보고드리겠습니다."),
    said("가상광역시장 윤채원", "저는 직접 나서기보다 실무 부서가 금요일까지 정리하도록 하겠습니다."),
    said("가상부장관 문해솔", "비정규직의 직접 고용 전환은 연말까지 마무리하겠습니다."),
    said("가상부장관 문해솔", "직접고용 전환 실적을 다음 주까지 보고드리겠습니다."),
    said("가상부장관 문해솔", "직접비 내역을 정리해서 보고드리겠습니다."),
    # heads in other label forms
    said("가상재판소장 후보자 문해솔", "제가 다음 주까지 보고드리겠습니다."),
    said("가상재판소장 내정자 문해솔", "제가 다음 주까지 보고드리겠습니다."),
    said("문해솔 가상교통부 장관", "제가 다음 주까지 보고드리겠습니다."),
    said("가상시 시장 윤채원", "제가 다음 주까지 보고드리겠습니다."),
    # "우리 시/저희 군" is an institution
    said("가상광역시장 윤채원", "우리 시에서 직접 점검하겠습니다."),
    said("가상군수 서도윤", "저희 군에서 직접 점검하겠습니다."),
    said("윤채원 위원", "저희 시에서 다음 주까지 점검하겠습니다."),
]


@pytest.mark.parametrize(("minutes", "evidence"), HEAD_DIRECT)
def test_heads_and_groups_stay_unconfirmed(minutes, evidence):
    owner = minutes.splitlines()[-1].split(":")[0].split()
    name = next(t for t in owner if t in ("문해솔", "윤채원", "서도윤"))
    assert status(minutes, name, evidence)[1] == "unconfirmed"


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("가상부장관 문해솔", "그 문제는 제가 직접 챙기려고 합니다."),
        ("가상광역시장 윤채원", "결과는 제가 직접 다음 주 금요일까지 보고드리고자 합니다."),
        ("가상군수 서도윤", "현장 점검은 제가 직접 할 생각입니다."),
        ("가상부장관 문해솔", "제가 직접 10. 14.까지 보고드리겠습니다."),  # '10. 14.' is one sentence
    ],
)
def test_heads_saying_directly_are_personal(label, line):
    minutes, evidence = said(label, line)
    name = label.split()[-1]
    assert status(minutes, name, evidence)[1] == "confirmed"


def test_attendee_list_title_marks_a_head():
    minutes = (
        "회의명: 가상 회의\n참석: 윤채원(가상광역시장), 한가람 위원\n"
        "윤채원 00:01:00 제가 다음 주까지 보고드리겠습니다.\n"
    )
    assert status(minutes, "윤채원", "제가 다음 주까지 보고드리겠습니다.")[1] == "unconfirmed"


@pytest.mark.parametrize(
    ("owner", "evidence", "name"),
    [
        ("서도윤 군수", "주민 설명회 개최: 서도윤 군수, 10월 20일까지", "서도윤"),
        ("윤채원 광역시장", "결과 보고서 제출: 윤채원 광역시장, 10월 14일까지", "윤채원"),
    ],
)
def test_written_assignment_to_a_local_head_keeps_the_name(owner, evidence, name):
    minutes = f"회의명: 가상 협의회\n- {evidence}\n"
    assert status(minutes, owner, evidence) == (name, "confirmed")


WEAK = """[녹취] 주간 회의 (가상 녹취록)
김민준 00:01:00 그 부분은 참고하도록 하겠고요 다음 안건 보시죠
최유진 00:02:00 일정에 맞추도록 노력하도록 하겠고요 다음 안건 보시죠
정하은 00:03:00 개선되도록 노력하겠으며 다음 안건 보시죠
박지훈 00:04:00 차질 없도록 최선을 다하도록 하겠고 다음 안건 보시죠
이서연 00:05:00 말씀하신 건 참고를 하도록 하겠습니다.
김민준 00:06:00 그 의견은 참고하도록 할게요.
정하은 00:07:00 그 의견은 참고하는 것으로 하겠습니다.
최유진 00:08:00 그 부분은 검토해 볼게요.
박지훈 00:09:00 그 부분은 생각해 볼 계획입니다.
이서연 00:10:00 그 부분은 검토해 봐야겠습니다.
김민준 00:11:00 그 부분은 검토를 해 보도록 하겠습니다.
정하은 00:12:00 제가 지금까지 나온 의견을 검토해 보겠습니다.
최유진 00:13:00 저는 어디까지나 원칙적으로 검토해 보겠습니다.
박지훈 00:14:00 제가 1일 평균 민원 건수를 생각해 보겠습니다.
"""


@pytest.mark.parametrize("line", WEAK.splitlines()[1:])
def test_weak_promise_forms_stay_unconfirmed(line):
    owner, rest = line.split(" ", 1)
    evidence = rest.split(" ", 1)[1]
    assert status(WEAK, owner, evidence)[1] == "unconfirmed"


def test_weak_form_for_a_head_is_still_weak():
    minutes, evidence = said("가상부장관 문해솔", "제가 직접 검토해 볼게요.")
    assert status(minutes, "문해솔", evidence)[1] == "unconfirmed"


TEAM = [
    # team members' own tasks stay personal even next to words ending in 시장·군수·장관
    ("회의명: 전통시장 현장 점검 팀 회의\n- 구역별 담당: 동부시장 정하은, 서부시장 김민준\n"
     "정하은 주무관: 점검 결과는 제가 금요일까지 정리해서 공유하겠습니다.\n",
     "정하은", "점검 결과는 제가 금요일까지 정리해서 공유하겠습니다."),
    ("회의명: 가상 팀 회의\n○ 안건 2: 전통시장\n김민준 주무관: 상인회 의견은 제가 10월 14일까지 정리하겠습니다.\n",
     "김민준", "상인회 의견은 제가 10월 14일까지 정리하겠습니다."),
    ("[녹취] 가상 회의\n박지훈 00:01:00 다음은 어디죠, 수산시장?\n"
     "최유진 00:01:30 네, 현장 사진은 제가 내일까지 공유하겠습니다.\n",
     "최유진", "네, 현장 사진은 제가 내일까지 공유하겠습니다."),
    ("[녹취] 가상 회의\n박지훈 00:01:00 워크숍 장소 단풍이 정말 장관\n"
     "박소연 00:02:00 숙소 예약은 제가 금요일까지 하겠습니다.\n",
     "박소연", "숙소 예약은 제가 금요일까지 하겠습니다."),
    ("회의명: 가상 팀 회의\n주무관 김민준: 시장 동향 자료는 제가 금요일까지 공유하겠습니다.\n",
     "김민준", "시장 동향 자료는 제가 금요일까지 공유하겠습니다."),
    ("회의명: 가상 팀 회의\n사무관 이서연: 군수 물자 목록은 제가 금요일까지 정리하겠습니다.\n",
     "이서연", "군수 물자 목록은 제가 금요일까지 정리하겠습니다."),
    ("[녹취] 가상 회의\n정하은 00:01:00 지난주 김민준과 시장 조사를 다녀왔습니다.\n"
     "김민준 00:02:00 조사 결과는 제가 금요일까지 정리하겠습니다.\n",
     "김민준", "조사 결과는 제가 금요일까지 정리하겠습니다."),
    # nouns like 고민·유념·노력 inside an object are not weak promises
    ("회의명: 가상 팀 회의\n정하은 주무관: 제가 신규 임용자 고민 사항을 정리하도록 하겠습니다.\n",
     "정하은", "제가 신규 임용자 고민 사항을 정리하도록 하겠습니다."),
    ("회의명: 가상 팀 회의\n정하은 주무관: 신규 임용자가 유념할 사항을 정리하도록 하겠습니다.\n",
     "정하은", "신규 임용자가 유념할 사항을 정리하도록 하겠습니다."),
    ("회의명: 가상 팀 회의\n박지훈 주무관: 팀이 노력해 온 내용을 정리토록 하겠습니다.\n",
     "박지훈", "팀이 노력해 온 내용을 정리토록 하겠습니다."),
    # presiders and the '제가' + date exception; '10. 14.' stays one sentence
    ("회의명: 가상시의회 본회의\n의장 한가람: 제가 금요일까지 검토해 보겠습니다.\n",
     "한가람", "제가 금요일까지 검토해 보겠습니다."),
    ("회의명: 가상 팀 회의\n김민준 주무관: 제가 10. 14.까지 검토해 보겠습니다.\n",
     "김민준", "제가 10. 14.까지 검토해 보겠습니다."),
    # a heading such as '광역시장 협의:' is not a speaker
    ("가상 협의회 회의 결과\n광역시장 협의:\n가상광역시장 윤채원이 10월 20일까지 회신하기로 함.\n",
     "윤채원", "가상광역시장 윤채원이 10월 20일까지 회신하기로 함."),
]


@pytest.mark.parametrize(("minutes", "owner", "evidence"), TEAM)
def test_personal_tasks_stay_confirmed(minutes, owner, evidence):
    assert status(minutes, owner, evidence)[1] == "confirmed"


@pytest.mark.parametrize("line", ["광역시장 협의:", "시장 동향:", "군수 지원: 다음 주 일정"])
def test_headings_are_not_speakers(line):
    assert speaker_label(line, load_owner_rules()) is None


# --- second review round ---------------------------------------------------------

T = "회의명: 가상 팀 회의\n한가람 과장: 다음 안건 보시죠.\n"


def team(line: str, label: str = "김민준 주무관") -> tuple[str, str]:
    return f"{T}{label}: {line}\n", line


def team_case(line: str, label: str = "김민준 주무관") -> tuple[str, str, str]:
    """(minutes, owner, evidence) for 김민준's own line."""
    minutes, evidence = team(line, label)
    return minutes, "김민준", evidence


SECOND_UNCONFIRMED = [
    # '겠' that is not a promise does not rescue a weak promise
    team("네, 알겠습니다. 그 부분은 검토해 보겠습니다."),
    team("당장은 어렵겠습니다만 개선되도록 노력하겠습니다."),
    team("쉽지는 않겠지만 최선을 다하겠습니다."),
    team("그렇게 하겠다고 약속드리기는 어렵고 검토해 보겠습니다."),
    # nouns with 계획/예정/기로 are not promises
    team("추진 계획 관련해서는 검토해 보겠습니다."),
    team("분위기로 봐서는 좀 더 고민해 보겠습니다."),
    # more weak forms
    team("그 부분은 검토를 좀 해 보겠습니다."),
    team("노력은 해 보겠습니다."),
    team("노력을 아끼지 않겠습니다."),
    team("최선 다하겠습니다."),
    team("유념해 두겠습니다."),
    team("그 의견은 참고만 하겠습니다."),
    team("그 의견은 참고해 두겠습니다."),
    team("그 의견은 참고로 삼겠습니다."),
    team("그 부분은 고민해 보죠."),
    # 저희/우리 as the subject in more forms
    team("저희 다음 주까지 정리하겠습니다."),
    team("저희 팀은요 금요일까지 정리하겠습니다."),
    team("저희 TF에서 금요일까지 정리하겠습니다."),
    team("저희 실무진에서 금요일까지 정리하겠습니다."),
    team("저희 둘이 금요일까지 정리하겠습니다."),
    team("저희 과에서는 제가 말씀드린 일정대로 정리하겠습니다."),
    # '제가' + something that is not a date
    team("제가 1/2 정도는 검토해 보겠습니다."),
    team("제가 2-3가지 방안을 생각해 보겠습니다."),
]


@pytest.mark.parametrize(("minutes", "evidence"), SECOND_UNCONFIRMED)
def test_second_round_unconfirmed(minutes, evidence):
    assert status(minutes, "김민준", evidence)[1] == "unconfirmed"


@pytest.mark.parametrize(
    ("minutes", "owner", "evidence"),
    [
        # the quote stops before the weak ending; the minutes sentence decides
        (f"{T}김민준 주무관: 그 부분은 다음 회의 전에 검토해 보겠습니다.\n", "김민준",
         "그 부분은 다음 회의 전에"),
        # the quote starts after a 저희 subject
        (f"{T}김민준 주무관: 저희 팀에서 다음 주까지 자료를 정리하겠습니다.\n", "김민준",
         "다음 주까지 자료를 정리하겠습니다."),
        # someone else's promise is not the owner's task
        (f"{T}김민준 주무관: 이서연 사무관님께 금요일까지 자료를 보내 드리겠습니다.\n", "이서연",
         "이서연 사무관님께 금요일까지 자료를 보내 드리겠습니다."),
        # a written request to a head is not an assignment
        ("회의명: 가상 협의회 결과\n- 위원들은 윤채원 시장이 다음 주까지 직접 결과를 보고해 줄 것을 요청함.\n",
         "윤채원",
         "위원들은 윤채원 시장이 다음 주까지 직접 결과를 보고해 줄 것을 요청함."),
    ],
)
def test_second_round_context(minutes, owner, evidence):
    assert status(minutes, owner, evidence)[1] == "unconfirmed"


@pytest.mark.parametrize(
    "line",
    [
        "담당 실장이 내일 직접 확인해서 보고드리겠습니다.",
        "직접 챙기기는 힘들고 차관이 보고드리겠습니다.",
        "제가 직접 할 수는 없고 실무진이 금요일까지 정리하겠습니다.",
        "저희 직접 현장을 점검하겠습니다.",
    ],
)
def test_second_round_heads_without_their_own_direct(line):
    minutes, evidence = said("가상부장관 문해솔", line)
    assert status(minutes, "문해솔", evidence)[1] == "unconfirmed"


@pytest.mark.parametrize(
    "line",
    [
        "제가 현장에서 직접 확인하겠습니다.",
        "제가 일일이 직접 챙기겠습니다.",
        "제가 직접 챙겨서 늦지 않게 보고드리겠습니다.",
        "제가 직접 관련 부서와 협의하겠습니다.",
        "제가 직접 현장을 점검하기로 하였습니다.",
        "현장은 제가 직접 챙기겠습니다. 협조해 주시면 감사하겠습니다.",
    ],
)
def test_second_round_heads_with_their_own_direct(line):
    minutes, evidence = said("가상부장관 문해솔", line)
    assert status(minutes, "문해솔", evidence)[1] == "confirmed"


@pytest.mark.parametrize(
    ("minutes", "owner", "evidence"),
    [
        team_case("제가 금요일까지 검토해 보겠습니다.", "김민준 팀원"),
        ("회의명: 가상 팀 회의\n○ 김민준 주무관: 제가 금요일까지 검토해 보겠습니다.\n", "김민준",
         "제가 금요일까지 검토해 보겠습니다."),
        team_case("제가 금요일까지 검토해 보겠습니다.", "인사팀 김민준 주무관"),
        ("회의명: 가상 팀 회의\n박지훈 팀장(사회): 제가 금요일까지 검토해 보겠습니다.\n", "박지훈",
         "제가 금요일까지 검토해 보겠습니다."),
        team_case("제가 14일(수)까지 검토해 보겠습니다."),
        team_case("제가 내일 오전 10시까지 검토해 보겠습니다."),
        team_case("제가... 금요일까지 검토해 볼게요."),
        team_case("제가 금요일까지 올리겠습니다. 우리 팀도 바쁘시겠지만 한번 봐 주세요."),
        team_case("제가 금요일까지 마무리하겠습니다. 저희 팀도 같이 보시겠어요?"),
        team_case("제가 금요일까지 마무리하겠습니다. 원래 저희 팀이 하기로 했던 일입니다."),
        team_case("제가 저희 팀이 정리한 자료를 금요일까지 공유하겠습니다."),
        ("회의명: 가상 팀 회의\n- 자료 정리는 정하은 주무관이 저희 과가 맡은 범위에서 하기로 함.\n", "정하은",
         "자료 정리는 정하은 주무관이 저희 과가 맡은 범위에서 하기로 함."),
        (f"{T}한가람 과장: 저희 팀은 김민준 씨가 금요일까지 정리하기로 합시다.\n", "김민준",
         "저희 팀은 김민준 씨가 금요일까지 정리하기로 합시다."),
        ("회의명: 가상 팀 회의\n- 신규 직원 적응 노력 계획 수립: 김민준 주무관, 10월 14일까지\n", "김민준",
         "신규 직원 적응 노력 계획 수립: 김민준 주무관, 10월 14일까지"),
        ("회의명: 가상 팀 회의\n- 자료 정리는 김민준 주무관이 금요일까지 하기로 함.\n"
         "김민준 주무관: 네, 최선을 다하겠습니다.\n",
         "김민준", "자료 정리는 김민준 주무관이 금요일까지 하기로 함. 네, 최선을 다하겠습니다."),
    ],
)
def test_second_round_personal_tasks_stay_confirmed(minutes, owner, evidence):
    assert status(minutes, owner, evidence)[1] == "confirmed"
