from dataclasses import replace

import pytest
from sqlalchemy import select

from app.models import AuditLog, Blueprint, Difficulty, Domain, Item, ItemStatus, Role
from app.services import blueprint_admin as svc
from app.services.blueprint_validation import BlueprintError, validate_blueprint
from tests.helpers import make_user


@pytest.fixture()
def admin(db):
    return make_user(db, role=Role.ADMIN)


def domains(db):
    return list(db.scalars(select(Domain).order_by(Domain.id)))


def as_form(f: svc.BlueprintForm) -> dict:
    d = {"name": f.name, "time_limit_min": str(f.time_limit_min), "pass_score": str(f.pass_score),
         "domain_min_percent": str(f.domain_min_percent), "recent_exclude_n": str(f.recent_exclude_n)}
    if f.sort_by_difficulty:
        d["sort_by_difficulty"] = "on"
    if f.multi_partial_credit:
        d["multi_partial_credit"] = "on"
    for (code, diff), (count, pts) in f.rules.items():
        d[f"count_{code}_{diff}"] = str(count)
        d[f"points_{code}_{diff}"] = str(pts)
    return d


def valid_form(name="새 기준표") -> svc.BlueprintForm:
    """ARCH 에 LOW 10 × 2.0 + MID 16 × 2.5 + HIGH 12 × 3.3333... 대신 정수로 맞춘 100점."""
    f = svc.BlueprintForm(name=name)
    f.rules = {("ARCH", "LOW"): (10, 2.0), ("ARCH", "MID"): (16, 2.5), ("OPS", "HIGH"): (8, 5.0)}  # 20+40+40
    return f


# ---------- 폼 파싱/검증 ----------
def test_seeded_blueprint_round_trips_through_form(seeded):
    bp = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(bp, domains(seeded))
    assert f.total_items == 40 and f.total_points == 100 and f.domain_min_percent == 40
    parsed, errors = svc.parse_form(as_form(f), domains(seeded))
    assert errors == [] and parsed.rules == f.rules and parsed.sort_by_difficulty


def test_valid_form_has_no_errors(db):
    from app.seed import seed_domains

    seed_domains(db)
    _, errors = svc.parse_form(as_form(valid_form()), domains(db))
    assert errors == []


@pytest.mark.parametrize(
    "mutate,needle",
    [
        (lambda d: d.update({"points_ARCH_LOW": "3.0"}), "배점 합계가 "),
        (lambda d: d.update({"name": "  "}), "이름"),
        (lambda d: d.update({"time_limit_min": "0"}), "제한시간"),
        (lambda d: d.update({"time_limit_min": "abc"}), "제한시간"),
        (lambda d: d.update({"pass_score": "101"}), "합격 점수"),
        (lambda d: d.update({"domain_min_percent": "-1"}), "영역 최소"),
        (lambda d: d.update({"recent_exclude_n": "21"}), "최근 제외"),
        (lambda d: d.update({"count_ARCH_LOW": "-1"}), "문항 수"),
        (lambda d: d.update({"count_ARCH_LOW": "1.5"}), "문항 수"),
        (lambda d: d.update({"points_ARCH_LOW": "0"}), "배점은 0보다"),
        (lambda d: d.update({"points_ARCH_LOW": "x"}), "배점은"),
    ],
)
def test_form_validation_errors(db, mutate, needle):
    from app.seed import seed_domains

    seed_domains(db)
    d = as_form(valid_form())
    mutate(d)
    _, errors = svc.parse_form(d, domains(db))
    assert any(needle in e for e in errors), errors


def test_empty_rules_rejected(db):
    from app.seed import seed_domains

    seed_domains(db)
    f = svc.BlueprintForm(name="비어 있음")
    _, errors = svc.parse_form(as_form(f), domains(db))
    assert any("한 개 이상" in e for e in errors)


def test_points_sum_error_reports_actual_sum(db):
    from app.seed import seed_domains

    seed_domains(db)
    f = valid_form()
    f.rules[("OPS", "HIGH")] = (8, 5.25)  # 102
    _, errors = svc.parse_form(as_form(f), domains(db))
    assert "102점" in " ".join(errors)


