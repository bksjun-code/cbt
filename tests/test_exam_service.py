from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import AttemptItem, AttemptStatus, Difficulty, ExamAttempt, ItemType, utcnow
from app.services import exam
from app.services.exam import AttemptClosed, InvalidAnswer, NoActiveBlueprint
from tests.helpers import domain, make_blueprint, make_item, make_user


def small(db, n_items=4, **bp_kw):
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", Difficulty.MID): (2, 50.0)}, **bp_kw)
    items = [make_item(db, d) for _ in range(n_items)]
    return d, items


def test_start_creates_attempt_with_expiry_and_seed(db):
    small(db, time_limit_min=45)
    u = make_user(db)
    t0 = utcnow()
    a, created = exam.start_attempt(db, u, t0)
    assert created and a.status == AttemptStatus.IN_PROGRESS
    assert a.expires_at == t0 + timedelta(minutes=45) and len(a.seed) == 32
    assert [i.seq for i in a.items] == [1, 2]
    assert db.scalars(select(ExamAttempt)).all() == [a]


def test_start_again_resumes_same_attempt(db):
    small(db)
    u = make_user(db)
    a1, c1 = exam.start_attempt(db, u)
    a2, c2 = exam.start_attempt(db, u)
    assert (c1, c2) == (True, False) and a1.id == a2.id
    assert db.query(ExamAttempt).count() == 1


def test_resume_keeps_saved_answers(db):
    small(db)
    u = make_user(db)
    a, _ = exam.start_attempt(db, u)
    exam.save_answer(db, a, 1, ["B"])
    again, _ = exam.start_attempt(db, u)
    assert again.items[0].selected_keys == ["B"]


def test_expired_active_attempt_is_auto_submitted_then_new_one_created(db):
    small(db)
    u = make_user(db)
    t0 = utcnow()
    a1, _ = exam.start_attempt(db, u, t0)
    exam.save_answer(db, a1, 1, ["A"], t0)
    late = t0 + timedelta(minutes=61)
    a2, created = exam.start_attempt(db, u, late)
    assert created and a2.id != a1.id
    assert a1.status == AttemptStatus.EXPIRED and a1.total_score is not None
    assert a1.submitted_at == a1.expires_at


def test_no_active_blueprint(db):
    make_blueprint(db, {("ARCH", Difficulty.MID): (1, 100.0)}, is_active=False)
    with pytest.raises(NoActiveBlueprint):
        exam.start_attempt(db, make_user(db))


def test_concurrent_start_falls_back_to_existing(db, monkeypatch):
    small(db)
    u = make_user(db)
    existing, _ = exam.start_attempt(db, u)
    real = exam._in_progress
    calls = {"n": 0}

    def flaky(db_, uid):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(db_, uid)

    monkeypatch.setattr(exam, "_in_progress", flaky)
    a, created = exam.start_attempt(db, u)  # 첫 확인은 "없음" → 생성 시도 → 유니크 위반 → 기존 반환
    assert not created and a.id == existing.id
    assert db.query(ExamAttempt).count() == 1


# ---------- 답안 저장 ----------
def test_save_answer_validations(db):
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", Difficulty.MID): (2, 50.0)})
    make_item(db, d, item_type=ItemType.SINGLE)
    make_item(db, d, item_type=ItemType.MULTI, correct=("A", "B"))
    u = make_user(db)
    a, _ = exam.start_attempt(db, u)
    single = next(i for i in a.items if i.item.item_type == ItemType.SINGLE)
    multi = next(i for i in a.items if i.item.item_type == ItemType.MULTI)

    with pytest.raises(InvalidAnswer):
        exam.save_answer(db, a, single.seq, ["A", "B"])
    with pytest.raises(InvalidAnswer):
        exam.save_answer(db, a, single.seq, ["Z"])
    with pytest.raises(InvalidAnswer):
        exam.save_answer(db, a, 99, ["A"])
    exam.save_answer(db, a, multi.seq, ["B", "A", "A"])
    assert multi.selected_keys == ["A", "B"] and multi.answered_at is not None
    exam.save_answer(db, a, multi.seq, [])  # 해제
    assert multi.selected_keys is None and multi.answered_at is None


def test_save_after_submit_rejected(db):
    small(db)
    a, _ = exam.start_attempt(db, make_user(db))
    exam.submit_attempt(db, a)
    with pytest.raises(AttemptClosed):
        exam.save_answer(db, a, 1, ["A"])
    with pytest.raises(AttemptClosed):
        exam.set_flag(db, a, 1, True)


def test_save_within_grace_ok_after_grace_auto_submits(db):
    small(db)
    u = make_user(db)
    t0 = utcnow()
    a, _ = exam.start_attempt(db, u, t0)
    exam.save_answer(db, a, 1, ["A"], a.expires_at + timedelta(seconds=3))  # 유예 이내
    with pytest.raises(AttemptClosed):
        exam.save_answer(db, a, 2, ["A"], a.expires_at + timedelta(seconds=30))
    assert a.status == AttemptStatus.EXPIRED
    assert a.items[1].selected_keys is None  # 만료 후 답안은 반영되지 않음
    assert a.total_score is not None


def test_flag_toggle(db):
    small(db)
    a, _ = exam.start_attempt(db, make_user(db))
    exam.set_flag(db, a, 2, True)
    assert a.items[1].flagged
    exam.set_flag(db, a, 2, False)
    assert not a.items[1].flagged


# ---------- 제출 / 만료 ----------
def test_submit_is_idempotent(db):
    small(db)
    a, _ = exam.start_attempt(db, make_user(db))
    exam.save_answer(db, a, 1, ["A"])
    exam.submit_attempt(db, a)
    first = (a.total_score, a.submitted_at)
    exam.submit_attempt(db, a, utcnow() + timedelta(hours=5))
    assert (a.total_score, a.submitted_at) == first and a.status == AttemptStatus.SUBMITTED


def test_late_submit_is_marked_expired(db):
    small(db)
    t0 = utcnow()
    a, _ = exam.start_attempt(db, make_user(db), t0)
    exam.submit_attempt(db, a, t0 + timedelta(minutes=70))
    assert a.status == AttemptStatus.EXPIRED


def test_expire_overdue_sweeps_only_expired(db):
    small(db, n_items=4)
    t0 = utcnow()
    u1, u2 = make_user(db), make_user(db)
    old, _ = exam.start_attempt(db, u1, t0 - timedelta(minutes=120))
    fresh, _ = exam.start_attempt(db, u2, t0)
    assert exam.expire_overdue(db, t0) == 1
    assert old.status == AttemptStatus.EXPIRED and fresh.status == AttemptStatus.IN_PROGRESS
    assert exam.expire_overdue(db, t0) == 0


def test_attempt_items_primary_key_and_unique_item(db):
    small(db)
    a, _ = exam.start_attempt(db, make_user(db))
    assert len({i.item_id for i in a.items}) == len(a.items)
    assert db.get(AttemptItem, (a.id, 1)) is not None
