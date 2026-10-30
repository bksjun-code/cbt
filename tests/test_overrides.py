import pytest
from sqlalchemy import select

from app.models import AttemptStatus, AuditLog, Difficulty, ItemOverride, ItemType, OverrideMode, Role
from app.services import exam
from app.services.grade_overrides import OverrideError, get_override, revoke_override, set_override
from app.services.grading import GradeInput, grade_items
from app.services.results import build_result
from tests.helpers import domain, make_blueprint, make_item, make_user

S = ItemType.SINGLE
CREDIT, EXCL = OverrideMode.CREDIT_ALL, OverrideMode.EXCLUDE


def gi(code, correct, selected, pts=10.0, ov=None):
    return GradeInput(code, S, frozenset(correct), frozenset(selected), pts, ov)


# ---------- 순수 채점 ----------
def test_credit_all_counts_wrong_and_blank_as_correct():
    r = grade_items([gi("A", "X", "Y", ov=CREDIT), gi("A", "X", [], ov=CREDIT), gi("A", "X", "Y")], 70, 0)
    assert [g.is_correct for g in r.items] == [True, True, False]
    assert r.items[1].earned == 10 and r.total_score == pytest.approx(66.67)


def test_exclude_removes_item_from_earned_and_max():
    r = grade_items([gi("A", "X", "X"), gi("A", "X", "Y", ov=EXCL)], 70, 0)
    assert r.items[1].excluded and r.total_score == 100
    assert r.domain_scores["A"]["max"] == 10  # 만점에서도 제외


def test_excluding_a_whole_domain_does_not_cause_cutoff_failure():
    rows = [gi("A", "X", "X", 10), gi("B", "X", "Y", 10, ov=EXCL)]
    r = grade_items(rows, 70, 0.4)
    assert r.passed and r.failed_domains == [] and r.domain_scores["B"]["max"] == 0


def test_override_can_flip_cutoff_failure_to_pass():
    rows = [gi("A", "X", "X", 10)] * 9 + [gi("B", "X", "Y", 10)]
    assert not grade_items(rows, 70, 0.4).passed  # B 영역 0% 과락
    rows = [gi("A", "X", "X", 10)] * 9 + [gi("B", "X", "Y", 10, ov=CREDIT)]
    assert grade_items(rows, 70, 0.4).passed


def test_all_items_excluded_scores_zero_without_crash():
    r = grade_items([gi("A", "X", "X", ov=EXCL)], 70, 0.4)
    assert r.total_score == 0 and not r.passed


# ---------- DB 연동 ----------
@pytest.fixture()
def world(db):
    """ARCH MID 2문항(각 50점). u1 만점, u2 무응답, u3 시간 만료(1문항만 정답), u4 진행 중."""
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", Difficulty.MID): (2, 50.0)}, pass_score=70, recent_exclude_n=0)
    items = [make_item(db, d, correct=("A",)), make_item(db, d, correct=("B",))]
    admin = make_user(db, role=Role.ADMIN)
    users = [make_user(db) for _ in range(4)]

    def correct(a):
        for ai in a.items:
            exam.save_answer(db, a, ai.seq, list(ai.item.correct_keys))

    a1, _ = exam.start_attempt(db, users[0])
    correct(a1)
    exam.submit_attempt(db, a1)
    a2, _ = exam.start_attempt(db, users[1])
    exam.submit_attempt(db, a2)
    a3, _ = exam.start_attempt(db, users[2])
    exam.save_answer(db, a3, 1, list(a3.items[0].item.correct_keys))
    exam.finalize_attempt(db, a3, AttemptStatus.EXPIRED)
    a4, _ = exam.start_attempt(db, users[3])
    return db, admin, items, (a1, a2, a3, a4)


def test_set_override_regrades_finished_attempts_only(world):
    db, admin, items, (a1, a2, a3, a4) = world
    assert (a1.total_score, a2.total_score, a3.total_score) == (100, 0, 50)
    target = items[0]
    res = set_override(db, target, CREDIT, "정답 키 오류", admin)
    db.commit()
    assert res.regraded == 3 and a4.status == AttemptStatus.IN_PROGRESS and a4.total_score is None
    assert a1.total_score == 100                      # 이미 정답: 변화 없음
    assert a2.total_score == 50 and a2.passed is False  # 0 → 50
    # a3 는 seq1 문항만 정답으로 저장: 보정 대상이 그 문항이면 그대로(50), 아니면 나머지 문항을 인정받아 100
    assert a3.total_score == (50 if a3.items[0].item_id == target.id else 100)
    assert res.score_changed == (1 if a3.items[0].item_id == target.id else 2)  # a2 (+ a3)


