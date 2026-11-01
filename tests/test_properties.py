"""속성 기반 테스트(hypothesis): 무작위로 만든 수많은 입력에서 '항상 성립해야 하는 규칙'을 검증한다.

출제 알고리즘은 브루트포스로 구한 정답(oracle)과 비교한다.
"""
import itertools
import random

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.db import Base, make_engine
from app.models import Blueprint, BlueprintRule, Difficulty, Item, ItemStatus, ItemType, OverrideMode
from app.seed import seed_domains
from app.services.grading import GradeInput, grade_items
from app.services.results_admin import FORMULA_PREFIXES, safe_cell
from app.services.selection import DIFF_RANK, InsufficientItemsError, build_plan

L, M, H = Difficulty.LOW, Difficulty.MID, Difficulty.HIGH
FAST = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])

# =====================================================================
# 출제 알고리즘
# =====================================================================
CELLS = [("ARCH", L), ("ARCH", M), ("OPS", L), ("OPS", H)]
GROUPS = ["g1", "g2", "g3", "g4", "g5"]  # 일부러 적게 → 셀 내부/셀 사이 variant_group 충돌이 자주 생긴다
STATUSES = [ItemStatus.APPROVED] * 5 + [ItemStatus.DRAFT, ItemStatus.RETIRED, ItemStatus.REVIEWED]


@st.composite
def exam_specs(draw):
    spec = []
    for cell in CELLS:
        items = [(draw(st.sampled_from(GROUPS)), draw(st.sampled_from(STATUSES))) for _ in range(draw(st.integers(0, 5)))]
        spec.append((cell, items, draw(st.integers(0, 3))))
    excluded_mask = draw(st.lists(st.booleans(), min_size=40, max_size=40))
    return spec, draw(st.text(min_size=1, max_size=6)), excluded_mask, draw(st.booleans())


def build_world(spec, sort_by_difficulty):
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    domains = seed_domains(db)
    bp = Blueprint(product_id=domains["ARCH"].product_id, name="p", total_items=sum(n for _, _, n in spec), time_limit_min=60, sort_by_difficulty=sort_by_difficulty)
    all_items = []
    for (code, diff), items, need in spec:
        if need:
            bp.rules.append(BlueprintRule(domain_id=domains[code].id, difficulty=diff, item_count=need, points_per_item=1.0))
        for group, status in items:
            it = Item(domain_id=domains[code].id, difficulty=diff, item_type=ItemType.SINGLE, stem=f"s{len(all_items)}",
                      options={k: k for k in "ABCDE"}, correct_keys=["A"], explanation="e", status=status, variant_group=group)
            db.add(it)
            all_items.append((it, code, diff, status, group))
    db.add(bp)
    db.flush()
    return engine, db, bp, all_items


def feasible(spec) -> bool:
    """브루트포스 oracle: 셀마다 서로 다른 variant_group 으로 need 개를 고르되, 시험 전체에서도 그룹이 겹치지 않는 조합이 있는가."""
    per_cell = []
    for cell, items, need in spec:
        if need == 0:
            continue
        groups = sorted({g for g, st_ in items if st_ == ItemStatus.APPROVED})
        per_cell.append(list(itertools.combinations(groups, need)))
    for combo in itertools.product(*per_cell):
        used = [g for c in combo for g in c]
        if len(used) == len(set(used)):
            return True
    return False


