"""문항 채점 보정(결과 수정): 전원 정답 인정 / 채점 제외 → 영향받는 결과 자동 재채점.

  - 사유 필수. 문항당 활성 보정은 1건이며 새로 설정하면 교체된다.
  - 종료된(SUBMITTED/EXPIRED) 응시 결과만 재채점한다. 진행 중 시험은 제출 시점에 보정이 자동 반영된다.
  - 변경 전·후 점수/합격 여부를 audit_logs 에 남긴다(최대 200건 상세, 총 건수는 항상 기록).
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AttemptItem, AttemptStatus, ExamAttempt, Item, ItemOverride, OverrideMode, User,
)
from app.services.audit import log_audit
from app.services.grading import regrade_attempt

DETAIL_CAP = 200


class OverrideError(Exception):
    pass


@dataclass
class AttemptChange:
    attempt_id: int
    score_before: float | None
    score_after: float
    passed_before: bool | None
    passed_after: bool

    @property
    def flipped(self) -> bool:
        return self.passed_before != self.passed_after


@dataclass
class OverrideResult:
    regraded: int = 0
    changes: list[AttemptChange] = field(default_factory=list)

    @property
    def score_changed(self) -> int:
        return sum(1 for c in self.changes if c.score_before != c.score_after)

    @property
    def flipped(self) -> int:
        return sum(1 for c in self.changes if c.flipped)


def get_override(db: Session, item_id: int) -> ItemOverride | None:
    return db.get(ItemOverride, item_id)


def _regrade_item_attempts(db: Session, item_id: int) -> OverrideResult:
    res = OverrideResult()
    attempts = db.scalars(
        select(ExamAttempt).join(AttemptItem, AttemptItem.attempt_id == ExamAttempt.id)
        .where(AttemptItem.item_id == item_id, ExamAttempt.status != AttemptStatus.IN_PROGRESS)
        .order_by(ExamAttempt.id)
    )
    for a in list(attempts):
        before_score, before_passed = a.total_score, a.passed
        r = regrade_attempt(db, a)
        res.regraded += 1
        res.changes.append(AttemptChange(a.id, before_score, r.total_score, before_passed, r.passed))
    return res


def _detail(res: OverrideResult, extra: dict) -> dict:
    changed = [c for c in res.changes if c.score_before != c.score_after or c.flipped]
    return {
        **extra, "regraded": res.regraded, "score_changed": res.score_changed, "pass_flipped": res.flipped,
        "changes": [
            {"attempt": c.attempt_id, "score": [c.score_before, c.score_after], "passed": [c.passed_before, c.passed_after]}
            for c in changed[:DETAIL_CAP]
        ],
        "changes_truncated": len(changed) > DETAIL_CAP,
    }


def set_override(db: Session, item: Item, mode: OverrideMode, reason: str, actor: User) -> OverrideResult:
    reason = (reason or "").strip()
    if not reason:
        raise OverrideError("보정 사유를 입력해야 합니다")
    existing = db.get(ItemOverride, item.id)
    before_mode = existing.mode.value if existing else None
    if existing is None:
        db.add(ItemOverride(item_id=item.id, mode=mode, reason=reason, created_by=actor.id))
    else:
        existing.mode, existing.reason, existing.created_by = mode, reason, actor.id
    db.flush()
    res = _regrade_item_attempts(db, item.id)
    log_audit(db, actor.id, "RESULT_OVERRIDE_SET", f"item:{item.id}",
              _detail(res, {"mode": mode.value, "mode_before": before_mode, "reason": reason}))
    db.flush()
    return res


def revoke_override(db: Session, item: Item, reason: str, actor: User) -> OverrideResult:
    reason = (reason or "").strip()
    if not reason:
        raise OverrideError("해제 사유를 입력해야 합니다")
    existing = db.get(ItemOverride, item.id)
    if existing is None:
        raise OverrideError("적용 중인 보정이 없습니다")
    before_mode = existing.mode.value
    db.delete(existing)
    db.flush()
    res = _regrade_item_attempts(db, item.id)
    log_audit(db, actor.id, "RESULT_OVERRIDE_REVOKED", f"item:{item.id}",
              _detail(res, {"mode_before": before_mode, "reason": reason}))
    db.flush()
    return res
