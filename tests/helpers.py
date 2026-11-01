"""테스트 공용 팩토리."""
from sqlalchemy import select

from app.models import (
    Blueprint, BlueprintRule, Difficulty, Domain, Item, ItemStatus, ItemType, Role, User,
)
from app.seed import seed_domains
from app.services.security import hash_password

_counter = {"n": 0}


def make_user(db, login=None, role=Role.CANDIDATE) -> User:
    _counter["n"] += 1
    u = User(login_id=login or f"user{_counter['n']}", name="테스트", password_hash=hash_password("pw-long-enough"), role=role)
    db.add(u)
    db.flush()
    return u


def make_item(db, domain: Domain, diff=Difficulty.MID, group=None, status=ItemStatus.APPROVED,
              item_type=ItemType.SINGLE, correct=("A",), n_options=5) -> Item:
    _counter["n"] += 1
    keys = "ABCDE"[:n_options]
    it = Item(
        domain_id=domain.id, difficulty=diff, item_type=item_type, stem=f"질문 {_counter['n']}",
        options={k: f"보기 {k} {_counter['n']}" for k in keys}, correct_keys=list(correct),
        explanation=f"해설-{_counter['n']}-비밀", reference="ref", status=status,
        variant_group=group or f"G{_counter['n']}",
    )
    db.add(it)
    db.flush()
    return it


def make_blueprint(db, cells: dict, name="BP", **kw) -> Blueprint:
    """cells: {(domain_code, Difficulty): (count, points)}"""
    domains = seed_domains(db)
    first = next(iter(cells), ("ARCH", None))[0]  # 규칙 영역의 제품(규칙이 없으면 OGG)
    kw.setdefault("product_id", domains[first].product_id)
    bp = Blueprint(name=name, total_items=sum(c for c, _ in cells.values()), time_limit_min=kw.pop("time_limit_min", 60),
                   is_active=kw.pop("is_active", True), **kw)
    for (code, diff), (count, pts) in cells.items():
        bp.rules.append(BlueprintRule(domain_id=domains[code].id, difficulty=diff, item_count=count, points_per_item=pts))
    db.add(bp)
    db.flush()
    return bp


def domain(db, code) -> Domain:
    seed_domains(db)
    return db.scalar(select(Domain).where(Domain.code == code))
