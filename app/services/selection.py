"""출제 알고리즘.

  1. Blueprint 규칙 → (영역 × 난이도) 셀별 필요 문항 수
  2. 셀 후보 = APPROVED 문항만
  3. 응시자의 최근 N회 출제 문항은 우선 제외(부족하면 완화하고 relaxations 로 보고)
  4. 같은 variant_group 은 시험 전체에서 1문항만
     먼저 무작위 탐욕 선택(여유가 적은 셀부터)을 하고, 셀 사이의 그룹 충돌로 막히면 이분 매칭으로 정확한 배정을 찾는다.
     → 유효한 시험이 하나라도 존재하면 반드시 만든다(브루트포스와 무작위 2만 건 비교로 검증, tests/test_properties.py).
  5. 모든 무작위는 attempt seed 로 만든 random.Random 한 개로만 수행 → 재현 가능
  6. 문항 순서 셔플 (옵션: 난이도 LOW→MID→HIGH 티어 정렬, 티어 내부는 무작위)
  7. 보기 순서 셔플 → option_order (원본 key 순서 목록)
  8. 유효한 배정이 존재하지 않을 때만 InsufficientItemsError (부족 셀 목록 포함)

재현성 전제: 같은 seed + 같은 문항풀(APPROVED 집합) + 같은 excluded_item_ids.
무작위 추출 전 후보를 id 로 정렬하고 셀을 고정 순서로 처리하므로 DB 반환 순서에 의존하지 않는다.
"""
import random
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AttemptItem, Blueprint, BlueprintRule, Difficulty, Domain, ExamAttempt, Item,
    ItemStatus, AttemptStatus,
)

DIFF_RANK = {Difficulty.LOW: 0, Difficulty.MID: 1, Difficulty.HIGH: 2}


@dataclass(frozen=True)
class PlannedItem:
    item_id: int
    domain_id: int
    difficulty: Difficulty
    variant_group: str
    points: float
    option_order: list[str]


@dataclass(frozen=True)
class CellStatus:
    """셀별 가용 문항 현황 (관리자 대시보드/사전 검사 공용)."""

    domain_id: int
    domain_code: str
    difficulty: Difficulty
    needed: int
    approved_items: int
    distinct_groups: int  # 같은 variant_group 은 1문항만 쓸 수 있으므로 실제 가용 수

    @property
    def shortage(self) -> bool:
        return self.distinct_groups < self.needed


@dataclass(frozen=True)
class Relaxation:
    domain_id: int
    difficulty: Difficulty
    needed: int
    fresh_picked: int
    reused_picked: int  # 최근 출제 문항을 다시 쓴 수


@dataclass
class ExamPlan:
    seed: str
    items: list[PlannedItem] = field(default_factory=list)
    relaxations: list[Relaxation] = field(default_factory=list)


class InsufficientItemsError(Exception):
    def __init__(self, shortages: list[CellStatus]):
        self.shortages = shortages
        desc = ", ".join(
            f"{s.domain_code}/{s.difficulty.value} 필요 {s.needed} 가용 {s.distinct_groups}" for s in shortages
        )
        super().__init__(f"출제 가능 문항 부족: {desc}")


def _active_rules(bp: Blueprint) -> list[BlueprintRule]:
    rules = [r for r in bp.rules if r.item_count > 0]
    return sorted(rules, key=lambda r: (r.domain_id, DIFF_RANK[r.difficulty]))


def _load_candidates(db: Session, rules: list[BlueprintRule]) -> dict[tuple[int, Difficulty], list[Item]]:
    if not rules:
        return {}
    items = db.scalars(
        select(Item)
        .where(Item.status == ItemStatus.APPROVED, Item.domain_id.in_({r.domain_id for r in rules}))
        .order_by(Item.id)
    )
    cells: dict[tuple[int, Difficulty], list[Item]] = {(r.domain_id, r.difficulty): [] for r in rules}
    for it in items:
        key = (it.domain_id, it.difficulty)
        if key in cells:
            cells[key].append(it)
    return cells


def cell_statuses(db: Session, bp: Blueprint) -> list[CellStatus]:
    rules = _active_rules(bp)
    cells = _load_candidates(db, rules)
    codes = {d.id: d.code for d in db.scalars(select(Domain))}
    out = []
    for r in rules:
        cands = cells[(r.domain_id, r.difficulty)]
        out.append(
            CellStatus(
                domain_id=r.domain_id, domain_code=codes[r.domain_id], difficulty=r.difficulty,
                needed=r.item_count, approved_items=len(cands),
                distinct_groups=len({c.variant_group for c in cands}),
            )
        )
    return out


def recent_item_ids(db: Session, user_id: int, n: int, before_attempt_id: int | None = None,
                    product_id: int | None = None) -> set[int]:
    """사용자의 최근 n회(종료된) 시험에서 출제된 문항 id. product_id 를 주면 그 제품 시험만 센다."""
    if n <= 0:
        return set()
    q = select(ExamAttempt.id).where(
        ExamAttempt.user_id == user_id, ExamAttempt.status != AttemptStatus.IN_PROGRESS
    )
    if product_id is not None:
        q = q.join(Blueprint, Blueprint.id == ExamAttempt.blueprint_id).where(Blueprint.product_id == product_id)
    if before_attempt_id is not None:
        q = q.where(ExamAttempt.id < before_attempt_id)
    attempt_ids = list(db.scalars(q.order_by(ExamAttempt.id.desc()).limit(n)))
    if not attempt_ids:
        return set()
    return set(db.scalars(select(AttemptItem.item_id).where(AttemptItem.attempt_id.in_(attempt_ids))))


