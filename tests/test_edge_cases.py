"""분기 커버리지로 드러난 경계·오류 경로 테스트."""
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import StatementError
from sqlalchemy.orm import Session

from app.models import AttemptStatus, AuditLog, Blueprint, ExamAttempt, User, utcnow
from app.schemas.item import ItemIn
from app.services import exam
from app.services.blueprint_validation import BlueprintError, validate_blueprint
from app.services.exam import NoActiveBlueprint
from app.services.item_admin import domain_by_code
from app.services.item_analysis import ItemStat
from app.services.selection import build_plan
from app.web.charts import radar
from tests.helpers import make_blueprint, make_user
from tests.test_web import HX, PW, client, env, login, page_token, token  # noqa: F401


# ================= 서비스/모델 경계 =================
def test_finalize_is_idempotent_and_keeps_first_result(seeded):
    a, _ = exam.start_attempt(seeded, make_user(seeded))
    exam.submit_attempt(seeded, a)
    snap = (a.status, a.submitted_at, a.total_score, a.passed)
    again = exam.finalize_attempt(seeded, a, AttemptStatus.EXPIRED, utcnow() + timedelta(hours=9))
    assert again is a and (a.status, a.submitted_at, a.total_score, a.passed) == snap


def test_empty_blueprint_never_creates_a_zero_question_exam(db):
    """규칙이 하나도 없는 Blueprint 가 활성이어도(DB 직접 수정 등) 문항 0개짜리 시험을 만들지 않는다."""
    from app.seed import seed_domains

    seed_domains(db)
    db.add(Blueprint(name="빈 기준표", total_items=0, time_limit_min=60, is_active=True))
    db.flush()
    u = make_user(db)
    with pytest.raises(NoActiveBlueprint, match="출제 규칙이 없습니다"):
        exam.start_attempt(db, u)
    assert db.query(ExamAttempt).count() == 0
    log = db.scalars(select(AuditLog).where(AuditLog.action == "EXAM_CREATE_REFUSED")).one()
    assert log.detail == {"reason": "empty_blueprint"}


def test_build_plan_with_no_rules_returns_empty_plan(db):
    bp = make_blueprint(db, {})
    plan = build_plan(db, bp, set(), "seed")
    assert plan.items == [] and plan.relaxations == []


def test_blueprint_validation_checks_ratio_and_pass_score(seeded):
    bp = seeded.scalar(select(Blueprint))
    bp.domain_min_ratio = 1.5
    with pytest.raises(BlueprintError, match="domain_min_ratio"):
        validate_blueprint(bp)
    bp.domain_min_ratio = 0.4
    for bad in (0, -5, 100.5):
        bp.pass_score = bad
        with pytest.raises(BlueprintError, match="pass_score"):
            validate_blueprint(bp)
    bp.pass_score = 100  # 경계값은 허용
    validate_blueprint(bp)


def test_blueprint_validation_reports_all_problems_at_once(seeded):
    bp = seeded.scalar(select(Blueprint))
    bp.total_items, bp.domain_min_ratio, bp.pass_score = 41, -0.1, 0
    with pytest.raises(BlueprintError) as e:
        validate_blueprint(bp)
    assert str(e.value).count(";") == 2


def test_domain_by_code_unknown_raises(seeded):
    assert domain_by_code(seeded, "ARCH").code == "ARCH"
    with pytest.raises(ValueError, match="알 수 없는 영역"):
        domain_by_code(seeded, "NOPE")


def test_naive_datetime_is_rejected_on_write(db):
    """DB 는 항상 UTC aware 만 받는다(타임존 혼동으로 만료/통계가 틀어지는 것을 방지)."""
    db.add(User(login_id="naive", name="x", password_hash="h", created_at=datetime(2026, 1, 1)))
    with pytest.raises((ValueError, StatementError)):
        db.flush()


def test_item_schema_rejects_whitespace_only_option():
    base = {"domain": "OPS", "difficulty": "LOW", "item_type": "SINGLE", "stem": "s", "explanation": "e",
            "variant_group": "g", "correct_keys": ["A"]}
    with pytest.raises(ValidationError, match="빈 보기"):
        ItemIn.model_validate({**base, "options": {"A": "정상", "B": "   "}})
    with pytest.raises(ValidationError, match="빈 보기"):
        ItemIn.model_validate({**base, "options": {"A": "정상", "B": ""}})


