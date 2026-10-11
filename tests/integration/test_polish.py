"""말투 다듬기: 개수·숫자가 어긋나면 원문을 그대로 쓰고, 모델이 없거나 실패해도 추출 결과를 막지 않는다."""
from onboarding_agent.llm.client import LLMUnavailable
from onboarding_agent.meeting.polish import PolishOut, polish_overview

SUMMARY = ["설문에서 시간이 부족하다는 의견이 있었다.", "멘토링을 매주 하기로 했다."]
DECISIONS = ["멘토링을 매주 하기로 했다.", "장소는 10월 21일 전까지 정하기로 했다."]
ISSUES = ["수당 기준은 아직 정해지지 않았다."]


class Fake:
    def __init__(self, out=None, exc=None):
        self.out, self.exc = out, exc

    def generate_structured(self, prompt, schema, system=None):
        if self.exc:
            raise self.exc
        return self.out, "fake-model"


def good():
    return PolishOut(
        summary=["설문에서 시간이 부족하다는 의견이 있었습니다."],          # 결정사항과 겹치는 줄은 빠져도 된다
        decisions=["멘토링을 매주 하기로 했습니다.", "장소는 10월 21일 전까지 정하기로 했습니다."],
        open_issues=["수당 기준은 아직 정해지지 않았습니다."],
    )


def test_polished_text_replaces_the_sentences():
    out = polish_overview(SUMMARY, DECISIONS, ISSUES, client=Fake(good()))
    assert out.changed
    assert out.decisions[0].endswith("했습니다.") and out.open_issues == ["수당 기준은 아직 정해지지 않았습니다."]
    assert out.summary == ["설문에서 시간이 부족하다는 의견이 있었습니다."]


def test_changed_numbers_fall_back_to_the_original_section():
    bad = good().model_copy(update={"decisions": ["멘토링을 매주 하기로 했습니다.", "장소는 10월 25일 전까지 정하기로 했습니다."]})
    out = polish_overview(SUMMARY, DECISIONS, ISSUES, client=Fake(bad))
    assert out.decisions == DECISIONS                      # 날짜가 바뀐 구역은 원문 유지
    assert out.open_issues[0].endswith("않았습니다.")       # 문제없는 구역은 그대로 다듬어짐


def test_wrong_count_falls_back():
    bad = good().model_copy(update={"decisions": ["멘토링을 매주 하기로 했습니다."]})
    assert polish_overview(SUMMARY, DECISIONS, ISSUES, client=Fake(bad)).decisions == DECISIONS


def test_runaway_long_sentence_falls_back():
    bad = good().model_copy(update={"open_issues": ["수당 기준은 아직 정해지지 않았습니다. " * 20]})
    assert polish_overview(SUMMARY, DECISIONS, ISSUES, client=Fake(bad)).open_issues == ISSUES


def test_summary_with_new_numbers_falls_back():
    bad = good().model_copy(update={"summary": ["설문에서 3명이 시간이 부족하다고 했습니다."]})
    assert polish_overview(SUMMARY, DECISIONS, ISSUES, client=Fake(bad)).summary == SUMMARY


def test_unavailable_or_failing_model_returns_the_original():
    for fake in (Fake(exc=LLMUnavailable("no model")), Fake(exc=RuntimeError("boom"))):
        out = polish_overview(SUMMARY, DECISIONS, ISSUES, client=fake)
        assert (out.summary, out.decisions, out.open_issues, out.changed) == (SUMMARY, DECISIONS, ISSUES, False)


def test_empty_input_makes_no_call():
    assert polish_overview([], [], [], client=Fake(exc=RuntimeError("must not be called"))).changed is False