@given(exam_specs())
@FAST
def test_plan_satisfies_every_invariant_or_is_refused(case):
    spec, seed, mask, sort_flag = case
    engine, db, bp, all_items = build_world(spec, sort_flag)
    try:
        by_id = {it.id: (code, diff, status, group) for it, code, diff, status, group in all_items}
        excluded = {it.id for (it, *_), flag in zip(all_items, mask) if flag}
        try:
            plan = build_plan(db, bp, excluded, seed)
        except InsufficientItemsError:
            return  # 거부는 아래 test_refusal_is_correct 에서 정당성을 따로 검증
        # 1) 셀별 문항 수 정확
        need = {cell: n for cell, _, n in spec if n}
        got = {}
        for p in plan.items:
            code, diff, status, group = by_id[p.item_id]
            got[(code, diff)] = got.get((code, diff), 0) + 1
            assert status == ItemStatus.APPROVED          # 2) APPROVED 만
        assert got == need
        # 3) 시험 전체에서 variant_group 중복 없음
        groups = [by_id[p.item_id][3] for p in plan.items]
        assert len(groups) == len(set(groups))
        # 4) 같은 문항 중복 없음, 보기 순서는 순열
        assert len({p.item_id for p in plan.items}) == len(plan.items)
        assert all(sorted(p.option_order) == list("ABCDE") for p in plan.items)
        # 5) 난이도 정렬 옵션
        if sort_flag:
            ranks = [DIFF_RANK[p.difficulty] for p in plan.items]
            assert ranks == sorted(ranks)
        # 6) 같은 입력 → 같은 결과(재현성)
        again = build_plan(db, bp, excluded, seed)
        assert [(p.item_id, p.option_order) for p in again.items] == [(p.item_id, p.option_order) for p in plan.items]
        # 7) 최근 문항 제외: 완화 기록이 없다면 제외 문항은 한 개도 쓰이지 않았어야 한다
        if not plan.relaxations:
            assert not {p.item_id for p in plan.items} & excluded
        else:
            reused = sum(r.reused_picked for r in plan.relaxations)
            assert reused == len({p.item_id for p in plan.items} & excluded)
    finally:
        db.close()
        engine.dispose()


@given(exam_specs())
@FAST
def test_refusal_is_correct_exam_is_refused_only_when_no_valid_exam_exists(case):
    """완전성: 시험을 만들 수 있는 조합이 존재하면 반드시 만들어야 하고, 없으면 거부해야 한다."""
    spec, seed, _mask, sort_flag = case
    engine, db, bp, _ = build_world(spec, sort_flag)
    try:
        try:
            build_plan(db, bp, set(), seed)
            produced = True
        except InsufficientItemsError:
            produced = False
        assert produced == feasible(spec), spec
    finally:
        db.close()
        engine.dispose()


@given(st.lists(st.tuples(st.sampled_from(GROUPS), st.sampled_from(STATUSES)), min_size=0, max_size=8), st.integers(0, 4))
@FAST
def test_single_cell_always_succeeds_when_enough_distinct_approved_groups(items, need):
    """셀이 하나뿐이면 variant_group 이 셀 사이에서 충돌할 수 없으므로 '서로 다른 그룹 수 ≥ 필요 수' 일 때 항상 성공해야 한다."""
    spec = [(("ARCH", M), items, need)]
    engine, db, bp, _ = build_world(spec, True)
    try:
        distinct = len({g for g, s in items if s == ItemStatus.APPROVED})
        if need == 0:
            return
        if distinct >= need:
            assert len(build_plan(db, bp, set(), "x").items) == need
        else:
            with pytest.raises(InsufficientItemsError):
                build_plan(db, bp, set(), "x")
    finally:
        db.close()
        engine.dispose()


# =====================================================================
# 채점
# =====================================================================
KEYS = "ABCDE"


@st.composite
def graded_inputs(draw, min_size=1, max_size=12):
    rows = []
    for _ in range(draw(st.integers(min_size, max_size))):
        correct = frozenset(draw(st.lists(st.sampled_from(KEYS), min_size=1, max_size=2, unique=True)))
        selected = frozenset(draw(st.lists(st.sampled_from(KEYS), max_size=3, unique=True)))
        rows.append(GradeInput(draw(st.sampled_from(["A", "B", "C"])),
                               ItemType.MULTI if len(correct) > 1 else ItemType.SINGLE,
                               correct, selected, draw(st.sampled_from([1.0, 2.0, 2.5, 3.25]))))
    return rows


def with_selected(row: GradeInput, selected) -> GradeInput:
    return GradeInput(row.domain_code, row.item_type, row.correct, frozenset(selected), row.points, row.override)