def _exact_assignment(ordered: list[BlueprintRule], cells: dict[tuple[int, Difficulty], list[Item]],
                      excluded: set[int], rng: random.Random) -> dict[tuple[int, Difficulty], list[Item]] | None:
    """셀별 필요 문항 수와 '시험 전체에서 variant_group 은 1문항만' 조건을 동시에 만족하는 배정을 이분 매칭으로 찾는다.

    슬롯(셀의 필요 문항 하나) ↔ variant_group 을 짝짓는다. 각 그룹은 한 슬롯에만 배정된다.
    인접 순서는 '최근에 출제되지 않은 문항이 있는 그룹'을 먼저 두어 가능한 한 신규 문항을 쓰고,
    무작위는 호출자의 rng 만 사용하므로 같은 seed 에서는 같은 결과가 나온다. 배정이 없으면 None.
    """
    by_group: dict[tuple[int, Difficulty], dict[str, list[Item]]] = {}
    slots: list[tuple[tuple[int, Difficulty], list[str]]] = []
    for r in ordered:
        key = (r.domain_id, r.difficulty)
        groups: dict[str, list[Item]] = {}
        for it in sorted(cells[key], key=lambda c: c.id):
            groups.setdefault(it.variant_group, []).append(it)
        by_group[key] = groups
        fresh = [g for g, its in groups.items() if any(i.id not in excluded for i in its)]
        stale = [g for g in groups if g not in fresh]
        rng.shuffle(fresh)
        rng.shuffle(stale)
        slots.extend((key, fresh + stale) for _ in range(r.item_count))

    owner: dict[str, int] = {}  # variant_group -> 그 그룹을 쓰는 슬롯 번호

    def augment(slot: int, seen: set[str]) -> bool:
        for g in slots[slot][1]:
            if g in seen:
                continue
            seen.add(g)
            if g not in owner or augment(owner[g], seen):
                owner[g] = slot
                return True
        return False

    for s in range(len(slots)):
        if not augment(s, set()):
            return None

    slot_group = {s: g for g, s in owner.items()}
    result: dict[tuple[int, Difficulty], list[Item]] = {(r.domain_id, r.difficulty): [] for r in ordered}
    for s, (key, _) in enumerate(slots):
        items = by_group[key][slot_group[s]]
        pool = [i for i in items if i.id not in excluded] or items  # 신규 문항 우선
        result[key].append(rng.choice(pool))
    return result


def build_plan(db: Session, bp: Blueprint, excluded_item_ids: set[int], seed: str) -> ExamPlan:
    rules = _active_rules(bp)
    cells = _load_candidates(db, rules)

    # 사전 검사: 셀 단독으로도 불가능하면 즉시 거부
    statuses = {(s.domain_id, s.difficulty): s for s in cell_statuses(db, bp)}
    short = [s for s in statuses.values() if s.shortage]
    if short:
        raise InsufficientItemsError(short)

    rng = random.Random(seed)
    # 여유가 적은 셀부터 처리해 전역 variant_group 충돌로 인한 실패를 줄인다(결정적 순서)
    ordered = sorted(
        rules,
        key=lambda r: (
            statuses[(r.domain_id, r.difficulty)].distinct_groups - r.item_count,
            r.domain_id, DIFF_RANK[r.difficulty],
        ),
    )

    used_groups: set[str] = set()
    picked: list[tuple[BlueprintRule, Item]] = []
    relaxations: list[Relaxation] = []
    failed: list[CellStatus] = []

    for r in ordered:
        cands = cells[(r.domain_id, r.difficulty)]
        fresh = [c for c in cands if c.id not in excluded_item_ids]
        stale = [c for c in cands if c.id in excluded_item_ids]
        rng.shuffle(fresh)
        rng.shuffle(stale)
        chosen: list[Item] = []
        reused = 0
        for pool, is_stale in ((fresh, False), (stale, True)):
            for it in pool:
                if len(chosen) == r.item_count:
                    break
                if it.variant_group in used_groups:
                    continue
                chosen.append(it)
                used_groups.add(it.variant_group)
                reused += is_stale
        if len(chosen) < r.item_count:
            st = statuses[(r.domain_id, r.difficulty)]
            failed.append(CellStatus(st.domain_id, st.domain_code, st.difficulty, st.needed,
                                     st.approved_items, len(chosen)))
            continue
        if reused:
            relaxations.append(Relaxation(r.domain_id, r.difficulty, r.item_count,
                                          r.item_count - reused, reused))
        picked.extend((r, it) for it in chosen)

    if failed:
        # 탐욕 선택이 막혔다고 해서 시험을 만들 수 없는 것은 아니다(앞 셀이 뒤 셀에 필요한 그룹을 가져간 경우).
        # 정확한 해법(이분 매칭)으로 한 번 더 찾고, 그래도 없을 때만 거부한다.
        exact = _exact_assignment(ordered, cells, excluded_item_ids, rng)
        if exact is None:
            raise InsufficientItemsError(failed)
        picked, relaxations = [], []
        for r in ordered:
            chosen = exact[(r.domain_id, r.difficulty)]
            reused = sum(1 for it in chosen if it.id in excluded_item_ids)
            if reused:
                relaxations.append(Relaxation(r.domain_id, r.difficulty, r.item_count, r.item_count - reused, reused))
            picked.extend((r, it) for it in chosen)

    # 문항 순서
    rng.shuffle(picked)
    if bp.sort_by_difficulty:
        picked.sort(key=lambda p: DIFF_RANK[p[1].difficulty])  # 안정 정렬: 티어 내부는 무작위 유지

    planned = []
    for r, it in picked:
        keys = sorted(it.options)
        rng.shuffle(keys)
        planned.append(
            PlannedItem(it.id, it.domain_id, it.difficulty, it.variant_group, r.points_per_item, keys)
        )
    return ExamPlan(seed=seed, items=planned, relaxations=relaxations)
