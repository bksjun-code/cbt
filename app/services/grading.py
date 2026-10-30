"""채점 및 합격 판정 (서버 전용).

  - 총점은 100점 환산: 득점 합 / 배점 합 × 100
  - 합격 = 총점 ≥ pass_score AND 모든 영역 득점률 ≥ domain_min_ratio (배점이 있는 영역만 대상)
  - MULTI: 기본은 완전 일치만 정답. partial_credit=True 면 오답 보기를 하나도 고르지 않은 경우에 한해
    (선택한 정답 수 / 정답 수) 비율로 부분 점수
  - 문항 보정(ItemOverride): CREDIT_ALL 은 전원 정답 인정, EXCLUDE 는 득점·배점 모두에서 제외
"""
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AttemptStatus, Blueprint, ExamAttempt, ItemOverride, ItemType, OverrideMode, utcnow,
)

EPS = 1e-9


@dataclass(frozen=True)
class GradeInput:
    domain_code: str
    item_type: ItemType
    correct: frozenset[str]
    selected: frozenset[str]
    points: float
    override: OverrideMode | None = None


@dataclass(frozen=True)
class ItemGrade:
    earned: float
    is_correct: bool
    excluded: bool = False


@dataclass(frozen=True)
class GradeResult:
    total_score: float
    passed: bool
    domain_scores: dict[str, dict[str, float]]  # code -> {earned, max, ratio}
    failed_domains: list[str]
    items: list[ItemGrade]


def score_item(g: GradeInput, partial_credit: bool = False) -> ItemGrade:
    if g.override == OverrideMode.EXCLUDE:
        return ItemGrade(0.0, False, excluded=True)
    if g.override == OverrideMode.CREDIT_ALL:
        return ItemGrade(g.points, True)
    if not g.selected:
        return ItemGrade(0.0, False)
    if g.selected == g.correct:
        return ItemGrade(g.points, True)
    if partial_credit and g.item_type == ItemType.MULTI and g.selected <= g.correct:
        return ItemGrade(g.points * len(g.selected) / len(g.correct), False)
    return ItemGrade(0.0, False)


def grade_items(inputs: list[GradeInput], pass_score: float, domain_min_ratio: float,
                partial_credit: bool = False) -> GradeResult:
    graded = [score_item(i, partial_credit) for i in inputs]
    dom: dict[str, dict[str, float]] = {}
    for i, g in zip(inputs, graded):
        d = dom.setdefault(i.domain_code, {"earned": 0.0, "max": 0.0, "ratio": 0.0})
        if g.excluded:
            continue  # 채점 제외: 득점/배점 어느 쪽에도 넣지 않는다
        d["earned"] += g.earned
        d["max"] += i.points
    for d in dom.values():
        d["earned"] = round(d["earned"], 6)
        d["max"] = round(d["max"], 6)
        d["ratio"] = round(d["earned"] / d["max"], 6) if d["max"] > 0 else 0.0

    total_max = sum(d["max"] for d in dom.values())
    total_earned = sum(d["earned"] for d in dom.values())
    total = total_earned / total_max * 100 if total_max > 0 else 0.0

    failed = sorted(c for c, d in dom.items() if d["max"] > 0 and d["ratio"] + EPS < domain_min_ratio)
    passed = total + EPS >= pass_score and not failed
    return GradeResult(round(total, 2), passed, dom, failed, graded)


def load_overrides(db: Session, item_ids: list[int]) -> dict[int, OverrideMode]:
    if not item_ids:
        return {}
    rows = db.scalars(select(ItemOverride).where(ItemOverride.item_id.in_(item_ids)))
    return {o.item_id: o.mode for o in rows}


def _compute(db: Session, attempt: ExamAttempt, pass_score: float, domain_min_ratio: float,
             partial_credit: bool) -> GradeResult:
    overrides = load_overrides(db, [ai.item_id for ai in attempt.items])
    inputs = [
        GradeInput(
            domain_code=ai.item.domain.code, item_type=ai.item.item_type,
            correct=frozenset(ai.item.correct_keys), selected=frozenset(ai.selected_keys or []),
            points=ai.points, override=overrides.get(ai.item_id),
        )
        for ai in attempt.items
    ]
    result = grade_items(inputs, pass_score, domain_min_ratio, partial_credit)
    for ai, g in zip(attempt.items, result.items):
        ai.is_correct = None if g.excluded else g.is_correct  # 제외 문항은 정오 판정 없음
        ai.earned_points = 0.0 if g.excluded else round(g.earned, 6)
    attempt.total_score = result.total_score
    attempt.passed = result.passed
    attempt.domain_scores = result.domain_scores
    return result


def apply_grade(db: Session, attempt: ExamAttempt, bp: Blueprint, status: AttemptStatus,
                now=None) -> GradeResult:
    """attempt 의 저장된 답안을 채점해 결과를 기록한다(커밋은 호출자)."""
    result = _compute(db, attempt, bp.pass_score, bp.domain_min_ratio, bp.multi_partial_credit)
    attempt.pass_score = bp.pass_score
    attempt.domain_min_ratio = bp.domain_min_ratio
    attempt.status = status
    attempt.submitted_at = now or utcnow()
    return result


def regrade_attempt(db: Session, attempt: ExamAttempt) -> GradeResult:
    """종료된 attempt 를 현재 보정 기준으로 재채점. 상태·제출시각·합격선 스냅샷은 유지한다."""
    if attempt.status == AttemptStatus.IN_PROGRESS:
        raise ValueError("진행 중인 시험은 재채점할 수 없습니다")
    bp = db.get(Blueprint, attempt.blueprint_id)
    # 정상 흐름에서는 채점 시점의 스냅샷이 항상 있다. 값이 비어 있는 행이 있어도 보정 기능이 죽지 않도록 Blueprint 값으로 대체
    pass_score = attempt.pass_score if attempt.pass_score is not None else bp.pass_score
    min_ratio = attempt.domain_min_ratio if attempt.domain_min_ratio is not None else bp.domain_min_ratio
    return _compute(db, attempt, pass_score, min_ratio, bp.multi_partial_credit)
