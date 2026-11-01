"""Blueprint 관리: 폼 검증, 저장(규칙 교체), 활성화, 셀별 가용 문항 현황.

  - total_items 는 규칙의 문항 수 합계로 자동 계산한다(불일치 원천 차단)
  - 배점 합계는 정확히 100점이어야 저장된다
  - 활성화는 (1) 검증 통과 (2) 부족 셀 없음 일 때만 가능하다 → 응시자가 시험 시작 시 거부당하는 일을 막는다
  - 활성 Blueprint 는 제품마다 최대 1개(영역 목록 domains 는 그 Blueprint 제품의 영역)
"""
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import (
    Blueprint, BlueprintRule, Difficulty, Domain, Item, ItemStatus, Product, User,
)
from app.services.audit import log_audit
from app.services.blueprint_validation import TOTAL_POINTS, BlueprintError, validate_blueprint

DIFFS = (Difficulty.LOW, Difficulty.MID, Difficulty.HIGH)
EPS = 1e-6


@dataclass
class BlueprintForm:
    name: str = ""
    time_limit_min: int = 60
    pass_score: float = 70.0
    domain_min_percent: float = 40.0  # 화면 입력은 %, 저장은 0~1
    recent_exclude_n: int = 2
    sort_by_difficulty: bool = True
    multi_partial_credit: bool = False
    rules: dict[tuple[str, str], tuple[int, float]] = field(default_factory=dict)  # (domain_code, diff) -> (count, points)

    @property
    def total_items(self) -> int:
        return sum(c for c, _ in self.rules.values())

    @property
    def total_points(self) -> float:
        return round(sum(c * p for c, p in self.rules.values()), 6)


def form_from_blueprint(bp: Blueprint, domains: list[Domain]) -> BlueprintForm:
    code = {d.id: d.code for d in domains}
    return BlueprintForm(
        name=bp.name, time_limit_min=bp.time_limit_min, pass_score=bp.pass_score,
        domain_min_percent=round(bp.domain_min_ratio * 100, 4), recent_exclude_n=bp.recent_exclude_n,
        sort_by_difficulty=bp.sort_by_difficulty, multi_partial_credit=bp.multi_partial_credit,
        rules={(code[r.domain_id], r.difficulty.value): (r.item_count, r.points_per_item) for r in bp.rules},
    )


def _num(raw, cast, default=None):
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        return default


def parse_form(form, domains: list[Domain]) -> tuple[BlueprintForm, list[str]]:
    """request.form() → (BlueprintForm, 오류 목록). 오류가 있어도 입력값은 그대로 되돌려 보여 주기 위해 반환한다."""
    errors: list[str] = []
    f = BlueprintForm(name=(form.get("name") or "").strip(), sort_by_difficulty=form.get("sort_by_difficulty") == "on",
                      multi_partial_credit=form.get("multi_partial_credit") == "on")

    def field_num(key, cast, label, lo, hi, default):
        v = _num(form.get(key), cast)
        if v is None or not lo <= v <= hi:
            errors.append(f"{label}은(는) {lo}~{hi} 범위의 숫자여야 합니다")
            return default
        return v

    f.time_limit_min = field_num("time_limit_min", int, "제한시간(분)", 1, 600, 60)
    f.pass_score = field_num("pass_score", float, "합격 점수", 1, 100, 70.0)
    f.domain_min_percent = field_num("domain_min_percent", float, "영역 최소 득점률(%)", 0, 100, 40.0)
    f.recent_exclude_n = field_num("recent_exclude_n", int, "최근 제외 횟수", 0, 20, 2)
    if not f.name or len(f.name) > 100:
        errors.append("이름은 1~100자여야 합니다")

    for d in domains:
        for diff in DIFFS:
            key = (d.code, diff.value)
            count = _num(form.get(f"count_{d.code}_{diff.value}") or "0", int)
            pts = _num(form.get(f"points_{d.code}_{diff.value}") or "0", float)
            if count is None or not 0 <= count <= 100:
                errors.append(f"{d.code}/{diff.value} 문항 수는 0~100 정수여야 합니다")
                count = 0
            if pts is None or not 0 <= pts <= 100:
                errors.append(f"{d.code}/{diff.value} 배점은 0~100 숫자여야 합니다")
                pts = 0.0
            if count > 0 and pts <= 0:
                errors.append(f"{d.code}/{diff.value}: 문항이 있으면 배점은 0보다 커야 합니다")
            f.rules[key] = (count, pts)

    if f.total_items == 0:
        errors.append("문항이 한 개 이상 배정되어야 합니다")
    elif abs(f.total_points - TOTAL_POINTS) > EPS:
        errors.append(f"배점 합계가 {f.total_points:g}점입니다. 정확히 {TOTAL_POINTS:g}점이어야 합니다")
    return f, errors


def _name_taken(db: Session, name: str, exclude_id: int | None) -> bool:
    q = select(Blueprint.id).where(Blueprint.name == name)
    if exclude_id is not None:
        q = q.where(Blueprint.id != exclude_id)
    return db.scalar(q) is not None


