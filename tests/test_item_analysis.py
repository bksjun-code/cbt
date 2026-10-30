from datetime import timedelta

import pytest

from app.models import (
    AttemptItem, AttemptStatus, Difficulty, ExamAttempt, ItemOverride, ItemStatus, ItemType, OverrideMode, utcnow,
)
from app.services import item_analysis as svc
from tests.helpers import domain, make_blueprint, make_item, make_user


@pytest.fixture()
def world(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", Difficulty.MID): (1, 100.0)})
    user = make_user(db)
    item = make_item(db, d, correct=("B",))
    return db, d, bp, user, item


def add(db, user, bp, item, specs, status=AttemptStatus.SUBMITTED):
    """specs: [(총점, 선택 키 목록|None)] — 응시 1건당 해당 문항 1개만 가진 종료 응시를 만든다."""
    for score, sel in specs:
        a = ExamAttempt(user_id=user.id, blueprint_id=bp.id, seed="s", expires_at=utcnow() + timedelta(hours=1),
                        status=status, total_score=score, passed=score >= 70)
        a.items = [AttemptItem(seq=1, item_id=item.id, option_order=list("ABCDE"), points=10, selected_keys=sel)]
        db.add(a)
        db.flush()


def stat(db, item, **kw):
    return svc.analyze(db, **kw)[item.id]


# ---------- 기본 통계 ----------
def test_p_counts_correct_blank_and_wrong(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(50, ["B"])] * 6 + [(50, ["A"])] * 2 + [(50, None)] * 2)
    s = stat(db, item)
    assert (s.n, s.correct, s.blank) == (10, 6, 2) and s.p == pytest.approx(0.6) and s.p_label == "보통"


@pytest.mark.parametrize("correct,expect", [(9, "쉬움"), (8, "쉬움"), (5, "보통"), (3, "보통"), (2, "어려움"), (0, "어려움")])
def test_p_labels(world, correct, expect):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(50, ["B"])] * correct + [(50, ["A"])] * (10 - correct))
    assert stat(db, item).p_label == expect


def test_unknown_key_in_selection_counts_wrong_and_is_ignored(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(50, ["Z"]), (50, ["B"])])
    s = stat(db, item)
    assert s.correct == 1 and {o.key for o in s.options} == {"A", "B", "C", "D", "E"} and all(o.key != "Z" for o in s.options)


def test_in_progress_attempts_excluded(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", Difficulty.MID): (1, 100.0)})
    item = make_item(db, d)
    u1, u2 = make_user(db), make_user(db)
    add(db, u1, bp, item, [(50, ["B"])])
    add(db, u2, bp, item, [(0, ["B"])], status=AttemptStatus.IN_PROGRESS)
    assert stat(db, item).n == 1


def test_no_data_returns_empty_and_item_filter(world):
    db, d, bp, u, item = world
    assert svc.analyze(db) == {}
    other = make_item(db, d)
    add(db, u, bp, item, [(50, ["B"])])
    add(db, u, bp, other, [(50, ["B"])])
    assert set(svc.analyze(db)) == {item.id, other.id} and set(svc.analyze(db, item_ids=[other.id])) == {other.id}


def test_expired_attempts_are_included(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(50, ["B"])], status=AttemptStatus.EXPIRED)
    assert stat(db, item).n == 1


# ---------- 변별도 ----------
@pytest.mark.parametrize("n,k", [(100, 27), (30, 8), (10, 3), (3, 1), (1, 1), (50, 14)])
def test_group_size(n, k):
    assert svc.group_size(n) == k


def test_perfect_discrimination(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(score, ["B"] if score > 50 else ["A"]) for score in range(1, 101)])
    s = stat(db, item)
    assert s.group_n == 27 and s.p_upper == 1 and s.p_lower == 0 and s.d == 1 and s.enough and not s.warn and s.p == 0.5


def test_zero_discrimination_warns(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(score, ["B"] if score % 2 == 0 else ["A"]) for score in range(1, 101)])
    s = stat(db, item)
    assert s.d == pytest.approx(1 / 27, abs=1e-3) and s.warn and not s.negative  # 14/27 - 13/27


def test_negative_discrimination(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(score, ["B"] if score <= 50 else ["A"]) for score in range(1, 101)])
    s = stat(db, item)
    assert s.d == -1 and s.negative and s.warn


