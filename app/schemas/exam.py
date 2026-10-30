"""클라이언트로 나가는 응시/결과 뷰 모델.

!! 정답 비노출 원칙 !!
응시 중(QuestionView, AttemptView)에는 correct_keys / explanation / is_correct / 점수를
담는 필드 자체가 존재하지 않는다. extra="forbid" 로 실수로 필드가 추가되는 것을 막고,
결과 뷰(ResultView)는 제출된 attempt 에 대해서만 생성된다(services/results.py).
"""
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models import AttemptItem, ExamAttempt


class _View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OptionView(_View):
    key: str   # 원본 보기 key (정답 여부를 암시하지 않음)
    text: str


class QuestionView(_View):
    seq: int
    item_type: str
    stem: str
    options: list[OptionView]  # 저장된 option_order 순서
    selected_keys: list[str]
    flagged: bool


class AttemptView(_View):
    attempt_id: int
    status: str
    expires_at: datetime
    total_questions: int
    answered: int
    flagged: list[int]
    answered_seqs: list[int]


def question_view(ai: AttemptItem) -> QuestionView:
    opts = ai.item.options
    return QuestionView(
        seq=ai.seq,
        item_type=ai.item.item_type.value,
        stem=ai.item.stem,
        options=[OptionView(key=k, text=opts[k]) for k in ai.option_order],
        selected_keys=list(ai.selected_keys or []),
        flagged=ai.flagged,
    )


def attempt_view(attempt: ExamAttempt) -> AttemptView:
    answered = [i.seq for i in attempt.items if i.selected_keys]
    return AttemptView(
        attempt_id=attempt.id,
        status=attempt.status.value,
        expires_at=attempt.expires_at,
        total_questions=len(attempt.items),
        answered=len(answered),
        flagged=[i.seq for i in attempt.items if i.flagged],
        answered_seqs=answered,
    )


# ---- 결과(제출 후 전용) ----
class DomainResult(_View):
    code: str
    name: str
    earned: float
    max: float
    ratio: float
    below_minimum: bool


class WrongItemView(_View):
    seq: int
    stem: str
    options: list[OptionView]
    selected_keys: list[str]
    correct_keys: list[str]
    explanation: str
    reference: str | None


class ResultView(_View):
    attempt_id: int
    status: str
    total_score: float
    passed: bool
    pass_score: float
    domain_min_ratio: float
    domains: list[DomainResult]
    explanations_visible: bool
    adjusted: bool = False  # 관리자 채점 보정(정답 인정/제외)이 적용된 문항이 있음
    wrong_items: list[WrongItemView]
