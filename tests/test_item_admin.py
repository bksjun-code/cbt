import pytest
from sqlalchemy import select

from app.models import AuditLog, Item, ItemReview, ItemStatus, ReviewAction, Role
from app.schemas.item import ItemIn
from app.services import exam
from app.services import item_admin as svc
from app.services.app_settings import set_setting
from app.services.selection import build_plan
from app.models import Blueprint
from tests.helpers import make_user

S = ItemStatus


def item_in(**over) -> ItemIn:
    base = {
        "domain": "EXTRACT", "difficulty": "MID", "item_type": "SINGLE", "stem": "새 문항 지문",
        "options": {"A": "a", "B": "b", "C": "c", "D": "d"}, "correct_keys": ["B"],
        "explanation": "해설", "variant_group": "NEW-GROUP",
    }
    base.update(over)
    return ItemIn.model_validate(base)


@pytest.fixture()
def admin(db):
    return make_user(db, role=Role.ADMIN)


@pytest.fixture()
def admin2(db):
    return make_user(db, role=Role.ADMIN)


def audits(db, action):
    return list(db.scalars(select(AuditLog).where(AuditLog.action == action)))


# ---------- 생성 ----------
def test_create_is_always_draft_and_audited(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    assert it.status == S.DRAFT and it.created_by == admin.id and it.version == 1
    assert it.approved_by is None and it.is_sample is False
    assert audits(seeded, "ITEM_CREATE")[0].target == f"item:{it.id}"


def test_correct_keys_are_stored_sorted(seeded, admin):
    it = svc.create_item(seeded, item_in(item_type="MULTI", correct_keys=["C", "A"]), admin)
    assert it.correct_keys == ["A", "C"]


def test_duplicate_stem_rejected_ignoring_case_and_whitespace(seeded, admin):
    svc.create_item(seeded, item_in(stem="Extract   파라미터는?"), admin)
    with pytest.raises(svc.DuplicateItem):
        svc.create_item(seeded, item_in(stem="  extract 파라미터는? ", variant_group="OTHER"), admin)


def test_duplicate_allowed_across_domains_and_against_retired(seeded, admin):
    first = svc.create_item(seeded, item_in(stem="같은 지문"), admin)
    svc.create_item(seeded, item_in(stem="같은 지문", domain="OPS"), admin)  # 다른 영역
    svc.transition(seeded, first, "retire", admin)
    svc.create_item(seeded, item_in(stem="같은 지문"), admin)  # 폐기본과는 중복 아님


# ---------- 워크플로 ----------
def test_full_review_workflow_records_history_and_audit(seeded, admin, admin2):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "submit_review", admin)
    assert it.status == S.REVIEWED
    svc.transition(seeded, it, "approve", admin2)
    assert it.status == S.APPROVED and it.approved_by == admin2.id and it.approved_at is not None
    acts = [h.action for h in svc.review_history(seeded, it.id)]
    assert acts == [ReviewAction.APPROVE, ReviewAction.SUBMIT_REVIEW]
    log = audits(seeded, "ITEM_APPROVE")[0]
    assert log.user_id == admin2.id and log.detail == {"from": "REVIEWED", "to": "APPROVED", "reason": None}


@pytest.mark.parametrize(
    "start,action",
    [(S.DRAFT, "approve"), (S.DRAFT, "reject"), (S.REVIEWED, "submit_review"), (S.APPROVED, "submit_review"),
     (S.APPROVED, "approve"), (S.APPROVED, "reject"), (S.RETIRED, "retire"), (S.RETIRED, "approve"),
     (S.RETIRED, "submit_review")],
)
def test_invalid_transitions_rejected(seeded, admin, start, action):
    it = svc.create_item(seeded, item_in(), admin)
    it.status = start
    with pytest.raises(svc.WorkflowError):
        svc.transition(seeded, it, action, admin, reason="x")
    assert it.status == start


def test_unknown_action_rejected(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    with pytest.raises(svc.WorkflowError):
        svc.transition(seeded, it, "publish", admin)


def test_reject_requires_reason_and_records_it(seeded, admin, admin2):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "submit_review", admin)
    for bad in (None, "", "   "):
        with pytest.raises(svc.WorkflowError):
            svc.transition(seeded, it, "reject", admin2, reason=bad)
    assert it.status == S.REVIEWED
    svc.transition(seeded, it, "reject", admin2, reason="  오답 보기 C 가 모호함 ")
    assert it.status == S.DRAFT
    h = svc.review_history(seeded, it.id)[0]
    assert h.action == ReviewAction.REJECT and h.reason == "오답 보기 C 가 모호함" and h.actor_id == admin2.id


