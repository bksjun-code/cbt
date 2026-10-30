"""문항 분석: 난이도 지수(p), 변별도(D), 보기별 선택 분포.

  - 대상: 종료된(SUBMITTED/EXPIRED) 응시의 출제 문항. 진행 중 시험은 제외한다.
  - 정오는 응시자가 실제로 고른 보기와 정답 키를 직접 비교해 계산한다(완전 일치, 무응답은 오답).
    채점 보정(정답 인정/채점 제외)은 문항 오류의 신호이므로 분석 값을 왜곡하지 않도록 반영하지 않고 표시만 한다.
  - p = 정답자 수 / 응시 수
  - D = 상위 27% 정답률 − 하위 27% 정답률. 집단은 '그 문항이 출제된 응시'를 총점 내림차순
    (동점은 attempt id 순)으로 정렬해 구성하고, 각 집단 크기는 round(n × 0.27) (최소 1)이다.
  - 표본(n)이 min_sample 미만이면 D 는 참고값으로만 계산하고 경고(D < 0.2)는 내지 않는다.
  - 보기 분포: 전체/상위/하위 집단별 선택 수. MULTI 는 한 응시가 여러 보기를 고를 수 있어 합이 100% 를 넘을 수 있다.
"""
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AttemptItem, AttemptStatus, Domain, ExamAttempt, Item, ItemOverride, OverrideMode

MIN_SAMPLE = 30
GROUP_RATIO = 0.27
D_WARN = 0.2
LOW_PICK = 0.05  # 오답 보기가 이 비율 미만으로 선택되면 '기능 부족(non-functional)'


@dataclass
class OptionStat:
    key: str
    text: str
    is_correct: bool
    count: int = 0
    pct: float = 0.0
    upper: int = 0
    lower: int = 0
    non_functional: bool = False  # 오답인데 거의 선택되지 않음
    inverted: bool = False        # 오답인데 상위 집단이 하위 집단보다 더 많이 선택(정답 키/문항 모호성 의심)


@dataclass
class ItemStat:
    item: Item
    n: int = 0
    correct: int = 0
    blank: int = 0
    p: float | None = None
    group_n: int = 0
    p_upper: float | None = None
    p_lower: float | None = None
    d: float | None = None
    enough: bool = False
    override: OverrideMode | None = None
    options: list[OptionStat] = field(default_factory=list)

    @property
    def warn(self) -> bool:
        return self.enough and self.d is not None and self.d < D_WARN

    @property
    def negative(self) -> bool:
        return self.enough and self.d is not None and self.d < 0

    @property
    def p_label(self) -> str:
        if self.p is None:
            return "-"
        return "쉬움" if self.p >= 0.8 else ("어려움" if self.p < 0.3 else "보통")


def group_size(n: int) -> int:
    return max(1, round(n * GROUP_RATIO))


def _is_correct(selected: list | None, correct: list) -> bool:
    return bool(selected) and set(selected) == set(correct)


