"""여러 시험(제품): 제품별 활성 Blueprint, 1인 1건 응시, 제품별 최근 출제 제외, 합격증 제품명, 화면 필터."""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Blueprint, Difficulty, Domain, ExamAttempt, Product, Role, User
from app.schemas.item import ItemIn
from app.services import blueprint_admin, exam
from app.services.item_admin import create_item
from app.services.selection import recent_item_ids
from tests.helpers import domain, make_blueprint, make_item, make_user
from tests.test_certificates import pdf_text
from tests.test_web import env, login, token  # noqa: F401


def product(db, code: str) -> Product:
    return db.scalar(select(Product).where(Product.code == code))


def open_db_exam(db, n_items=4, name="DB 기준표") -> Blueprint:
    """Oracle Database 시험을 연다: DB_ARCH/MID 승인 문항 n개 + 2문항×50점 활성 Blueprint."""
    d = domain(db, "DB_ARCH")
    for _ in range(n_items):
        make_item(db, d)
    return make_blueprint(db, {("DB_ARCH", Difficulty.MID): (2, 50.0)}, name=name)


# ---------- 서비스 ----------
def test_activating_db_blueprint_keeps_ogg_active(seeded):
    admin = make_user(seeded, role=Role.ADMIN)
    ogg_bp = seeded.scalar(select(Blueprint).where(Blueprint.is_active.is_(True)))
    first = open_db_exam(seeded)
    second = make_blueprint(seeded, {("DB_ARCH", Difficulty.MID): (2, 50.0)}, name="DB 두 번째", is_active=False)
    db_domains = list(seeded.scalars(select(Domain).where(Domain.product_id == first.product_id)))
    blueprint_admin.activate(seeded, second, admin, db_domains)
    seeded.commit()
    for bp in (first, second, ogg_bp):
        seeded.refresh(bp)
    assert second.is_active and not first.is_active  # 같은 제품 안에서만 교체
    assert ogg_bp.is_active                          # 다른 제품의 활성 Blueprint 는 그대로
    assert exam.get_active_blueprint(seeded, first.product_id).id == second.id
    assert exam.get_active_blueprint(seeded, ogg_bp.product_id).id == ogg_bp.id


def test_one_attempt_at_a_time_across_products(seeded):
    bp = open_db_exam(seeded)
    u = make_user(seeded)
    a, created = exam.start_attempt(seeded, u, product_id=bp.product_id)
    assert created and a.blueprint_id == bp.id
    b, created = exam.start_attempt(seeded, u, product_id=product(seeded, "OGG").id)
    assert not created and b.id == a.id  # 진행 중인 DB 시험으로 돌아간다


def test_exam_without_active_blueprint_cannot_start(seeded):
    with pytest.raises(exam.NoActiveBlueprint):
        exam.start_attempt(seeded, make_user(seeded), product_id=product(seeded, "WLS").id)


def test_default_start_picks_first_open_exam(seeded):
    open_db_exam(seeded)
    a, _ = exam.start_attempt(seeded, make_user(seeded))
    assert a.blueprint.product.code == "OGG"  # 정렬 순서 첫 제품


def test_recent_exclusion_counts_only_same_product(seeded):
    bp = open_db_exam(seeded)
    u = make_user(seeded)
    a, _ = exam.start_attempt(seeded, u, product_id=bp.product_id)
    exam.submit_attempt(seeded, a)
    o, _ = exam.start_attempt(seeded, u)  # 그 뒤에 본 OGG 시험
    exam.submit_attempt(seeded, o)
    assert recent_item_ids(seeded, u.id, 1, product_id=bp.product_id) == {ai.item_id for ai in a.items}
    assert recent_item_ids(seeded, u.id, 1) == {ai.item_id for ai in o.items}


