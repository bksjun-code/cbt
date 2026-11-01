"""문항 관리: CRUD, 검수 워크플로, 출제 이력 기반 수정 잠금, 신규 버전 생성.

상태 전이
  DRAFT --submit_review--> REVIEWED --approve--> APPROVED
  REVIEWED --reject(사유 필수)--> DRAFT
  DRAFT/REVIEWED/APPROVED --retire--> RETIRED   (RETIRED 는 종단 상태)

수정 정책
  - 출제 이력(attempt_items 존재)이 있거나 RETIRED 인 문항은 수정 불가 → new_version() 사용
  - REVIEWED/APPROVED 문항을 수정하면 DRAFT 로 되돌려 재검수를 거치게 한다(승인 정보 초기화)
"""
from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import (
    AttemptItem, Difficulty, Domain, Item, ItemReview, ItemStatus, ItemType, Product, ReviewAction, User, utcnow,
)
from app.schemas.item import ItemIn
from app.services.app_settings import get_setting
from app.services.audit import log_audit


class WorkflowError(Exception):
    """허용되지 않는 상태 전이/수정 시도."""


class DuplicateItem(ValueError):
    def __init__(self, existing: Item):
        self.existing = existing
        super().__init__(f"동일한 문항이 이미 존재합니다 (#{existing.id})")


S = ItemStatus
TRANSITIONS: dict[str, tuple[set[ItemStatus], ItemStatus, ReviewAction]] = {
    "submit_review": ({S.DRAFT}, S.REVIEWED, ReviewAction.SUBMIT_REVIEW),
    "approve": ({S.REVIEWED}, S.APPROVED, ReviewAction.APPROVE),
    "reject": ({S.REVIEWED}, S.DRAFT, ReviewAction.REJECT),
    "retire": ({S.DRAFT, S.REVIEWED, S.APPROVED}, S.RETIRED, ReviewAction.RETIRE),
}


def norm_stem(s: str) -> str:
    return " ".join(s.split()).casefold()


def domain_by_code(db: Session, code: str) -> Domain:
    d = db.scalar(select(Domain).where(Domain.code == code))
    if d is None:
        raise ValueError(f"알 수 없는 영역: {code}")
    return d


def find_duplicate(db: Session, domain_id: int, stem: str, exclude_id: int | None = None) -> Item | None:
    """같은 영역에서 (공백/대소문자 무시) 동일한 지문의 비폐기 문항."""
    q = select(Item).where(Item.domain_id == domain_id, Item.status != S.RETIRED)
    if exclude_id is not None:
        q = q.where(Item.id != exclude_id)
    target = norm_stem(stem)
    return next((it for it in db.scalars(q) if norm_stem(it.stem) == target), None)


def has_history(db: Session, item_id: int) -> bool:
    return bool(db.scalar(select(exists().where(AttemptItem.item_id == item_id))))


def is_editable(db: Session, item: Item) -> bool:
    return item.status != S.RETIRED and not has_history(db, item.id)


def _version(data: ItemIn, domain: Domain) -> str:
    return data.product_version or domain.product.default_version


def _fill(item: Item, data: ItemIn, domain: Domain) -> None:
    item.domain_id = domain.id
    item.difficulty = data.difficulty
    item.item_type = data.item_type
    item.stem = data.stem
    item.options = data.options
    item.correct_keys = sorted(data.correct_keys)
    item.explanation = data.explanation
    item.reference = data.reference
    item.product_version = _version(data, domain)
    item.architecture = data.architecture
    item.variant_group = data.variant_group.strip()


def create_item(db: Session, data: ItemIn, actor: User | None, *, check_duplicate: bool = True) -> Item:
    """항상 DRAFT 로 생성(자동 승인 금지)."""
    domain = domain_by_code(db, data.domain)
    if check_duplicate and (dup := find_duplicate(db, domain.id, data.stem)):
        raise DuplicateItem(dup)
    item = Item(status=S.DRAFT, created_by=actor.id if actor else None)
    _fill(item, data, domain)
    db.add(item)
    db.flush()
    log_audit(db, actor.id if actor else None, "ITEM_CREATE", f"item:{item.id}",
              {"domain": data.domain, "difficulty": data.difficulty.value})
    return item


def _changed_fields(item: Item, data: ItemIn, domain: Domain) -> list[str]:
    cmp = {
        "domain": (item.domain_id, domain.id), "difficulty": (item.difficulty, data.difficulty),
        "item_type": (item.item_type, data.item_type), "stem": (item.stem, data.stem),
        "options": (item.options, data.options), "correct_keys": (sorted(item.correct_keys), sorted(data.correct_keys)),
        "explanation": (item.explanation, data.explanation), "reference": (item.reference, data.reference),
        "product_version": (item.product_version, _version(data, domain)), "architecture": (item.architecture, data.architecture),
        "variant_group": (item.variant_group, data.variant_group.strip()),
    }
    return [k for k, (a, b) in cmp.items() if a != b]


