import pytest
from sqlalchemy import select

from app.models import AttemptStatus, Blueprint, Difficulty, ItemType
from app.services import exam
from app.services.grading import GradeInput, grade_items, score_item
from tests.helpers import domain, make_blueprint, make_item, make_user

S, MU = ItemType.SINGLE, ItemType.MULTI


def gi(code, correct, selected, pts=10.0, t=S):
    return GradeInput(code, t, frozenset(correct), frozenset(selected), pts)


def _two_domain(a_right, a_total, b_right, b_total, pts=1.0):
    rows = []
    for i in range(a_total):
        rows.append(gi("A", "X", "X" if i < a_right else "Y", pts))
    for i in range(b_total):
        rows.append(gi("B", "X", "X" if i < b_right else "Y", pts))
    return rows


# ---------- 단일 문항 ----------
def test_single_correct_wrong_unanswered():
    assert score_item(gi("A", "B", "B")).is_correct
    assert score_item(gi("A", "B", "C")).earned == 0
    r = score_item(gi("A", "B", []))
    assert r.earned == 0 and not r.is_correct


def test_multi_requires_exact_match_by_default():
    ok = gi("A", "AB", "AB", t=MU)
    assert score_item(ok).is_correct
    for sel in ("A", "ABC", "C", "AC"):
        r = score_item(gi("A", "AB", sel, t=MU))
        assert r.earned == 0 and not r.is_correct


def test_multi_partial_credit_setting():
    r = score_item(gi("A", "AB", "A", pts=10, t=MU), partial_credit=True)
    assert r.earned == pytest.approx(5.0) and not r.is_correct
    # 오답 보기를 하나라도 섞으면 0점
    assert score_item(gi("A", "AB", "AC", pts=10, t=MU), partial_credit=True).earned == 0
    # 완전 일치는 만점
    assert score_item(gi("A", "AB", "AB", pts=10, t=MU), partial_credit=True).earned == 10
    # SINGLE 에는 부분 점수 없음
    assert score_item(gi("A", "B", "C"), partial_credit=True).earned == 0


# ---------- 합격 판정 ----------
def test_perfect_score_passes():
    r = grade_items(_two_domain(5, 5, 5, 5), 70, 0.4)
    assert r.total_score == 100 and r.passed and not r.failed_domains


def test_total_score_exactly_pass_line_passes():
    r = grade_items(_two_domain(4, 5, 3, 5), 70, 0.4)  # 7/10
    assert r.total_score == 70 and r.passed


def test_just_below_pass_line_fails():
    r = grade_items(_two_domain(3, 5, 3, 5), 70, 0.4)  # 6/10
    assert r.total_score == 60 and not r.passed and not r.failed_domains


def test_domain_cutoff_fails_even_with_high_total():
    # A 영역 100%, B 영역 20% (총점 높아도 과락)
    rows = _two_domain(9, 9, 1, 5)
    r = grade_items(rows, 70, 0.4)
    assert r.total_score >= 70 and not r.passed and r.failed_domains == ["B"]


def test_domain_ratio_exactly_at_minimum_passes():
    rows = _two_domain(10, 10, 2, 5)  # B = 40%
    r = grade_items(rows, 70, 0.4)
    assert r.domain_scores["B"]["ratio"] == 0.4 and r.passed and not r.failed_domains


def test_domain_just_below_minimum_fails():
    rows = _two_domain(20, 20, 1, 5)  # B = 20%  / 총점 84
    assert not grade_items(rows, 70, 0.4).passed
    rows = _two_domain(20, 20, 2, 6)  # B = 33%
    assert grade_items(rows, 70, 0.4).failed_domains == ["B"]


def test_multiple_failed_domains_reported():
    rows = [gi("A", "X", "Y"), gi("B", "X", "Y"), gi("C", "X", "X", 100)]
    assert grade_items(rows, 50, 0.4).failed_domains == ["A", "B"]


def test_score_normalized_to_100_when_points_do_not_sum_100():
    rows = [gi("A", "X", "X", 1.0), gi("A", "X", "Y", 3.0)]  # 1/4
    assert grade_items(rows, 70, 0).total_score == 25.0


def test_empty_exam_scores_zero_and_fails():
    r = grade_items([], 70, 0.4)
    assert r.total_score == 0 and not r.passed