def test_item_version_alias_and_product_default(db):
    from app.seed import seed_domains

    seed_domains(db)
    base = {"difficulty": "MID", "item_type": "SINGLE", "stem": "stem", "options": {"A": "a", "B": "b"},
            "correct_keys": ["A"], "explanation": "e", "variant_group": "G-1"}
    assert ItemIn.model_validate({**base, "domain": "EXTRACT", "ogg_version": "21c"}).product_version == "21c"
    wls = create_item(db, ItemIn.model_validate({**base, "domain": "WLS_DEPLOY", "product_version": "  "}), None)
    assert wls.product_version == "14c"  # 비우면 제품 기본 버전
    ogg = create_item(db, ItemIn.model_validate({**base, "domain": "OPS", "stem": "other"}), None)
    assert ogg.product_version == "19c"


# ---------- 화면 ----------
def test_dashboard_lists_exams_and_starts_the_chosen_one(env):  # noqa: F811
    build, Session = env
    with TestClient(build()) as c:
        login(c, "cand")
        home = c.get("/").text
        assert home.count('name="product"') == 1 and 'value="OGG"' in home
        assert home.count("준비 중입니다") == 2  # DB·WebLogic 은 활성 Blueprint 가 없다
        with Session() as db:
            open_db_exam(db)
            db.commit()
        home = c.get("/").text
        assert 'value="ORADB"' in home and home.count("준비 중입니다") == 1
        r = c.post("/exam/start", data={"csrf_token": token(home), "product": "NOPE"}, follow_redirects=False)
        assert r.status_code == 503
        r = c.post("/exam/start", data={"csrf_token": token(home), "product": "ORADB"}, follow_redirects=False)
        assert r.status_code == 303
        aid = int(r.headers["location"].rsplit("/", 1)[1])
        with Session() as db:
            assert db.get(ExamAttempt, aid).blueprint.product.code == "ORADB"
        home = c.get("/").text
        assert "진행 중인 <b>Oracle Database</b>" in home and 'name="product"' not in home


def test_db_exam_certificate_uses_product_name_and_code(env):  # noqa: F811
    build, Session = env
    with Session() as db:
        bp = open_db_exam(db)
        u = db.scalar(select(User).where(User.login_id == "cand"))
        a, _ = exam.start_attempt(db, u, product_id=bp.product_id)
        for ai in a.items:
            exam.save_answer(db, a, ai.seq, list(ai.item.correct_keys))
        exam.submit_attempt(db, a)
        aid = a.id
    with TestClient(build(public_base_url="https://cbt.example.com")) as c:
        login(c, "cand")
        assert "Oracle Database 시험 결과" in c.get(f"/exam/{aid}/result").text
        r = c.get(f"/exam/{aid}/certificate.pdf")
        number = re.search(r'certificate_(RP-ORADB-\d{8}-[0-9A-F]{6})\.pdf', r.headers["content-disposition"]).group(1)
        text = pdf_text(r.content)
        assert "Oracle Database 사내 자격시험" in text and "GoldenGate" not in text
        v = c.get(f"/verify/{number}").text
        assert "유효한 합격증입니다" in v and "Oracle Database 사내 자격시험" in v


def test_admin_screens_filter_by_exam(env):  # noqa: F811
    build, Session = env
    with Session() as db:
        open_db_exam(db)
        db.commit()
    with TestClient(build()) as c:
        login(c, "admin")
        assert "(4건)" in c.get("/admin/items?product=ORADB").text
        listing = c.get("/admin/blueprints").text
        assert "Oracle Database" in listing and "/admin/blueprints/new?product=WLS" in listing
        new = c.get("/admin/blueprints/new?product=ORADB").text
        assert 'name="count_DB_ARCH_MID" value="2"' in new and "count_ARCH_" not in new  # DB 영역만, DB 활성 기준 복사
        assert c.get("/admin/analytics?product=ORADB").status_code == 200
        assert c.get("/admin/results?product=ORADB").status_code == 200
        upload = c.get("/admin/items/upload").text
        assert "WLS_DEPLOY" in upload and "DB_BACKUP" in upload