def test_four_eyes_setting_blocks_self_approval(seeded, admin, admin2):
    set_setting(seeded, "require_separate_approver", True)
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "submit_review", admin)
    with pytest.raises(svc.WorkflowError, match="작성자"):
        svc.transition(seeded, it, "approve", admin)
    assert it.status == S.REVIEWED
    svc.transition(seeded, it, "approve", admin2)
    assert it.status == S.APPROVED


def test_self_approval_allowed_by_default(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "submit_review", admin)
    svc.transition(seeded, it, "approve", admin)
    assert it.status == S.APPROVED


def test_only_approved_items_enter_exam_pool(seeded, admin):
    bp = seeded.scalar(select(Blueprint))
    it = svc.create_item(seeded, item_in(domain="ARCH", difficulty="LOW", variant_group="POOL-TEST"), admin)
    ids = {p.item_id for s in range(30) for p in build_plan(seeded, bp, set(), f"s{s}").items}
    assert it.id not in ids  # DRAFT 는 제외
    svc.transition(seeded, it, "submit_review", admin)
    ids = {p.item_id for s in range(30) for p in build_plan(seeded, bp, set(), f"s{s}").items}
    assert it.id not in ids  # REVIEWED 도 제외
    svc.transition(seeded, it, "approve", admin)
    ids = {p.item_id for s in range(60) for p in build_plan(seeded, bp, set(), f"s{s}").items}
    assert it.id in ids


# ---------- 수정 정책 ----------
def test_edit_draft_in_place(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.update_item(seeded, it, item_in(stem="수정된 지문", explanation="새 해설"), admin)
    assert it.stem == "수정된 지문" and it.status == S.DRAFT
    assert audits(seeded, "ITEM_UPDATE")[0].detail["changed"] == ["stem", "explanation"]


@pytest.mark.parametrize("state", [S.REVIEWED, S.APPROVED])
def test_edit_after_review_resets_to_draft(seeded, admin, state):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "submit_review", admin)
    if state == S.APPROVED:
        svc.transition(seeded, it, "approve", admin)
    svc.update_item(seeded, it, item_in(correct_keys=["C"]), admin)
    assert it.status == S.DRAFT and it.approved_by is None and it.approved_at is None
    d = audits(seeded, "ITEM_UPDATE")[0].detail
    assert d["status_before"] == state.value and d["status_after"] == "DRAFT"


def test_noop_edit_keeps_status_and_writes_no_audit(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "submit_review", admin)
    svc.update_item(seeded, it, item_in(), admin)
    assert it.status == S.REVIEWED and not audits(seeded, "ITEM_UPDATE")