def test_form_values_are_preserved_even_on_error(db):
    from app.seed import seed_domains

    seed_domains(db)
    d = as_form(valid_form("입력 유지"))
    d["points_ARCH_LOW"] = "9"
    f, errors = svc.parse_form(d, domains(db))
    assert errors and f.name == "입력 유지" and f.rules[("ARCH", "LOW")] == (10, 9.0)


# ---------- 생성/수정 ----------
def test_create_computes_total_items_inactive_and_audits(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    bp = svc.create_blueprint(db, valid_form(), admin, domains(db))
    assert bp.total_items == 34 and bp.is_active is False and bp.domain_min_ratio == 0.4
    assert {(r.domain.code, r.difficulty, r.item_count) for r in bp.rules} == {
        ("ARCH", Difficulty.LOW, 10), ("ARCH", Difficulty.MID, 16), ("OPS", Difficulty.HIGH, 8)}
    validate_blueprint(bp)
    log = db.scalar(select(AuditLog).where(AuditLog.action == "BLUEPRINT_CREATE"))
    assert log.user_id == admin.id and log.detail["after"]["total_items"] == 34


def test_create_duplicate_name_rejected(seeded, admin):
    f = valid_form(name="기본 40문항")
    with pytest.raises(BlueprintError, match="같은 이름"):
        svc.create_blueprint(seeded, f, admin, domains(seeded))


def test_update_replaces_rules_keeping_total_points_100(seeded, admin):
    bp = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(bp, domains(seeded))
    f.rules[("ARCH", "LOW")] = (1, 2.0)   # -2.0점
    f.rules[("OPS", "LOW")] = (3, 2.0)    # +2.0점 → 합계 100 유지, 문항 수 40 유지
    parsed, errors = svc.parse_form(as_form(f), domains(seeded))
    assert errors == []
    svc.update_blueprint(seeded, bp, parsed, admin, domains(seeded))
    seeded.commit()
    got = {(r.domain.code, r.difficulty.value): r.item_count for r in bp.rules}
    assert got[("ARCH", "LOW")] == 1 and got[("OPS", "LOW")] == 3 and bp.total_items == 40
    log = seeded.scalars(select(AuditLog).where(AuditLog.action == "BLUEPRINT_UPDATE")).one()
    assert log.detail["before"]["rules"]["ARCH/LOW"] == [2, 2.0] and log.detail["after"]["rules"]["ARCH/LOW"] == [1, 2.0]


def test_update_rejected_when_points_sum_breaks(seeded, admin):
    bp = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(bp, domains(seeded))
    f.rules[("ARCH", "LOW")] = (1, 2.0)  # 합계 98
    _, errors = svc.parse_form(as_form(f), domains(seeded))
    assert any("98점" in e for e in errors)
    with pytest.raises(BlueprintError, match="배점 합계"):  # 폼 검증을 우회해도 서비스가 거부
        svc.update_blueprint(seeded, bp, f, admin, domains(seeded))


def test_update_simple_change_and_audit(seeded, admin):
    bp = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(bp, domains(seeded))
    f.time_limit_min, f.pass_score, f.recent_exclude_n, f.sort_by_difficulty = 75, 80.0, 3, False
    svc.update_blueprint(seeded, bp, f, admin, domains(seeded))
    seeded.commit()
    assert (bp.time_limit_min, bp.pass_score, bp.recent_exclude_n, bp.sort_by_difficulty) == (75, 80.0, 3, False)
    assert bp.total_items == 40 and len(bp.rules) == 21  # 규칙 재생성 후에도 동일 구성(21개 셀)
    log = seeded.scalars(select(AuditLog).where(AuditLog.action == "BLUEPRINT_UPDATE")).one()
    assert log.detail["before"]["time_limit_min"] == 60 and log.detail["after"]["time_limit_min"] == 75
    assert log.detail["active"] is True


def test_update_drops_cells_set_to_zero(seeded, admin):
    bp = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(bp, domains(seeded))
    f.rules[("INSTALL", "HIGH")] = (0, 0.0)           # 1문항 제거 (3.25점)
    f.rules[("INSTALL", "LOW")] = (1, 2.0 + 3.25)     # 같은 칸 배점 보정으로 합계 100 유지
    svc.update_blueprint(seeded, bp, f, admin, domains(seeded))
    seeded.commit()
    assert bp.total_items == 39 and len(bp.rules) == 20
    validate_blueprint(bp)


def test_update_name_collision(seeded, admin):
    other = svc.create_blueprint(seeded, valid_form("다른 기준표"), admin, domains(seeded))
    f = svc.form_from_blueprint(other, domains(seeded))
    f.name = "기본 40문항"
    with pytest.raises(BlueprintError, match="같은 이름"):
        svc.update_blueprint(seeded, other, f, admin, domains(seeded))


# ---------- 가용 문항 ----------
def test_availability_counts_distinct_variant_groups_and_only_approved(seeded):
    av = svc.availability(seeded)
    assert av[("REPLICAT", "MID")] == (4, 4) and av[("ARCH", "LOW")] == (3, 3)
    arch = seeded.scalar(select(Domain).where(Domain.code == "ARCH"))
    it = seeded.scalars(select(Item).where(Item.domain_id == arch.id, Item.difficulty == Difficulty.LOW)).first()
    it.status = ItemStatus.DRAFT
    seeded.flush()
    assert svc.availability(seeded)[("ARCH", "LOW")] == (2, 2)


def test_matrix_marks_shortage_in_red_cells(seeded):
    bp = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(bp, domains(seeded))
    assert svc.shortages(seeded, f, domains(seeded)) == []
    f.rules[("ARCH", "HIGH")] = (5, 1.0)  # 가용 3 < 필요 5
    rows = {r["domain"].code: r["cells"] for r in svc.matrix(seeded, f, domains(seeded))}
    assert rows["ARCH"][2].shortage and not rows["ARCH"][0].shortage
    assert svc.shortages(seeded, f, domains(seeded)) == ["ARCH/HIGH: 필요 5, 가용 3"]


def test_cells_not_in_rules_never_count_as_shortage(db):
    from app.seed import seed_domains

    seed_domains(db)
    f = valid_form()
    assert svc.shortages(db, f, domains(db)) != []  # 문항이 하나도 없으니 필요 칸은 부족
    assert all(not c.shortage for r in svc.matrix(db, svc.BlueprintForm(name="x"), domains(db)) for c in r["cells"])


# ---------- 활성화 ----------
def test_activate_switches_single_active_blueprint(seeded, admin):
    first = seeded.scalar(select(Blueprint))
    bp2 = svc.create_blueprint(seeded, replace(svc.form_from_blueprint(first, domains(seeded)), name="복제본"),
                               admin, domains(seeded))
    seeded.commit()
    svc.activate(seeded, bp2, admin, domains(seeded))
    seeded.commit()
    seeded.refresh(first)
    assert bp2.is_active and not first.is_active
    assert seeded.query(Blueprint).filter(Blueprint.is_active.is_(True)).count() == 1
    log = seeded.scalar(select(AuditLog).where(AuditLog.action == "BLUEPRINT_ACTIVATE"))
    assert log.detail == {"deactivated": [first.id]}


def test_activate_blocked_when_cell_short(seeded, admin):
    first = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(first, domains(seeded))
    f.name = "문항 부족"
    bp2 = svc.create_blueprint(seeded, f, admin, domains(seeded))
    arch = seeded.scalar(select(Domain).where(Domain.code == "ARCH"))
    for it in seeded.scalars(select(Item).where(Item.domain_id == arch.id, Item.difficulty == Difficulty.HIGH)):
        it.status = ItemStatus.RETIRED
    seeded.flush()
    with pytest.raises(BlueprintError, match="ARCH/HIGH"):
        svc.activate(seeded, bp2, admin, domains(seeded))
    assert not bp2.is_active and first.is_active  # 기존 활성 유지


def test_activate_blocked_when_invalid(seeded, admin):
    first = seeded.scalar(select(Blueprint))
    f = svc.form_from_blueprint(first, domains(seeded))
    f.name = "깨진 기준표"
    bp2 = svc.create_blueprint(seeded, f, admin, domains(seeded))
    bp2.rules[0].points_per_item += 1  # 정합성 훼손(예: 수동 DB 수정)
    with pytest.raises(BlueprintError, match="배점 합계"):
        svc.activate(seeded, bp2, admin, domains(seeded))
    assert not bp2.is_active


def test_list_blueprints_active_first(seeded, admin):
    svc.create_blueprint(seeded, valid_form("뒤에 만든 것"), admin, domains(seeded))
    names = [b.name for b in svc.list_blueprints(seeded)]
    assert names[0] == "기본 40문항" and names[1] == "뒤에 만든 것"