def analyze(db: Session, *, min_sample: int = MIN_SAMPLE, item_ids: list[int] | None = None) -> dict[int, ItemStat]:
    q = (
        select(AttemptItem.item_id, AttemptItem.attempt_id, AttemptItem.selected_keys, ExamAttempt.total_score)
        .join(ExamAttempt, ExamAttempt.id == AttemptItem.attempt_id)
        .where(ExamAttempt.status != AttemptStatus.IN_PROGRESS)
    )
    if item_ids is not None:
        q = q.where(AttemptItem.item_id.in_(item_ids))
    by_item: dict[int, list[tuple[float, int, list | None]]] = {}
    for item_id, attempt_id, selected, score in db.execute(q):
        by_item.setdefault(item_id, []).append((score or 0.0, attempt_id, selected))
    if not by_item:
        return {}

    items = {i.id: i for i in db.scalars(select(Item).where(Item.id.in_(list(by_item))))}
    overrides = {o.item_id: o.mode for o in db.scalars(select(ItemOverride).where(ItemOverride.item_id.in_(list(by_item))))}
    out: dict[int, ItemStat] = {}

    for item_id, rows in by_item.items():
        item = items[item_id]
        st = ItemStat(item=item, n=len(rows), override=overrides.get(item_id))
        flags = [_is_correct(sel, item.correct_keys) for _, _, sel in rows]
        st.correct = sum(flags)
        st.blank = sum(1 for _, _, sel in rows if not sel)
        st.p = st.correct / st.n

        order = sorted(range(st.n), key=lambda i: (-rows[i][0], rows[i][1]))  # 총점 내림차순, 동점은 id 순
        k = group_size(st.n)
        upper, lower = set(order[:k]), set(order[-k:])
        st.group_n = k
        st.p_upper = sum(flags[i] for i in upper) / k
        st.p_lower = sum(flags[i] for i in lower) / k
        st.d = round(st.p_upper - st.p_lower, 4)
        st.enough = st.n >= min_sample

        opts = {key: OptionStat(key, text, key in item.correct_keys) for key, text in sorted(item.options.items())}
        for i, (_, _, sel) in enumerate(rows):
            for key in set(sel or []):
                if key in opts:
                    opts[key].count += 1
                    opts[key].upper += i in upper
                    opts[key].lower += i in lower
        for o in opts.values():
            o.pct = o.count / st.n
            if not o.is_correct and st.enough:
                o.non_functional = o.pct < LOW_PICK
                o.inverted = o.upper > o.lower
        st.options = list(opts.values())
        out[item_id] = st
    return out


@dataclass
class Filters:
    domain: str = ""
    difficulty: str = ""
    status: str = ""
    warn_only: bool = False
    sort: str = "d"  # d(변별도 오름차순, 경고 우선) | p(정답률 오름차순) | n(응시 수 내림차순) | id

    def as_query(self) -> str:
        from urllib.parse import urlencode

        return urlencode({k: v for k, v in {"domain": self.domain, "difficulty": self.difficulty, "status": self.status,
                                            "warn": "1" if self.warn_only else "", "sort": self.sort if self.sort != "d" else ""}.items() if v})


def list_stats(db: Session, f: Filters, *, min_sample: int = MIN_SAMPLE) -> list[ItemStat]:
    stats = list(analyze(db, min_sample=min_sample).values())
    domains = {d.id: d.code for d in db.scalars(select(Domain))}
    if f.domain:
        stats = [s for s in stats if domains.get(s.item.domain_id) == f.domain]
    if f.difficulty:
        stats = [s for s in stats if s.item.difficulty.value == f.difficulty]
    if f.status:
        stats = [s for s in stats if s.item.status.value == f.status]
    if f.warn_only:
        stats = [s for s in stats if s.warn]
    if f.sort == "p":
        stats.sort(key=lambda s: (s.p, s.item.id))
    elif f.sort == "n":
        stats.sort(key=lambda s: (-s.n, s.item.id))
    elif f.sort == "id":
        stats.sort(key=lambda s: s.item.id)
    else:  # 경고(표본 충분 + D<0.2)를 먼저, 그 안에서는 D 오름차순. 표본 부족 항목은 뒤로
        stats.sort(key=lambda s: (not s.enough, s.d if s.d is not None else 9, s.item.id))
    return stats


def summary(stats: list[ItemStat]) -> dict:
    # 키 이름 주의: 템플릿에서 summary.items 처럼 읽으면 dict 메서드가 되므로 items/keys/values 는 쓰지 않는다
    enough = [s for s in stats if s.enough]
    return {
        "count": len(stats), "enough": len(enough), "warn": sum(1 for s in stats if s.warn),
        "negative": sum(1 for s in stats if s.negative),
        "avg_p": (sum(s.p for s in stats) / len(stats)) if stats else None,
        "avg_d": (sum(s.d for s in enough) / len(enough)) if enough else None,
    }