@given(graded_inputs(), st.sampled_from([0.0, 0.4, 0.5]), st.sampled_from([50.0, 70.0]), st.booleans())
@FAST
def test_grading_basic_invariants(rows, min_ratio, pass_score, partial):
    r = grade_items(rows, pass_score, min_ratio, partial)
    assert 0.0 <= r.total_score <= 100.0
    for d in r.domain_scores.values():
        assert 0.0 <= d["ratio"] <= 1.0 and d["earned"] <= d["max"] + 1e-9
    assert (r.passed is True) <= (r.total_score + 1e-6 >= pass_score and not r.failed_domains)  # 합격이면 두 조건을 모두 만족
    if r.total_score + 1e-6 >= pass_score and not r.failed_domains:
        assert r.passed
    # 순서 무관
    shuffled = rows[:]
    random.Random(1).shuffle(shuffled)
    r2 = grade_items(shuffled, pass_score, min_ratio, partial)
    assert (r2.total_score, r2.passed, r2.failed_domains) == (r.total_score, r.passed, r.failed_domains)


@given(graded_inputs(), st.sampled_from([0.4]), st.sampled_from([70.0]))
@FAST
def test_grading_all_correct_is_100_and_all_wrong_is_0(rows, min_ratio, pass_score):
    perfect = [with_selected(r, r.correct) for r in rows]
    res = grade_items(perfect, pass_score, min_ratio)
    assert res.total_score == 100.0 and res.passed and not res.failed_domains
    blank = [with_selected(r, []) for r in rows]
    res0 = grade_items(blank, pass_score, min_ratio)
    assert res0.total_score == 0.0 and not res0.passed


@given(graded_inputs(), st.data())
@FAST
def test_grading_is_monotonic_fixing_an_answer_never_hurts(rows, data):
    """오답 하나를 정답으로 고치면 총점은 줄지 않고, 이미 합격이었다면 합격이 유지된다(과락도 새로 생기지 않는다)."""
    before = grade_items(rows, 70.0, 0.4)
    wrong = [i for i, r in enumerate(rows) if r.selected != r.correct]
    if not wrong:
        return
    i = data.draw(st.sampled_from(wrong))
    fixed = rows[:]
    fixed[i] = with_selected(rows[i], rows[i].correct)
    after = grade_items(fixed, 70.0, 0.4)
    assert after.total_score + 1e-6 >= before.total_score
    if before.passed:
        assert after.passed
    assert set(after.failed_domains) <= set(before.failed_domains)


@given(graded_inputs(), st.sampled_from([0.5, 2.0, 10.0]))
@FAST
def test_grading_is_invariant_to_point_scale(rows, k):
    scaled = [GradeInput(r.domain_code, r.item_type, r.correct, r.selected, r.points * k) for r in rows]
    a, b = grade_items(rows, 70.0, 0.4), grade_items(scaled, 70.0, 0.4)
    assert abs(a.total_score - b.total_score) <= 0.011 and a.failed_domains == b.failed_domains


@given(graded_inputs(), graded_inputs(min_size=1, max_size=4))
@FAST
def test_excluded_items_never_change_the_result(rows, extra):
    base = grade_items(rows, 70.0, 0.4)
    excluded = [GradeInput(e.domain_code, e.item_type, e.correct, e.selected, e.points, OverrideMode.EXCLUDE) for e in extra]
    r = grade_items(rows + excluded, 70.0, 0.4)
    assert r.total_score == base.total_score and r.failed_domains == base.failed_domains and r.passed == base.passed


@given(graded_inputs(), st.data())
@FAST
def test_credit_all_never_lowers_the_score(rows, data):
    i = data.draw(st.integers(0, len(rows) - 1))
    base = grade_items(rows, 70.0, 0.4)
    credited = rows[:]
    r = rows[i]
    credited[i] = GradeInput(r.domain_code, r.item_type, r.correct, r.selected, r.points, OverrideMode.CREDIT_ALL)
    assert grade_items(credited, 70.0, 0.4).total_score + 1e-6 >= base.total_score


# =====================================================================
# 내보내기 인젝션 방어
# =====================================================================
@given(st.text())
@settings(max_examples=500, deadline=None)
def test_safe_cell_never_leaves_a_leading_formula_character(s):
    out = safe_cell(s)
    assert not out.startswith(FORMULA_PREFIXES)
    if not s.startswith(FORMULA_PREFIXES):
        assert out == s
    else:
        assert out == "'" + s


@given(st.one_of(st.integers(), st.floats(allow_nan=False), st.none(), st.booleans()))
def test_safe_cell_passes_non_strings_through(v):
    assert safe_cell(v) is v