def test_floating_point_boundary_with_default_points():
    # 기본 배점(2.0/2.5/3.25) 조합에서 부동소수 오차로 경계 판정이 틀리지 않는지
    rows = [gi("A", "X", "X", 2.5)] * 28 + [gi("A", "X", "Y", 2.5)] * 12  # 70.0
    assert grade_items(rows, 70, 0.4).passed
    rows = [gi("A", "X", "X", 3.25)] * 8 + [gi("A", "X", "X", 2.0)] * 12 + [gi("A", "X", "X", 2.5)] * 8 + [
        gi("A", "X", "Y", 2.5)] * 12
    assert grade_items(rows, 70, 0.4).total_score == pytest.approx(26 + 24 + 20)


# ---------- DB 연동 채점(보기 순서 셔플에도 원본 key 기준) ----------
def _answer_all(db, attempt, right_count):
    for n, ai in enumerate(attempt.items):
        if n < right_count:
            exam.save_answer(db, attempt, ai.seq, list(ai.item.correct_keys))
        else:
            wrong = [k for k in ai.option_order if k not in ai.item.correct_keys][:1]
            exam.save_answer(db, attempt, ai.seq, wrong)


def test_end_to_end_all_correct_passes_with_shuffled_options(seeded):
    u = make_user(seeded)
    a, _ = exam.start_attempt(seeded, u)
    assert any(ai.option_order != sorted(ai.option_order) for ai in a.items)  # 실제로 셔플됨
    _answer_all(seeded, a, 40)
    exam.submit_attempt(seeded, a)
    assert a.status == AttemptStatus.SUBMITTED
    assert a.total_score == 100 and a.passed
    assert all(ai.is_correct for ai in a.items)
    assert a.pass_score == 70 and a.domain_min_ratio == 0.4
    assert set(a.domain_scores) == {"ARCH", "INSTALL", "EXTRACT", "REPLICAT", "MAPPING", "OPS", "TROUBLE"}


def test_end_to_end_all_wrong_fails_and_unanswered_counted(seeded):
    u = make_user(seeded)
    a, _ = exam.start_attempt(seeded, u)
    exam.submit_attempt(seeded, a)  # 무응답 제출
    assert a.total_score == 0 and a.passed is False
    assert all(ai.is_correct is False and ai.earned_points == 0 for ai in a.items)


def test_end_to_end_domain_cutoff(db):
    arch, ops = domain(db, "ARCH"), domain(db, "OPS")
    make_blueprint(db, {("ARCH", Difficulty.MID): (5, 10.0), ("OPS", Difficulty.MID): (5, 10.0)})
    for _ in range(5):
        make_item(db, arch)
        make_item(db, ops)
    u = make_user(db)
    a, _ = exam.start_attempt(db, u)
    for ai in a.items:
        is_ops = ai.item.domain_id == ops.id
        if is_ops:
            continue  # OPS 는 전부 무응답 → 0%
        exam.save_answer(db, a, ai.seq, ["A"])
    exam.submit_attempt(db, a)
    assert a.total_score == 50.0 and not a.passed
    assert a.domain_scores["OPS"]["ratio"] == 0 and a.domain_scores["ARCH"]["ratio"] == 1


def test_end_to_end_cutoff_with_high_total(db):
    a_d, b_d = domain(db, "ARCH"), domain(db, "OPS")
    make_blueprint(db, {("ARCH", Difficulty.MID): (9, 10.0), ("OPS", Difficulty.MID): (1, 10.0)}, pass_score=70)
    for _ in range(9):
        make_item(db, a_d)
    make_item(db, b_d)
    u = make_user(db)
    a, _ = exam.start_attempt(db, u)
    for ai in a.items:
        if ai.item.domain_id == a_d.id:
            exam.save_answer(db, a, ai.seq, ["A"])
    exam.submit_attempt(db, a)
    assert a.total_score == 90.0 and a.passed is False  # 과락


def test_multi_partial_credit_via_blueprint_flag(db):
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", Difficulty.MID): (1, 100.0)}, multi_partial_credit=True, pass_score=50)
    make_item(db, d, item_type=ItemType.MULTI, correct=("A", "B"))
    u = make_user(db)
    a, _ = exam.start_attempt(db, u)
    exam.save_answer(db, a, 1, ["A"])
    exam.submit_attempt(db, a)
    assert a.total_score == 50.0 and a.items[0].is_correct is False and a.passed


def test_snapshot_survives_blueprint_change(seeded):
    u = make_user(seeded)
    a, _ = exam.start_attempt(seeded, u)
    exam.submit_attempt(seeded, a)
    bp = seeded.scalar(select(Blueprint))
    bp.pass_score = 10
    seeded.commit()
    assert a.pass_score == 70