def test_warning_threshold_boundary(world):
    db, _, bp, u, item = world
    # n=50 → k=14. 상위 14 정답, 하위 정답 11 → D = 3/14 ≈ 0.214 (경고 아님), 하위 정답 12 → D = 2/14 ≈ 0.143 (경고)
    for lower_correct, warn in ((11, False), (12, True)):
        specs = []
        for score in range(1, 51):
            top = score > 36
            bottom = score <= 14
            right = top or (bottom and score <= lower_correct) or (not top and not bottom and score % 2 == 0)
            specs.append((score, ["B"] if right else ["A"]))
        it = make_item(db, domain(db, "OPS"), correct=("B",))
        add(db, u, bp, it, specs)
        s = svc.analyze(db, item_ids=[it.id])[it.id]
        assert s.group_n == 14 and s.warn is warn, (lower_correct, s.d)


def test_small_sample_computes_d_but_never_warns(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(score, ["B"] if score <= 10 else ["A"]) for score in range(1, 21)])  # D 음수지만 n=20
    s = stat(db, item)
    assert s.n == 20 and s.d < 0 and not s.enough and not s.warn and not s.negative
    s2 = stat(db, item, min_sample=10)
    assert s2.enough and s2.warn and s2.negative


def test_ties_are_deterministic_and_use_attempt_order(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(50, ["B"] if i < 20 else ["A"]) for i in range(40)])  # 전원 동점
    a, b = stat(db, item), stat(db, item)
    assert (a.d, a.p_upper, a.p_lower) == (b.d, b.p_upper, b.p_lower)
    assert a.group_n == 11 and a.p_upper == 1 and a.p_lower == 0  # 동점은 attempt id 순: 앞쪽이 '상위'


def test_single_attempt(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(80, ["B"])])
    s = stat(db, item)
    assert s.n == 1 and s.group_n == 1 and s.d == 0 and not s.enough


# ---------- 보기 분포 ----------
def test_option_distribution_counts_pct_and_blank(world):
    db, _, bp, u, item = world
    sel = ["A"] * 20 + ["B"] * 60 + ["D"] * 5 + ["E"] * 5 + [None] * 10
    add(db, u, bp, item, [(i + 1, [k] if k else None) for i, k in enumerate(sel)])
    s = stat(db, item)
    o = {x.key: x for x in s.options}
    assert [(k, o[k].count) for k in "ABCDE"] == [("A", 20), ("B", 60), ("C", 0), ("D", 5), ("E", 5)]
    assert o["B"].pct == 0.6 and o["B"].is_correct and not o["A"].is_correct and s.blank == 10
    assert o["C"].non_functional and not o["D"].non_functional and not o["E"].non_functional  # 5% 는 기준 미만이 아님
    assert not o["B"].non_functional  # 정답 보기는 기능 부족 판정 대상이 아님


def test_inverted_distractor_when_upper_group_picks_it_more(world):
    db, _, bp, u, item = world
    # 점수가 높을수록 A(오답)를 고르고 낮을수록 B(정답)를 고른다
    add(db, u, bp, item, [(s, ["A"] if s > 50 else ["B"]) for s in range(1, 101)])
    o = {x.key: x for x in stat(db, item).options}
    assert o["A"].upper == 27 and o["A"].lower == 0 and o["A"].inverted
    assert o["B"].lower == 27 and not o["B"].inverted and not o["C"].inverted


def test_option_flags_need_enough_sample(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(s, ["A"] if s > 10 else ["B"]) for s in range(1, 21)])
    assert not any(o.non_functional or o.inverted for o in stat(db, item).options)


def test_multi_item_exact_match_and_percentages_can_exceed_100(db):
    d = domain(db, "ARCH")
    bp = make_blueprint(db, {("ARCH", Difficulty.MID): (1, 100.0)})
    u = make_user(db)
    item = make_item(db, d, item_type=ItemType.MULTI, correct=("A", "B"))
    add(db, u, bp, item, [(50, ["A", "B"]), (50, ["B", "A"]), (50, ["A"]), (50, ["A", "B", "C"])])
    s = stat(db, item)
    assert s.correct == 2 and s.p == 0.5  # 순서 무관 완전 일치만 정답
    pct = {o.key: o.pct for o in s.options}
    assert pct["A"] == 1.0 and pct["B"] == 0.75 and pct["C"] == 0.25 and sum(pct.values()) > 1