def test_override_flips_pass_and_is_audited_with_before_after(world):
    db, admin, items, (a1, a2, a3, a4) = world
    set_override(db, items[0], CREDIT, "복수 정답 가능", admin)
    res = set_override(db, items[1], CREDIT, "복수 정답 가능", admin)  # 두 문항 모두 인정
    db.commit()
    assert a2.total_score == 100 and a2.passed is True
    log = db.scalars(select(AuditLog).where(AuditLog.action == "RESULT_OVERRIDE_SET").order_by(AuditLog.id.desc())).first()
    d = log.detail
    assert log.target == f"item:{items[1].id}" and d["mode"] == "CREDIT_ALL" and d["reason"] == "복수 정답 가능"
    assert d["regraded"] == 3 and d["pass_flipped"] >= 1
    assert any(c["attempt"] == a2.id and c["score"] == [50.0, 100.0] and c["passed"] == [False, True] for c in d["changes"])
    assert res.flipped >= 1 and d["changes_truncated"] is False


def test_exclude_mode_marks_item_and_normalizes_score(world):
    db, admin, items, (a1, a2, a3, a4) = world
    set_override(db, items[0], EXCL, "문항 모호", admin)
    db.commit()
    for a in (a1, a2, a3):
        ex = next(ai for ai in a.items if ai.item_id == items[0].id)
        assert ex.is_correct is None and ex.earned_points == 0
    assert a1.total_score == 100                         # 남은 1문항 만점
    assert a2.total_score == 0 and a2.domain_scores["ARCH"]["max"] == 50
    res = build_result(db, a2)
    assert res.adjusted and all(w.seq != next(ai.seq for ai in a2.items if ai.item_id == items[0].id) for w in res.wrong_items)
    assert len(res.wrong_items) == 1


def test_replace_then_revoke_restores_original_scores(world):
    db, admin, items, (a1, a2, a3, a4) = world
    before = [(a.total_score, a.passed) for a in (a1, a2, a3)]
    set_override(db, items[0], CREDIT, "사유1", admin)
    set_override(db, items[0], EXCL, "사유2로 교체", admin)
    assert get_override(db, items[0].id).mode == EXCL and db.query(ItemOverride).count() == 1
    revoke_override(db, items[0], "보정 철회", admin)
    db.commit()
    assert [(a.total_score, a.passed) for a in (a1, a2, a3)] == before
    assert get_override(db, items[0].id) is None
    assert not build_result(db, a2).adjusted
    assert [x.action for x in db.scalars(select(AuditLog).where(AuditLog.action.like("RESULT_OVERRIDE%")))] == [
        "RESULT_OVERRIDE_SET", "RESULT_OVERRIDE_SET", "RESULT_OVERRIDE_REVOKED"]


def test_reason_required_and_revoke_needs_existing(world):
    db, admin, items, _ = world
    for bad in ("", "   ", None):
        with pytest.raises(OverrideError):
            set_override(db, items[0], CREDIT, bad, admin)
    with pytest.raises(OverrideError, match="적용 중인 보정이 없습니다"):
        revoke_override(db, items[0], "사유", admin)
    set_override(db, items[0], CREDIT, "사유", admin)
    with pytest.raises(OverrideError):
        revoke_override(db, items[0], " ", admin)
    assert get_override(db, items[0].id).mode == CREDIT


def test_in_progress_attempt_picks_up_override_on_submit(world):
    db, admin, items, (a1, a2, a3, a4) = world
    set_override(db, items[0], CREDIT, "사전 보정", admin)
    set_override(db, items[1], CREDIT, "사전 보정", admin)
    db.commit()
    assert a4.total_score is None
    exam.submit_attempt(db, a4)  # 무응답 제출이지만 두 문항 모두 정답 인정
    assert a4.total_score == 100 and a4.passed is True


def test_override_does_not_change_status_or_submit_time_or_snapshot(world):
    db, admin, items, (a1, a2, a3, a4) = world
    snap = [(a.status, a.submitted_at, a.pass_score, a.domain_min_ratio) for a in (a1, a2, a3)]
    set_override(db, items[0], CREDIT, "사유", admin)
    assert [(a.status, a.submitted_at, a.pass_score, a.domain_min_ratio) for a in (a1, a2, a3)] == snap


def test_regrade_in_progress_attempt_rejected(world):
    from app.services.grading import regrade_attempt

    db, _, _, (_, _, _, a4) = world
    with pytest.raises(ValueError):
        regrade_attempt(db, a4)


def test_regrade_falls_back_to_blueprint_when_snapshot_missing(world):
    db, admin, items, (a1, a2, a3, a4) = world
    a2.pass_score = None
    a2.domain_min_ratio = None
    set_override(db, items[0], CREDIT, "스냅샷 없는 행", admin)
    set_override(db, items[1], CREDIT, "스냅샷 없는 행", admin)
    assert a2.total_score == 100 and a2.passed is True  # Blueprint(70점)로 판정