def update_item(db: Session, item: Item, data: ItemIn, actor: User) -> Item:
    if item.status == S.RETIRED:
        raise WorkflowError("폐기된 문항은 수정할 수 없습니다. 신규 버전을 만드세요.")
    if has_history(db, item.id):
        raise WorkflowError("출제 이력이 있는 문항은 수정할 수 없습니다. 폐기 후 신규 버전을 만드세요.")
    domain = domain_by_code(db, data.domain)
    if dup := find_duplicate(db, domain.id, data.stem, exclude_id=item.id):
        raise DuplicateItem(dup)
    changed = _changed_fields(item, data, domain)
    if not changed:
        return item
    prev_status = item.status
    _fill(item, data, domain)
    if prev_status in (S.REVIEWED, S.APPROVED):  # 재검수 필요
        item.status = S.DRAFT
        item.approved_by = None
        item.approved_at = None
    log_audit(db, actor.id, "ITEM_UPDATE", f"item:{item.id}",
              {"changed": changed, "status_before": prev_status.value, "status_after": item.status.value})
    db.flush()
    return item


def transition(db: Session, item: Item, action: str, actor: User, reason: str | None = None) -> Item:
    if action not in TRANSITIONS:
        raise WorkflowError(f"알 수 없는 작업: {action}")
    allowed_from, to, review_action = TRANSITIONS[action]
    if item.status not in allowed_from:
        raise WorkflowError(f"{item.status.value} 상태에서는 '{action}' 할 수 없습니다")
    reason = (reason or "").strip() or None
    if action == "reject" and not reason:
        raise WorkflowError("반려 사유를 입력해야 합니다")
    if action == "approve":
        if item.created_by is not None and item.created_by == actor.id and get_setting(db, "require_separate_approver", False):
            raise WorkflowError("작성자 본인은 승인할 수 없습니다(작성자와 승인자 분리 설정)")
        item.approved_by = actor.id
        item.approved_at = utcnow()
    before = item.status
    item.status = to
    db.add(ItemReview(item_id=item.id, action=review_action, reason=reason, actor_id=actor.id))
    log_audit(db, actor.id, f"ITEM_{review_action.value}", f"item:{item.id}",
              {"from": before.value, "to": to.value, "reason": reason})
    db.flush()
    return item


def new_version(db: Session, item: Item, actor: User) -> Item:
    """이력 있는 문항 등을 고치기 위한 경로: 원본을 RETIRED 처리하고 내용을 복사한 새 DRAFT 를 만든다."""
    successor = db.scalar(select(Item).where(Item.previous_version_id == item.id, Item.status != S.RETIRED))
    if successor is not None:
        raise WorkflowError(f"이미 신규 버전(#{successor.id})이 있습니다")
    if item.status != S.RETIRED:
        transition(db, item, "retire", actor, reason=f"신규 버전 생성(v{item.version + 1})")
    new = Item(
        domain_id=item.domain_id, difficulty=item.difficulty, item_type=item.item_type, stem=item.stem,
        options=dict(item.options), correct_keys=list(item.correct_keys), explanation=item.explanation,
        reference=item.reference, product_version=item.product_version, architecture=item.architecture,
        variant_group=item.variant_group, status=S.DRAFT, created_by=actor.id,
        version=item.version + 1, previous_version_id=item.id,
    )
    db.add(new)
    db.flush()
    log_audit(db, actor.id, "ITEM_NEW_VERSION", f"item:{new.id}", {"from_item": item.id, "version": new.version})
    return new


# ---------- 조회 ----------
PER_PAGE = 20


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_items(db: Session, *, product_code: str | None = None, domain_code: str | None = None, difficulty: str | None = None,
               status: str | None = None, item_type: str | None = None, q: str | None = None,
               page: int = 1, per_page: int = PER_PAGE) -> tuple[list[Item], int]:
    stmt = select(Item).join(Domain, Domain.id == Item.domain_id)
    if product_code:
        stmt = stmt.join(Product, Product.id == Domain.product_id).where(Product.code == product_code)
    if domain_code:
        stmt = stmt.where(Domain.code == domain_code)
    # enum 에 없는 값(주소창 직접 입력 등)은 무시한다. 그대로 비교하면 SQLAlchemy 가 LookupError 를 내 500 이 된다
    if difficulty in Difficulty.__members__:
        stmt = stmt.where(Item.difficulty == difficulty)
    if status in ItemStatus.__members__:
        stmt = stmt.where(Item.status == status)
    if item_type in ItemType.__members__:
        stmt = stmt.where(Item.item_type == item_type)
    if q and q.strip():
        like = _like(q.strip())
        stmt = stmt.where(Item.stem.ilike(like, escape="\\") | Item.variant_group.ilike(like, escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Item.id.desc()).limit(per_page).offset(max(page - 1, 0) * per_page))
    return list(rows), total


def status_counts(db: Session) -> dict[str, int]:
    counts = {s.value: 0 for s in ItemStatus}
    for st, n in db.execute(select(Item.status, func.count()).group_by(Item.status)):
        counts[st.value] = n
    return counts


def review_history(db: Session, item_id: int) -> list[ItemReview]:
    return list(db.scalars(select(ItemReview).where(ItemReview.item_id == item_id).order_by(ItemReview.id.desc())))


def variant_groups(db: Session) -> list[str]:
    return list(db.scalars(select(Item.variant_group).distinct().order_by(Item.variant_group)))


@dataclass(frozen=True)
class Lineage:
    previous: Item | None
    newer: list[Item]


def lineage(db: Session, item: Item) -> Lineage:
    prev = db.get(Item, item.previous_version_id) if item.previous_version_id else None
    newer = list(db.scalars(select(Item).where(Item.previous_version_id == item.id)))
    return Lineage(prev, newer)