def _apply(db: Session, bp: Blueprint, f: BlueprintForm, domains: list[Domain]) -> None:
    ids = {d.code: d.id for d in domains}
    bp.name, bp.time_limit_min, bp.pass_score = f.name, f.time_limit_min, f.pass_score
    bp.domain_min_ratio = round(f.domain_min_percent / 100, 6)
    bp.recent_exclude_n, bp.sort_by_difficulty, bp.multi_partial_credit = f.recent_exclude_n, f.sort_by_difficulty, f.multi_partial_credit
    bp.total_items = f.total_items
    bp.rules.clear()
    db.flush()  # 복합 PK 재삽입 충돌 방지
    for (code, diff), (count, pts) in sorted(f.rules.items()):
        if count > 0:
            bp.rules.append(BlueprintRule(domain_id=ids[code], difficulty=Difficulty(diff), item_count=count, points_per_item=pts))
    validate_blueprint(bp)


def _summary(f: BlueprintForm) -> dict:
    return {
        "name": f.name, "time_limit_min": f.time_limit_min, "pass_score": f.pass_score,
        "domain_min_percent": f.domain_min_percent, "recent_exclude_n": f.recent_exclude_n,
        "sort_by_difficulty": f.sort_by_difficulty, "multi_partial_credit": f.multi_partial_credit,
        "total_items": f.total_items, "rules": {f"{c}/{d}": list(v) for (c, d), v in sorted(f.rules.items()) if v[0] > 0},
    }


def create_blueprint(db: Session, f: BlueprintForm, actor: User, domains: list[Domain], product: Product) -> Blueprint:
    if _name_taken(db, f.name, None):
        raise BlueprintError("같은 이름의 Blueprint 가 이미 있습니다")
    bp = Blueprint(product_id=product.id, name=f.name, total_items=0, time_limit_min=f.time_limit_min, is_active=False)
    db.add(bp)
    db.flush()
    _apply(db, bp, f, domains)
    log_audit(db, actor.id, "BLUEPRINT_CREATE", f"blueprint:{bp.id}", {"product": product.code, "after": _summary(f)})
    db.flush()
    return bp


def update_blueprint(db: Session, bp: Blueprint, f: BlueprintForm, actor: User, domains: list[Domain]) -> Blueprint:
    if _name_taken(db, f.name, bp.id):
        raise BlueprintError("같은 이름의 Blueprint 가 이미 있습니다")
    before = _summary(form_from_blueprint(bp, domains))
    _apply(db, bp, f, domains)
    after = _summary(f)
    log_audit(db, actor.id, "BLUEPRINT_UPDATE", f"blueprint:{bp.id}",
              {"active": bp.is_active, "before": before, "after": after})
    db.flush()
    return bp


# ---------- 가용 문항 현황 ----------
@dataclass(frozen=True)
class Cell:
    needed: int
    approved: int
    groups: int  # 서로 다른 variant_group 수(실제로 출제 가능한 최대 문항 수)

    @property
    def shortage(self) -> bool:
        return self.groups < self.needed


def availability(db: Session) -> dict[tuple[str, str], tuple[int, int]]:
    """(domain_code, difficulty) → (APPROVED 문항 수, 서로 다른 variant_group 수)"""
    rows = db.execute(
        select(Domain.code, Item.difficulty, func.count(Item.id), func.count(func.distinct(Item.variant_group)))
        .join(Domain, Domain.id == Item.domain_id).where(Item.status == ItemStatus.APPROVED)
        .group_by(Domain.code, Item.difficulty)
    )
    return {(c, d.value): (n, g) for c, d, n, g in rows}


def matrix(db: Session, f: BlueprintForm, domains: list[Domain]) -> list[dict]:
    avail = availability(db)
    out = []
    for d in domains:
        cells = []
        for diff in DIFFS:
            needed = f.rules.get((d.code, diff.value), (0, 0.0))[0]
            n, g = avail.get((d.code, diff.value), (0, 0))
            cells.append(Cell(needed, n, g))
        out.append({"domain": d, "cells": cells})
    return out


def shortages(db: Session, f: BlueprintForm, domains: list[Domain]) -> list[str]:
    msgs = []
    for row in matrix(db, f, domains):
        for diff, c in zip(DIFFS, row["cells"]):
            if c.shortage:
                msgs.append(f"{row['domain'].code}/{diff.value}: 필요 {c.needed}, 가용 {c.groups}")
    return msgs


# ---------- 활성화 ----------
def activate(db: Session, bp: Blueprint, actor: User, domains: list[Domain]) -> None:
    validate_blueprint(bp)
    short = shortages(db, form_from_blueprint(bp, domains), domains)
    if short:
        raise BlueprintError("문항이 부족한 셀이 있어 활성화할 수 없습니다 — " + "; ".join(short))
    same_product = (Blueprint.product_id == bp.product_id, Blueprint.id != bp.id)
    previous = [b.id for b in db.scalars(select(Blueprint).where(Blueprint.is_active.is_(True), *same_product))]
    db.execute(update(Blueprint).where(*same_product).values(is_active=False))
    bp.is_active = True
    log_audit(db, actor.id, "BLUEPRINT_ACTIVATE", f"blueprint:{bp.id}", {"deactivated": previous})
    db.flush()


def list_blueprints(db: Session, product_id: int | None = None) -> list[Blueprint]:
    q = (select(Blueprint).join(Product, Product.id == Blueprint.product_id)
         .order_by(Product.sort_order, Blueprint.is_active.desc(), Blueprint.id.desc()))
    if product_id is not None:
        q = q.where(Blueprint.product_id == product_id)
    return list(db.scalars(q))