# =====================================================================
# 알려진 반례(회귀): 탐욕 선택만으로는 놓치던 경우
# =====================================================================
A, R, D = ItemStatus.APPROVED, ItemStatus.RETIRED, ItemStatus.DRAFT

KNOWN_COUNTEREXAMPLES = {
    # 앞 셀이 뒤 셀에 꼭 필요한 variant_group 을 가져가면 막히는 사례(2만 건 무작위 비교에서 발견된 것들)
    "ex1": [(("ARCH", L), [("g2", A), ("g3", A), ("g2", R)], 1),
            (("ARCH", M), [("g2", A), ("g4", A), ("g5", A), ("g3", R)], 1),
            (("OPS", L), [("g4", A), ("g4", R), ("g3", A), ("g1", A), ("g4", D)], 1),
            (("OPS", H), [("g4", A), ("g3", R), ("g5", A), ("g3", A)], 2)],
    "ex2": [(("ARCH", L), [("g3", A), ("g1", D), ("g5", A), ("g2", A), ("g2", A)], 2),
            (("ARCH", M), [("g2", R), ("g4", A), ("g3", A)], 1),
            (("OPS", L), [("g3", A)], 0),
            (("OPS", H), [("g3", D), ("g5", A), ("g2", A)], 1)],
    # HIGH 는 g1 뿐 → MID 는 g4 → LOW 는 g3 만 가능(유일해). LOW 가 g4 를 먼저 집으면 막힌다
    "ex3": [(("ARCH", L), [("g4", A), ("g4", A), ("g3", A)], 1),
            (("ARCH", M), [("g4", A), ("g1", A), ("g1", D), ("g4", R)], 1),
            (("OPS", L), [("g3", A)], 0),
            (("OPS", H), [("g5", R), ("g5", D), ("g1", A)], 1)],
}


def all_valid_item_sets(spec, all_items) -> set[frozenset[int]]:
    """브루트포스: 규칙을 만족하는(셀별 개수, APPROVED, 전체 variant_group 중복 없음) 모든 문항 id 집합."""
    approved: dict = {}
    for it, code, diff, status, group in all_items:
        if status == ItemStatus.APPROVED:
            approved.setdefault((code, diff), []).append((it.id, group))
    cells = [((code, diff), n) for (code, diff), _items, n in spec if n]
    out = set()
    for combo in itertools.product(*[list(itertools.combinations(approved.get(k, []), n)) for k, n in cells]):
        picks = [x for part in combo for x in part]
        if len({g for _, g in picks}) == len(picks):
            out.add(frozenset(i for i, _ in picks))
    return out


@pytest.mark.parametrize("name", sorted(KNOWN_COUNTEREXAMPLES))
def test_known_counterexamples_are_now_solved_for_every_seed(name):
    spec = KNOWN_COUNTEREXAMPLES[name]
    assert feasible(spec), "테스트 전제: 유효한 시험이 존재해야 한다"
    engine, db, bp, all_items = build_world(spec, True)
    try:
        by_id = {it.id: (code, diff, group) for it, code, diff, _s, group in all_items}
        outcomes = set()
        for s in range(80):  # 시드와 무관하게 항상 성공해야 한다(수정 전에는 시드에 따라 절반 가까이 실패)
            plan = build_plan(db, bp, set(), f"seed-{s}")
            groups = [by_id[p.item_id][2] for p in plan.items]
            assert len(groups) == len(set(groups))
            outcomes.add(tuple(sorted(groups)))
        assert outcomes  # 항상 계획이 만들어졌다
        if name == "ex3":
            assert outcomes == {("g1", "g3", "g4")}  # 유일한 해
    finally:
        db.close()
        engine.dispose()


def test_exact_fallback_is_reproducible_and_still_random_when_many_solutions_exist():
    spec = KNOWN_COUNTEREXAMPLES["ex1"]
    engine, db, bp, all_items = build_world(spec, True)
    try:
        sig = lambda s: [(p.item_id, tuple(p.option_order)) for p in build_plan(db, bp, set(), s).items]  # noqa: E731
        assert sig("same") == sig("same")
        # 보기 순서는 항상 무작위이므로 그것만으로는 검증이 안 된다 → '어떤 문항이 뽑혔는지'를 본다.
        # 브루트포스로 구한 '가능한 모든 문항 집합'을 시드만 바꿔서 빠짐없이 만들어 내야 한다(특정 해에 고착되지 않음)
        produced = {frozenset(p.item_id for p in build_plan(db, bp, set(), f"s{i}").items) for i in range(200)}
        assert produced == all_valid_item_sets(spec, all_items)
    finally:
        db.close()
        engine.dispose()


