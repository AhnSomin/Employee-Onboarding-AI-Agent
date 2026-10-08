import pytest

from onboarding_agent.config import REPO_ROOT
from onboarding_agent.meeting.roster import (
    Roster,
    RosterError,
    is_group_reference,
    load_roster,
    strip_titles,
)


@pytest.fixture(scope="module")
def roster() -> Roster:
    return load_roster(REPO_ROOT / "data" / "roster.yaml")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("김민준", "김민준"),
        ("김민준 주무관", "김민준"),
        ("김민준 주무관님", "김민준"),
        ("김 주무관", "김민준"),  # alias
        ("민준 님", "김민준"),  # alias after stripping the honorific
        ("이 사무관", "이서연"),
        ("이서연 사무관님", "이서연"),
        ("최 주무관", "최유진"),
        ("하은", "정하은"),  # given name
        ("인사기획팀 박지훈 주무관", "박지훈"),
    ],
)
def test_matches_names_aliases_and_titles(roster, raw, expected):
    match = roster.match(raw)
    assert match.member is not None, raw
    assert match.member.name == expected


def test_surname_with_title_is_ambiguous_when_two_members_fit(roster):
    match = roster.match("박 주무관")
    assert match.member is None
    assert {m.name for m in match.candidates} == {"박지훈", "박소연"}


def test_unknown_names_do_not_match(roster):
    assert roster.match("한도윤 주무관").member is None
    assert roster.match("홍 주무관").candidates == ()
    assert roster.match("").member is None


def test_strip_titles_and_group_references():
    assert strip_titles("남궁민 서기관님") == "남궁민"
    assert strip_titles("김 주무관") == "김"
    assert is_group_reference("인사팀")
    assert is_group_reference("운영지원과")
    assert is_group_reference("인사팀에서")
    assert is_group_reference("참석자 전원")
    assert not is_group_reference("김민준 주무관")


def test_slack_id_can_be_missing(roster):
    assert roster.match("이서연").member.slack_user_id is None
    assert roster.match("김민준").member.slack_user_id == "U00000001"


def test_missing_and_broken_roster_files(tmp_path):
    assert load_roster(tmp_path / "absent.yaml").members == []
    broken = tmp_path / "broken.yaml"
    broken.write_text("members: [name: 김민준", encoding="utf-8")
    with pytest.raises(RosterError):
        load_roster(broken)
