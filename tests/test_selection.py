from collections import Counter

import pytest
from sqlalchemy import select

from app.models import (
    AuditLog, Blueprint, Difficulty, ExamAttempt, ItemStatus,
)
from app.services import exam
from app.services.selection import (
    DIFF_RANK, InsufficientItemsError, build_plan, cell_statuses, recent_item_ids,
)
from tests.helpers import domain, make_blueprint, make_item, make_user

L, M, H = Difficulty.LOW, Difficulty.MID, Difficulty.HIGH


# ---------- 비율 준수 ----------
def test_plan_matches_blueprint_cells_and_points(seeded):
    bp = seeded.scalar(select(Blueprint))
    plan = build_plan(seeded, bp, set(), "seed-1")
    assert len(plan.items) == 40
    need = Counter({(r.domain_id, r.difficulty): r.item_count for r in bp.rules})
    got = Counter((p.domain_id, p.difficulty) for p in plan.items)
    assert got == need
    assert sum(p.points for p in plan.items) == pytest.approx(100.0)
    diff = Counter(p.difficulty for p in plan.items)
    assert diff == {L: 12, M: 20, H: 8}


def test_only_approved_items_are_used(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", M): (2, 50.0)})
    good = [make_item(db, d, M), make_item(db, d, M)]
    for st in (ItemStatus.DRAFT, ItemStatus.REVIEWED, ItemStatus.RETIRED):
        make_item(db, d, M, status=st)
    for s in range(30):
        plan = build_plan(db, bp, set(), f"s{s}")
        assert {p.item_id for p in plan.items} == {i.id for i in good}


# ---------- variant_group ----------
def test_no_duplicate_variant_group_in_default_exam(seeded):
    bp = seeded.scalar(select(Blueprint))
    for s in range(50):
        plan = build_plan(seeded, bp, set(), f"seed-{s}")
        groups = [p.variant_group for p in plan.items]
        assert len(groups) == len(set(groups))


def test_variant_group_within_cell_counts_once(db):
    d = domain(db, "EXTRACT")
    bp = make_blueprint(db, {("EXTRACT", M): (2, 50.0)})
    for g in ("X", "X", "X", "Y"):
        make_item(db, d, M, group=g)
    for s in range(20):
        plan = build_plan(db, bp, set(), f"s{s}")
        assert {p.variant_group for p in plan.items} == {"X", "Y"}


def test_variant_group_not_enough_distinct_groups_is_shortage(db):
    d = domain(db, "EXTRACT")
    bp = make_blueprint(db, {("EXTRACT", M): (2, 50.0)})
    for _ in range(5):
        make_item(db, d, M, group="SAME")  # 문항은 5개지만 그룹은 1개
    with pytest.raises(InsufficientItemsError) as e:
        build_plan(db, bp, set(), "s")
    assert e.value.shortages[0].distinct_groups == 1


def test_cross_cell_group_conflict_is_avoided_when_feasible(db):
    a, b = domain(db, "ARCH"), domain(db, "OPS")
    bp = make_blueprint(db, {("ARCH", L): (2, 25.0), ("OPS", M): (1, 50.0)})
    for g in ("g1", "g2", "g3"):
        make_item(db, a, L, group=g)
    make_item(db, b, M, group="g1")  # OPS 는 g1 밖에 없음 → ARCH 는 g2,g3 를 써야 함
    for s in range(40):
        plan = build_plan(db, bp, set(), f"s{s}")
        groups = [p.variant_group for p in plan.items]
        assert sorted(groups) == ["g1", "g2", "g3"]


def test_cross_cell_group_conflict_infeasible_raises(db):
    a, b = domain(db, "ARCH"), domain(db, "OPS")
    bp = make_blueprint(db, {("ARCH", L): (2, 25.0), ("OPS", M): (1, 50.0)})
    make_item(db, a, L, group="g1")
    make_item(db, a, L, group="g2")
    make_item(db, b, M, group="g1")
    make_item(db, b, M, group="g2")
    # ARCH 가 g1,g2 를 모두 써야 하므로 OPS 는 쓸 그룹이 없다
    with pytest.raises(InsufficientItemsError):
        build_plan(db, bp, set(), "s")


# ---------- 최근 출제 문항 제외 ----------
def _take(db, user, now=None):
    a, _ = exam.start_attempt(db, user, now)
    exam.submit_attempt(db, a, now)
    return a


def test_recent_items_excluded_when_pool_allows(db):
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", M): (2, 50.0)}, recent_exclude_n=2)
    for _ in range(6):
        make_item(db, d, M)
    u = make_user(db)
    a1, a2, a3 = _take(db, u), _take(db, u), _take(db, u)
    s1, s2, s3 = ({i.item_id for i in a.items} for a in (a1, a2, a3))
    assert not s2 & s1
    assert not s3 & (s1 | s2)  # 최근 2회와 겹치지 않음
    assert not db.scalars(select(AuditLog).where(AuditLog.action == "EXAM_SELECTION_RELAXED")).first()


def test_recent_exclusion_relaxed_and_audited_when_insufficient(db):
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", M): (2, 50.0)}, recent_exclude_n=2)
    for _ in range(3):
        make_item(db, d, M)
    u = make_user(db)
    _take(db, u)
    a2 = _take(db, u)  # 신규 가능 문항 1개뿐 → 최근 문항 1개 재사용
    assert len(a2.items) == 2
    log = db.scalars(select(AuditLog).where(AuditLog.action == "EXAM_SELECTION_RELAXED")).one()
    assert log.user_id == u.id and log.target == f"attempt:{a2.id}"
    cell = log.detail["cells"][0]
    assert cell["fresh"] == 1 and cell["reused_recent"] == 1


def test_recent_exclusion_zero_disabled(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", M): (2, 50.0)}, recent_exclude_n=0)
    for _ in range(2):
        make_item(db, d, M)
    u = make_user(db)
    _take(db, u)
    assert recent_item_ids(db, u.id, bp.recent_exclude_n) == set()
    _take(db, u)  # 완화 없이도 성공(제외 안 함)
    assert not db.scalars(select(AuditLog).where(AuditLog.action == "EXAM_SELECTION_RELAXED")).first()


def test_recent_exclusion_is_per_user(db):
    d = domain(db, "ARCH")
    make_blueprint(db, {("ARCH", M): (2, 50.0)}, recent_exclude_n=2)
    for _ in range(3):
        make_item(db, d, M)
    u1, u2 = make_user(db), make_user(db)
    _take(db, u1)
    _take(db, u2)
    assert not db.scalars(select(AuditLog).where(AuditLog.action == "EXAM_SELECTION_RELAXED").where(AuditLog.user_id == u2.id)).first()


# ---------- seed 재현성 ----------
def _sig(plan):
    return [(p.item_id, tuple(p.option_order)) for p in plan.items]


def test_same_seed_same_plan(seeded):
    bp = seeded.scalar(select(Blueprint))
    assert _sig(build_plan(seeded, bp, set(), "fixed")) == _sig(build_plan(seeded, bp, set(), "fixed"))


def test_different_seeds_give_different_plans(seeded):
    bp = seeded.scalar(select(Blueprint))
    sigs = {tuple(_sig(build_plan(seeded, bp, set(), f"seed-{i}"))) for i in range(10)}
    assert len(sigs) > 5


def test_saved_attempt_can_be_replayed_from_its_seed(seeded):
    u = make_user(seeded)
    a1 = _take(seeded, u)
    a2 = _take(seeded, u)  # a2 시점의 제외 집합 = a2 이전의 최근 N회
    bp = seeded.get(Blueprint, a2.blueprint_id)
    excluded = recent_item_ids(seeded, u.id, bp.recent_exclude_n, before_attempt_id=a2.id)
    replay = build_plan(seeded, bp, excluded, a2.seed)
    assert [(p.item_id, p.option_order, p.points) for p in replay.items] == [
        (i.item_id, i.option_order, i.points) for i in a2.items
    ]
    assert a1.seed != a2.seed and len(a2.seed) == 32


# ---------- 순서 / 보기 셔플 ----------
def test_difficulty_sorted_when_option_on(seeded):
    bp = seeded.scalar(select(Blueprint))
    bp.sort_by_difficulty = True
    for s in range(10):
        ranks = [DIFF_RANK[p.difficulty] for p in build_plan(seeded, bp, set(), f"s{s}").items]
        assert ranks == sorted(ranks)


def test_difficulty_not_sorted_when_option_off(seeded):
    bp = seeded.scalar(select(Blueprint))
    bp.sort_by_difficulty = False
    unsorted = 0
    for s in range(10):
        ranks = [DIFF_RANK[p.difficulty] for p in build_plan(seeded, bp, set(), f"s{s}").items]
        unsorted += ranks != sorted(ranks)
    assert unsorted > 0


def test_option_order_is_permutation_and_shuffled(seeded):
    bp = seeded.scalar(select(Blueprint))
    plan = build_plan(seeded, bp, set(), "x")
    non_identity = 0
    for p in plan.items:
        assert sorted(p.option_order) == list("ABCDE")
        non_identity += p.option_order != list("ABCDE")
    assert non_identity > 20


# ---------- 문항 부족 ----------
def test_shortage_refuses_exam_and_reports_cells(seeded):
    from app.models import Item

    arch = domain(seeded, "ARCH")
    for it in seeded.scalars(select(Item).where(Item.domain_id == arch.id, Item.difficulty == H)):
        it.status = ItemStatus.RETIRED
    seeded.flush()
    bp = seeded.scalar(select(Blueprint))
    with pytest.raises(InsufficientItemsError) as e:
        build_plan(seeded, bp, set(), "s")
    assert [(s.domain_code, s.difficulty) for s in e.value.shortages] == [("ARCH", H)]
    assert "ARCH/HIGH" in str(e.value)

    u = make_user(seeded)
    with pytest.raises(InsufficientItemsError):
        exam.start_attempt(seeded, u)
    assert seeded.scalars(select(ExamAttempt).where(ExamAttempt.user_id == u.id)).first() is None
    log = seeded.scalars(select(AuditLog).where(AuditLog.action == "EXAM_CREATE_REFUSED")).one()
    assert log.detail["shortages"][0]["domain"] == "ARCH"


def test_cell_statuses_dashboard_data(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", L): (2, 25.0), ("ARCH", M): (1, 50.0)})
    make_item(db, d, L)
    make_item(db, d, M)
    make_item(db, d, M)
    st = {(s.domain_code, s.difficulty): s for s in cell_statuses(db, bp)}
    assert st[("ARCH", L)].shortage and not st[("ARCH", M)].shortage
    assert st[("ARCH", M)].approved_items == 2


def test_zero_count_rules_are_ignored(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", L): (0, 2.0), ("ARCH", M): (1, 100.0)})
    make_item(db, d, M)
    plan = build_plan(db, bp, set(), "s")
    assert len(plan.items) == 1


# ---------- 문항 선택의 무작위성(변이 테스트에서 드러난 공백) ----------
def test_which_items_get_picked_varies_with_seed_and_is_roughly_uniform(db):
    """'접속할 때마다 다른 문제 세트' 의 핵심: 보기/순서가 아니라 **어떤 문항이 뽑히는지** 가 시드마다 달라야 하고 한쪽으로 쏠리지 않아야 한다."""
    from collections import Counter

    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", Difficulty.MID): (2, 50.0)})
    items = [make_item(db, d, Difficulty.MID) for _ in range(6)]
    counts = Counter()
    n = 600
    for s in range(n):
        for p in build_plan(db, bp, set(), f"seed-{s}").items:
            counts[p.item_id] += 1
    assert set(counts) == {i.id for i in items}                      # 모든 문항이 한 번쯤은 뽑힌다
    expected = n * 2 / 6                                             # 문항당 기대 선택 횟수 200 (σ≈11.5)
    assert all(abs(c - expected) < 50 for c in counts.values()), dict(counts)  # ±50 ≈ 4σ: 쏠림이 없다
    sets = {frozenset(p.item_id for p in build_plan(db, bp, set(), f"seed-{s}").items) for s in range(60)}
    assert len(sets) > 5                                             # 문제 세트(조합)도 다양하다