def test_exact_fallback_prefers_not_recently_used_items_and_reports_reuse():
    """대체 경로에서도 '최근 문항 제외 → 부족 시 완화 + 기록' 규칙이 지켜진다."""
    spec = KNOWN_COUNTEREXAMPLES["ex3"]
    engine, db, bp, all_items = build_world(spec, True)
    try:
        # 유일해에 쓰이는 문항 중 g3 문항 하나를 '최근 출제'로 표시 → 대체할 문항이 없으면 재사용으로 기록돼야 한다
        g3_ids = {it.id for it, _c, _d, st_, g in all_items if g == "g3" and st_ == ItemStatus.APPROVED}
        plan = build_plan(db, bp, g3_ids, "x")
        assert {p.item_id for p in plan.items} & g3_ids
        assert sum(r.reused_picked for r in plan.relaxations) == len({p.item_id for p in plan.items} & g3_ids)
        # 아무것도 제외하지 않으면 완화 기록이 없다
        assert build_plan(db, bp, set(), "x").relaxations == []
    finally:
        db.close()
        engine.dispose()


def test_truly_infeasible_case_is_still_refused_after_fallback():
    spec = [(("ARCH", L), [("g1", A), ("g2", A)], 2), (("ARCH", M), [("g1", A), ("g2", A)], 1)]  # 총 3개 필요, 그룹은 2개
    assert not feasible(spec)
    engine, db, bp, _ = build_world(spec, True)
    try:
        with pytest.raises(InsufficientItemsError):
            build_plan(db, bp, set(), "x")
    finally:
        db.close()
        engine.dispose()


def test_exact_fallback_picks_the_not_recently_used_item_within_a_group(monkeypatch):
    """대체 경로(매칭)에서도 같은 그룹 안에 신규 문항과 최근 출제 문항이 함께 있으면 신규 문항을 골라야 한다."""
    import app.services.selection as sel

    spec = [(("ARCH", L), [("g4", A), ("g4", A), ("g3", A), ("g3", A)], 1),   # g3 문항이 둘: 하나는 최근 출제
            (("ARCH", M), [("g4", A), ("g1", A)], 1),
            (("OPS", H), [("g1", A)], 1)]
    engine, db, bp, all_items = build_world(spec, True)
    calls = {"n": 0}
    real = sel._exact_assignment

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(sel, "_exact_assignment", spy)
    try:
        g3 = [it.id for it, _c, _d, _s, g in all_items if g == "g3"]
        recent, fresh = {g3[0]}, g3[1]
        for s in range(60):
            plan = build_plan(db, bp, recent, f"s{s}")
            ids = {p.item_id for p in plan.items}
            if any(by == "g3" for _it, _c, _d, _s, by in all_items if _it.id in ids):
                assert fresh in ids and not ids & recent, s
                assert plan.relaxations == []
        assert calls["n"] > 0, "대체 경로가 한 번도 실행되지 않아 이 테스트가 의미가 없음"
    finally:
        db.close()
        engine.dispose()


def test_exact_assignment_itself_explores_all_solutions_across_seeds():
    """매칭 함수만 직접 호출: 탐욕 단계가 성공하는 시드에 가려지지 않도록 대체 경로 자체의 무작위성을 검증한다.
    (그룹 순서를 섞지 않으면 항상 같은 해에 고착된다)"""
    import app.services.selection as sel

    spec = KNOWN_COUNTEREXAMPLES["ex1"]
    engine, db, bp, all_items = build_world(spec, True)
    try:
        rules = sel._active_rules(bp)
        cells = sel._load_candidates(db, rules)
        produced = set()
        for s in range(200):
            res = sel._exact_assignment(rules, cells, set(), random.Random(f"seed-{s}"))
            assert res is not None
            produced.add(frozenset(it.id for its in res.values() for it in its))
        assert produced == all_valid_item_sets(spec, all_items)
    finally:
        db.close()
        engine.dispose()