def test_edit_can_change_domain(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.update_item(seeded, it, item_in(domain="OPS"), admin)
    assert it.domain.code == "OPS"


def test_edit_duplicate_rejected_but_self_is_not_duplicate(seeded, admin):
    a = svc.create_item(seeded, item_in(stem="지문 A"), admin)
    b = svc.create_item(seeded, item_in(stem="지문 B", variant_group="B"), admin)
    svc.update_item(seeded, a, item_in(stem="지문 A", explanation="자기 자신과는 중복 아님"), admin)
    with pytest.raises(svc.DuplicateItem):
        svc.update_item(seeded, b, item_in(stem="지문 A", variant_group="B"), admin)


def test_retired_item_cannot_be_edited(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "retire", admin)
    with pytest.raises(svc.WorkflowError, match="폐기"):
        svc.update_item(seeded, it, item_in(stem="변경"), admin)


def _used_item(db):
    u = make_user(db)
    a, _ = exam.start_attempt(db, u)
    return db.get(Item, a.items[0].item_id)


def test_item_with_exam_history_is_locked(seeded, admin):
    it = _used_item(seeded)
    assert svc.has_history(seeded, it.id) and not svc.is_editable(seeded, it)
    with pytest.raises(svc.WorkflowError, match="출제 이력"):
        svc.update_item(seeded, it, item_in(domain=it.domain.code, stem="변경 시도"), admin)
    assert it.stem != "변경 시도"


def test_unused_item_is_editable(seeded):
    unused = seeded.scalars(select(Item).order_by(Item.id)).first()
    assert not svc.has_history(seeded, unused.id) and svc.is_editable(seeded, unused)


# ---------- 신규 버전 ----------
def test_new_version_retires_original_and_creates_draft_copy(seeded, admin):
    it = _used_item(seeded)
    new = svc.new_version(seeded, it, admin)
    assert it.status == S.RETIRED
    assert new.id != it.id and new.status == S.DRAFT and new.version == it.version + 1
    assert new.previous_version_id == it.id and new.created_by == admin.id
    assert (new.stem, new.options, new.correct_keys, new.variant_group) == (it.stem, it.options, it.correct_keys, it.variant_group)
    assert new.options is not it.options
    assert new.approved_by is None and new.is_sample is False
    assert audits(seeded, "ITEM_NEW_VERSION")[0].detail == {"from_item": it.id, "version": new.version}
    assert svc.is_editable(seeded, new)  # 새 문항은 이력이 없으므로 수정 가능
    lin = svc.lineage(seeded, new)
    assert lin.previous.id == it.id and svc.lineage(seeded, it).newer[0].id == new.id


def test_new_version_from_already_retired_item(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.transition(seeded, it, "retire", admin)
    new = svc.new_version(seeded, it, admin)
    assert new.version == 2 and len([h for h in svc.review_history(seeded, it.id) if h.action == ReviewAction.RETIRE]) == 1


def test_new_version_twice_is_rejected(seeded, admin):
    it = svc.create_item(seeded, item_in(), admin)
    svc.new_version(seeded, it, admin)
    with pytest.raises(svc.WorkflowError, match="이미 신규 버전"):
        svc.new_version(seeded, it, admin)


def test_new_version_can_be_replaced_after_its_retirement(seeded, admin):
    v1 = svc.create_item(seeded, item_in(), admin)
    v2 = svc.new_version(seeded, v1, admin)
    svc.transition(seeded, v2, "retire", admin)
    v2b = svc.new_version(seeded, v1, admin)  # v2 가 폐기되었으므로 다시 가능
    assert v2b.version == 2 and v2b.previous_version_id == v1.id


def test_review_rows_are_per_item(seeded, admin):
    a = svc.create_item(seeded, item_in(stem="A"), admin)
    b = svc.create_item(seeded, item_in(stem="B", variant_group="B"), admin)
    svc.transition(seeded, a, "submit_review", admin)
    assert seeded.query(ItemReview).filter_by(item_id=b.id).count() == 0


# ---------- 목록/검색 ----------
def test_list_filters_and_pagination(seeded):
    items, total = svc.list_items(seeded)
    assert total == 64 and len(items) == svc.PER_PAGE
    p4, _ = svc.list_items(seeded, page=4)
    assert len(p4) == 4
    arch, n = svc.list_items(seeded, domain_code="ARCH")
    assert n == 9 and all(i.domain.code == "ARCH" for i in arch)
    _, n = svc.list_items(seeded, domain_code="ARCH", difficulty="HIGH")
    assert n == 3
    _, n = svc.list_items(seeded, item_type="MULTI")
    assert n == 4
    _, n = svc.list_items(seeded, status="DRAFT")
    assert n == 0


def test_list_search_matches_stem_and_variant_group_case_insensitively(seeded):
    _, n = svc.list_items(seeded, q="manager")
    assert n >= 1
    items, _ = svc.list_items(seeded, q="arch-trail")
    assert [i.variant_group for i in items] == ["ARCH-TRAIL"]


def test_list_search_escapes_like_wildcards(seeded):
    assert svc.list_items(seeded, q="%")[1] == 0
    assert svc.list_items(seeded, q="_")[1] == 0


def test_status_counts(seeded, admin):
    svc.create_item(seeded, item_in(), admin)
    c = svc.status_counts(seeded)
    assert c == {"DRAFT": 1, "REVIEWED": 0, "APPROVED": 64, "RETIRED": 0}


def test_variant_groups_distinct_sorted(seeded):
    g = svc.variant_groups(seeded)
    assert g == sorted(set(g)) and len(g) == 64
