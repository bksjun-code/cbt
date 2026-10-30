"""응시 라이프사이클: 시작(이어하기), 답안 저장, 검토 표시, 제출, 만료 처리.

서버의 expires_at 이 유일한 기준이다. 네트워크 지연을 감안해 GRACE 이내 요청은 허용한다.
"""
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AttemptItem, AttemptStatus, Blueprint, ExamAttempt, ItemType, User, utcnow,
)
from app.services.audit import log_audit
from app.services.grading import apply_grade
from app.services.selection import InsufficientItemsError, build_plan, recent_item_ids

GRACE = timedelta(seconds=5)


class NoActiveBlueprint(Exception):
    pass


class AttemptClosed(Exception):
    """이미 제출/만료되었거나 시간이 지난 시험에 대한 변경 시도."""


class InvalidAnswer(ValueError):
    pass


def is_expired(attempt: ExamAttempt, now: datetime) -> bool:
    return now > attempt.expires_at + GRACE


def get_active_blueprint(db: Session) -> Blueprint:
    bp = db.scalar(select(Blueprint).where(Blueprint.is_active.is_(True)).order_by(Blueprint.id.desc()))
    if bp is None:
        raise NoActiveBlueprint("활성 Blueprint 가 없습니다")
    return bp


def _in_progress(db: Session, user_id: int) -> ExamAttempt | None:
    return db.scalar(
        select(ExamAttempt).where(ExamAttempt.user_id == user_id, ExamAttempt.status == AttemptStatus.IN_PROGRESS)
    )


def finalize_attempt(db: Session, attempt: ExamAttempt, status: AttemptStatus, now: datetime | None = None) -> ExamAttempt:
    """채점 후 종료. 이미 종료된 attempt 는 그대로 반환(멱등)."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return attempt
    bp = db.get(Blueprint, attempt.blueprint_id)
    apply_grade(db, attempt, bp, status, now)
    db.commit()
    return attempt


def start_attempt(db: Session, user: User, now: datetime | None = None) -> tuple[ExamAttempt, bool]:
    """진행 중 시험이 있으면 이어서(created=False), 없으면 새로 출제(created=True)."""
    now = now or utcnow()
    active = _in_progress(db, user.id)
    if active is not None:
        if not is_expired(active, now):
            return active, False
        finalize_attempt(db, active, AttemptStatus.EXPIRED, active.expires_at)

    bp = get_active_blueprint(db)
    excluded = recent_item_ids(db, user.id, bp.recent_exclude_n)
    seed = secrets.token_hex(16)
    try:
        plan = build_plan(db, bp, excluded, seed)
    except InsufficientItemsError as e:
        log_audit(db, user.id, "EXAM_CREATE_REFUSED", f"blueprint:{bp.id}", {
            "shortages": [
                {"domain": s.domain_code, "difficulty": s.difficulty.value,
                 "needed": s.needed, "available": s.distinct_groups}
                for s in e.shortages
            ]
        })
        db.commit()  # 거부 사실은 반드시 남긴다
        raise

    if not plan.items:  # 규칙이 비어 있는 Blueprint(DB 직접 수정 등)로 문항 0개짜리 시험이 만들어지는 것을 막는다
        log_audit(db, user.id, "EXAM_CREATE_REFUSED", f"blueprint:{bp.id}", {"reason": "empty_blueprint"})
        db.commit()
        raise NoActiveBlueprint("활성 Blueprint 에 출제 규칙이 없습니다")

    attempt = ExamAttempt(
        user_id=user.id, blueprint_id=bp.id, seed=seed, started_at=now,
        expires_at=now + timedelta(minutes=bp.time_limit_min),
    )
    attempt.items = [
        AttemptItem(seq=i, item_id=p.item_id, option_order=p.option_order, points=p.points)
        for i, p in enumerate(plan.items, start=1)
    ]
    try:
        with db.begin_nested():
            db.add(attempt)
            db.flush()
    except IntegrityError:
        # 동시 요청으로 다른 요청이 먼저 생성 → 그 attempt 로 이어하기
        existing = _in_progress(db, user.id)
        if existing is None:
            raise
        return existing, False

    if plan.relaxations:
        log_audit(db, user.id, "EXAM_SELECTION_RELAXED", f"attempt:{attempt.id}", {
            "recent_exclude_n": bp.recent_exclude_n,
            "cells": [
                {"domain_id": r.domain_id, "difficulty": r.difficulty.value, "needed": r.needed,
                 "fresh": r.fresh_picked, "reused_recent": r.reused_picked}
                for r in plan.relaxations
            ],
        })
    log_audit(db, user.id, "EXAM_STARTED", f"attempt:{attempt.id}", {"seed": seed, "blueprint_id": bp.id})
    db.commit()
    return attempt, True


def _open_item(db: Session, attempt: ExamAttempt, seq: int, now: datetime) -> AttemptItem:
    """변경 가능한 상태인지 확인하고 AttemptItem 반환. 만료됐다면 자동 제출 후 AttemptClosed."""
    if attempt.status != AttemptStatus.IN_PROGRESS:
        raise AttemptClosed("이미 종료된 시험입니다")
    if is_expired(attempt, now):
        finalize_attempt(db, attempt, AttemptStatus.EXPIRED, attempt.expires_at)
        raise AttemptClosed("제한시간이 지나 자동 제출되었습니다")
    ai = db.get(AttemptItem, (attempt.id, seq))
    if ai is None:
        raise InvalidAnswer("존재하지 않는 문항 번호입니다")
    return ai


def save_answer(db: Session, attempt: ExamAttempt, seq: int, selected_keys: list[str],
                now: datetime | None = None) -> AttemptItem:
    """임시 저장. 빈 목록은 선택 해제. 키는 원본 보기 key 기준."""
    now = now or utcnow()
    ai = _open_item(db, attempt, seq, now)
    keys = list(dict.fromkeys(selected_keys))  # 중복 제거(순서 유지)
    if not set(keys) <= set(ai.option_order):
        raise InvalidAnswer("존재하지 않는 보기입니다")
    if ai.item.item_type != ItemType.MULTI and len(keys) > 1:
        raise InvalidAnswer("단일 선택 문항입니다")
    ai.selected_keys = sorted(keys) if keys else None
    ai.answered_at = now if keys else None
    db.commit()
    return ai


def set_flag(db: Session, attempt: ExamAttempt, seq: int, flagged: bool, now: datetime | None = None) -> AttemptItem:
    ai = _open_item(db, attempt, seq, now or utcnow())
    ai.flagged = flagged
    db.commit()
    return ai


def submit_attempt(db: Session, attempt: ExamAttempt, now: datetime | None = None) -> ExamAttempt:
    """제출. 유예시간을 넘겼다면 EXPIRED 로 처리(저장된 답안까지만 채점)."""
    now = now or utcnow()
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return attempt
    if is_expired(attempt, now):
        return finalize_attempt(db, attempt, AttemptStatus.EXPIRED, attempt.expires_at)
    return finalize_attempt(db, attempt, AttemptStatus.SUBMITTED, now)


def expire_overdue(db: Session, now: datetime | None = None) -> int:
    """스케줄러용: 만료된 진행 중 시험을 모두 자동 제출 처리. 처리 건수 반환."""
    now = now or utcnow()
    overdue = db.scalars(select(ExamAttempt).where(ExamAttempt.status == AttemptStatus.IN_PROGRESS))
    n = 0
    for a in list(overdue):
        if is_expired(a, now):
            finalize_attempt(db, a, AttemptStatus.EXPIRED, a.expires_at)
            n += 1
    return n