# ---------- 채점 보정과의 관계 ----------
def test_override_does_not_distort_raw_statistics(world):
    db, _, bp, u, item = world
    add(db, u, bp, item, [(50, ["A"])] * 5 + [(50, ["B"])] * 5)
    db.add(ItemOverride(item_id=item.id, mode=OverrideMode.CREDIT_ALL, reason="테스트"))
    db.flush()
    s = stat(db, item)
    assert s.p == 0.5 and s.override == OverrideMode.CREDIT_ALL  # 보정으로 p 가 1.0 이 되지 않는다


# ---------- 목록/정렬/필터/요약 ----------
@pytest.fixture()
def many(db):
    arch, ops = domain(db, "ARCH"), domain(db, "OPS")
    bp = make_blueprint(db, {("ARCH", Difficulty.MID): (1, 100.0)})
    u = make_user(db)
    good = make_item(db, arch, Difficulty.LOW, correct=("B",))
    bad = make_item(db, ops, Difficulty.HIGH, correct=("B",))
    tiny = make_item(db, arch, Difficulty.MID, correct=("B",))
    old = make_item(db, arch, Difficulty.LOW, status=ItemStatus.RETIRED, correct=("B",))
    add(db, u, bp, good, [(s, ["B"] if s > 20 else ["A"]) for s in range(1, 41)])         # D 높음, p=0.5
    add(db, u, bp, bad, [(s, ["B"] if s % 2 == 0 else ["A"]) for s in range(1, 41)])      # D 낮음 → 경고
    add(db, u, bp, tiny, [(50, ["B"])] * 5)                                              # 표본 부족
    add(db, u, bp, old, [(s, ["B"] if s > 30 else ["A"]) for s in range(1, 41)])
    return db, dict(good=good, bad=bad, tiny=tiny, old=old)


def ids(stats):
    return [s.item.id for s in stats]


def test_default_sort_puts_warnings_first_and_small_samples_last(many):
    db, it = many
    got = ids(svc.list_stats(db, svc.Filters()))
    assert got[0] == it["bad"].id and got[-1] == it["tiny"].id and set(got) == {i.id for i in it.values()}


def test_warn_only_and_domain_difficulty_status_filters(many):
    db, it = many
    assert ids(svc.list_stats(db, svc.Filters(warn_only=True))) == [it["bad"].id]
    assert set(ids(svc.list_stats(db, svc.Filters(domain="OPS")))) == {it["bad"].id}
    assert set(ids(svc.list_stats(db, svc.Filters(difficulty="LOW")))) == {it["good"].id, it["old"].id}
    assert ids(svc.list_stats(db, svc.Filters(status="RETIRED"))) == [it["old"].id]
    assert svc.list_stats(db, svc.Filters(domain="INSTALL")) == []


def test_other_sorts(many):
    db, it = many
    by_p = svc.list_stats(db, svc.Filters(sort="p"))
    assert [s.p for s in by_p] == sorted(s.p for s in by_p)
    by_n = svc.list_stats(db, svc.Filters(sort="n"))
    assert by_n[-1].item.id == it["tiny"].id and by_n[0].n == 40
    by_id = ids(svc.list_stats(db, svc.Filters(sort="id")))
    assert by_id == sorted(by_id)


def test_summary(many):
    db, _ = many
    sm = svc.summary(svc.list_stats(db, svc.Filters()))
    assert sm["count"] == 4 and sm["enough"] == 3 and sm["warn"] == 1 and sm["negative"] == 0
    assert 0 < sm["avg_p"] < 1 and sm["avg_d"] is not None
    assert svc.summary([]) == {"count": 0, "enough": 0, "warn": 0, "negative": 0, "avg_p": None, "avg_d": None}


def test_filters_query_string():
    assert svc.Filters().as_query() == ""
    q = svc.Filters(domain="ARCH", warn_only=True, sort="p").as_query()
    assert "domain=ARCH" in q and "warn=1" in q and "sort=p" in q


@pytest.mark.parametrize("n,enough", [(29, False), (30, True), (31, True)])
def test_sample_threshold_is_inclusive(world, n, enough):
    """응시 수가 기준(30)과 정확히 같을 때는 '표본 충분' 이다(경계값)."""
    db, _, bp, u, item = world
    add(db, u, bp, item, [(s, ["B"] if s > n / 2 else ["A"]) for s in range(1, n + 1)])
    assert stat(db, item).enough is enough
