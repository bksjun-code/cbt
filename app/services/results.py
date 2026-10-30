"""결과 뷰 생성. 제출/만료된 attempt 에서만 가능하며, 해설 공개는 관리자 설정을 따른다."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AttemptStatus, Domain, ExamAttempt
from app.schemas.exam import DomainResult, OptionView, ResultView, WrongItemView
from app.services.app_settings import get_setting
from app.services.grading import load_overrides


class ResultNotAvailable(Exception):
    pass


def build_result(db: Session, attempt: ExamAttempt) -> ResultView:
    if attempt.status == AttemptStatus.IN_PROGRESS:
        raise ResultNotAvailable("제출 전에는 결과를 조회할 수 없습니다")

    doms = list(db.scalars(select(Domain).order_by(Domain.id)))
    names = {d.code: d.name for d in doms}
    order = {d.code: i for i, d in enumerate(doms)}  # 설계된 영역 순서(ARCH→…→TROUBLE)
    min_ratio = attempt.domain_min_ratio or 0.0
    domains = [
        DomainResult(code=c, name=names.get(c, c), earned=v["earned"], max=v["max"], ratio=v["ratio"],
                     below_minimum=v["max"] > 0 and v["ratio"] + 1e-9 < min_ratio)
        for c, v in sorted((attempt.domain_scores or {}).items(), key=lambda kv: order.get(kv[0], 99))
    ]
    visible = bool(get_setting(db, "show_explanations", True))
    wrong: list[WrongItemView] = []
    if visible:
        for ai in attempt.items:
            if ai.is_correct is not False:  # 정답(True) 또는 채점 제외(None)는 오답 해설 대상이 아님
                continue
            opts = ai.item.options
            wrong.append(
                WrongItemView(
                    seq=ai.seq, stem=ai.item.stem,
                    options=[OptionView(key=k, text=opts[k]) for k in ai.option_order],
                    selected_keys=list(ai.selected_keys or []),
                    correct_keys=list(ai.item.correct_keys),
                    explanation=ai.item.explanation, reference=ai.item.reference,
                )
            )
    return ResultView(
        attempt_id=attempt.id, status=attempt.status.value, total_score=attempt.total_score or 0.0,
        passed=bool(attempt.passed), pass_score=attempt.pass_score or 0.0,
        domain_min_ratio=min_ratio, domains=domains, explanations_visible=visible,
        adjusted=bool(load_overrides(db, [ai.item_id for ai in attempt.items])), wrong_items=wrong,
    )