def test_item_stat_without_data_has_dash_label_and_never_warns(seeded):
    from app.models import Item

    st = ItemStat(item=seeded.scalars(select(Item)).first())
    assert st.p_label == "-" and not st.warn and not st.negative


def test_radar_with_fewer_than_three_axes_is_empty_not_crash():
    for labels in ([], ["A"], ["A", "B"]):
        r = radar(labels, [0.5] * len(labels), 0.4)
        assert r["area"] == "" and r["rings"] == [] and r["labels"] == []


def test_radar_clamps_out_of_range_ratios():
    r = radar(["a", "b", "c"], [-1.0, 2.0, 0.5], 0.4)
    assert r["area"] and all(len(p.split(",")) == 2 for p in r["area"].split())


def test_get_db_yields_a_session_and_closes_it():
    from app.db import get_db

    gen = get_db()
    db = next(gen)
    assert isinstance(db, Session)
    with pytest.raises(StopIteration):
        next(gen)  # finally: close() 실행


def test_cli_create_admin_password_mismatch_creates_nothing(tmp_path, monkeypatch, capsys):
    from sqlalchemy.orm import sessionmaker

    from app import cli
    from app.db import Base, make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    Base.metadata.create_all(eng)
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    answers = iter(["correct-horse-battery", "different-password-1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": next(answers))
    assert cli.main(["create-admin", "boss", "대표"]) == 1
    assert "일치하지 않습니다" in capsys.readouterr().err
    with cli.SessionLocal() as db:
        assert db.scalar(select(User).where(User.login_id == "boss")) is None
    eng.dispose()


# ================= 웹: 종료된 시험에 대한 모든 요청 경로 =================
def start(c) -> int:
    r = c.post("/exam/start", data={"csrf_token": page_token(c)}, follow_redirects=False)
    return int(r.headers["location"].rsplit("/", 1)[1])


def hx(c):
    return {**HX, "X-CSRF-Token": page_token(c)}


def test_login_page_when_already_logged_in_redirects_home(client):  # noqa: F811
    login(client, "cand")
    r = client.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_closed_attempt_panel_flag_and_answer_redirect_to_result(client):  # noqa: F811
    login(client, "cand")
    aid = start(client)
    client.post(f"/exam/{aid}/submit", data={"csrf_token": page_token(client)})
    result = f"/exam/{aid}/result"
    # HTMX 요청은 HX-Redirect, 일반 요청은 303
    r = client.get(f"/exam/{aid}/q/1", headers=HX, follow_redirects=False)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == result
    r = client.get(f"/exam/{aid}/q/1", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == result
    r = client.put(f"/exam/{aid}/q/1/flag", data={"flagged": "true"}, headers=hx(client))
    assert r.status_code == 200 and r.headers["HX-Redirect"] == result
    r = client.put(f"/exam/{aid}/q/1/flag", data={"flagged": "true", "csrf_token": page_token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == result
    r = client.put(f"/exam/{aid}/q/1/answer", data={"selected": "A", "csrf_token": page_token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == result


def test_flag_after_expiry_auto_submits_and_redirects(client, env):  # noqa: F811
    _, Session_ = env
    login(client, "cand")
    aid = start(client)
    with Session_() as db:
        db.get(ExamAttempt, aid).expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    r = client.put(f"/exam/{aid}/q/1/flag", data={"flagged": "true"}, headers=hx(client))
    assert r.status_code == 200 and r.headers["HX-Redirect"] == f"/exam/{aid}/result"
    with Session_() as db:
        assert db.get(ExamAttempt, aid).status == AttemptStatus.EXPIRED


def test_invalid_seq_for_flag_and_answer_is_rejected(client):  # noqa: F811
    login(client, "cand")
    aid = start(client)
    assert client.put(f"/exam/{aid}/q/99/flag", data={"flagged": "true"}, headers=hx(client)).status_code == 400
    assert client.put(f"/exam/{aid}/q/99/answer", data={"selected": "A"}, headers=hx(client)).status_code == 400
    assert client.put(f"/exam/{aid}/q/0/answer", data={"selected": "A"}, headers=hx(client)).status_code == 400
    assert client.get(f"/exam/{aid}/q/99", headers=HX).status_code == 404


# ================= 웹: 관리자 오류 경로 =================
def admin_post(c, path, data=None, **kw):
    return c.post(path, data={"csrf_token": token(c.get("/admin").text), **(data or {})}, **kw)


def test_unlock_admin_account_via_ui_is_refused_with_message(client, env):  # noqa: F811
    _, Session_ = env
    login(client, "admin")
    with Session_() as db:
        admin_id = db.scalar(select(User).where(User.login_id == "admin")).id
    r = admin_post(client, f"/admin/users/{admin_id}/unlock", follow_redirects=True)
    assert "관리자 계정은 이 화면에서 관리할 수 없습니다" in r.text


def test_blueprint_rename_to_existing_name_returns_422(client, env):  # noqa: F811
    _, Session_ = env
    login(client, "admin")
    form = {"name": "두번째", "time_limit_min": "30", "pass_score": "70", "domain_min_percent": "40", "recent_exclude_n": "1",
            "count_ARCH_LOW": "3", "points_ARCH_LOW": "10", "count_ARCH_MID": "3", "points_ARCH_MID": "10",
            "count_ARCH_HIGH": "2", "points_ARCH_HIGH": "20"}
    r = admin_post(client, "/admin/blueprints/new", form, follow_redirects=False)
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    r = admin_post(client, f"/admin/blueprints/{new_id}", {**form, "name": "기본 40문항"})
    assert r.status_code == 422 and "같은 이름" in r.text
    with Session_() as db:
        assert db.get(Blueprint, new_id).name == "두번째"


# ================= 퍼징에서 발견해 고친 문제들의 회귀 테스트 =================
@pytest.mark.parametrize("pw,ok", [("a" * 72, True), ("a" * 73, False), ("가" * 24, True), ("가" * 25, False)])  # 한글은 글자당 3바이트
def test_cli_rejects_passwords_over_bcrypt_limit_instead_of_crashing(tmp_path, monkeypatch, capsys, pw, ok):
    from sqlalchemy.orm import sessionmaker

    from app import cli
    from app.db import Base, make_engine
    from app.services.security import verify_password

    eng = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    Base.metadata.create_all(eng)
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": pw)
    rc = cli.main(["create-admin", "boss", "대표"])
    assert (rc == 0) is ok
    with cli.SessionLocal() as db:
        user = db.scalar(select(User).where(User.login_id == "boss"))
        if ok:
            assert verify_password(pw, user.password_hash)
        else:
            assert user is None and "너무 깁니다" in capsys.readouterr().err
    eng.dispose()


def test_item_list_ignores_values_that_are_not_valid_enums(seeded):
    from app.services import item_admin as svc

    everything = svc.list_items(seeded)[1]
    for kw in ({"difficulty": "' OR '1'='1"}, {"status": "NOPE"}, {"item_type": "x"}, {"domain_code": "NOPE", "difficulty": ""}):
        total = svc.list_items(seeded, **kw)[1]
        assert total in (everything, 0)  # 존재하지 않는 영역은 결과 없음, enum 이 아닌 값은 필터 무시
    assert svc.list_items(seeded, difficulty="HIGH")[1] < everything     # 올바른 값은 정상 필터링


def test_huge_ids_and_page_numbers_are_404_or_clamped_not_500(client):  # noqa: F811
    login(client, "admin")
    for path in ("/admin/items/99999999999999999999", "/admin/results/99999999999999999999", "/admin/blueprints/99999999999999999999",
                 "/admin/analytics/items/99999999999999999999"):
        r = client.get(path)
        assert r.status_code == 404 and "Traceback" not in r.text, path
    for path in ("/admin/items", "/admin/results", "/admin/users", "/admin/audit", "/admin/analytics"):
        assert client.get(path, params={"page": "9" * 40}).status_code == 200, path
    client.cookies.clear()
    login(client, "cand")
    assert client.get("/exam/99999999999999999999").status_code == 404
    assert client.get("/exam/1/q/99999999999999999999", headers=HX).status_code in (404, 303, 200)


def test_json_upload_depth_and_digit_bombs_are_clean_format_errors():
    from app.services.item_import import ImportFormatError, parse_upload

    for raw in (b"[" * 100000, b'{"a":' * 50000, b"[" + b"1" * 5000 + b"]"):
        with pytest.raises(ImportFormatError, match="JSON 구조가 비정상"):
            parse_upload("bomb.json", raw)
