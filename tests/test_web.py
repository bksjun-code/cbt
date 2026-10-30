import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Base, get_db, make_engine
from app.main import create_app
from app.models import (
    AttemptStatus, AuditLog, ExamAttempt, Item, ItemStatus, Role, User, utcnow,
)
from app.seed import run_seed
from tests.helpers import make_user

PW = "candidate-pw-123"
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


@pytest.fixture()
def env(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'web.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        run_seed(s, samples=True)
        from app.services.security import hash_password

        s.add(User(login_id="cand", name="응시자", password_hash=hash_password(PW), role=Role.CANDIDATE))
        s.add(User(login_id="cand2", name="응시자2", password_hash=hash_password(PW), role=Role.CANDIDATE))
        s.add(User(login_id="admin", name="관리자", password_hash=hash_password(PW), role=Role.ADMIN))
        s.commit()

    def build(**kw):
        app = create_app(Settings(cookie_secure=False, scheduler_enabled=False, secret_key="test-secret", **kw))

        def _db():
            with Session() as db:
                yield db

        app.dependency_overrides[get_db] = _db
        return app

    yield build, Session
    engine.dispose()


@pytest.fixture()
def client(env):
    build, _ = env
    with TestClient(build()) as c:
        yield c


@pytest.fixture()
def Session(env):
    return env[1]


def token(html: str) -> str:
    return CSRF_RE.search(html).group(1)


def login(c: TestClient, who="cand", pw=PW, follow=False):
    t = token(c.get("/login").text)
    return c.post("/login", data={"login_id": who, "password": pw, "csrf_token": t}, follow_redirects=follow)


def page_token(c: TestClient) -> str:
    return token(c.get("/").text)


def start_exam(c: TestClient) -> int:
    r = c.post("/exam/start", data={"csrf_token": page_token(c)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return int(r.headers["location"].rsplit("/", 1)[1])


HX = {"HX-Request": "true"}
NUM_POINTS = re.compile(r'^\s*-?\d+(\.\d+)?,-?\d+(\.\d+)?(\s+-?\d+(\.\d+)?,-?\d+(\.\d+)?)*\s*$')


def _assert_svg_points_numeric(html: str) -> None:
    """템플릿 변수 충돌(예: dict.values)로 points 에 쓰레기가 들어가는 회귀를 막는다."""
    pts = re.findall(r'<polygon points="([^"]*)"', html)
    assert len(pts) >= 6  # 눈금 4 + 과락선 + 득점 영역
    assert all(NUM_POINTS.match(p) for p in pts), pts


# ================= 인증 =================
def test_anonymous_redirected_to_login(client):
    for path in ("/", "/exam/1", "/admin"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_login_success_candidate_and_admin_destinations(client):
    r = login(client)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert client.get("/").status_code == 200
    client.cookies.clear()
    r = login(client, "admin")
    assert r.headers["location"] == "/admin"
    assert client.get("/admin").status_code == 200


def test_login_failure_generic_message_no_user_enumeration(client):
    r1 = login(client, "cand", "wrong-password")
    r2 = login(client, "nobody", "wrong-password")
    assert r1.status_code == r2.status_code == 401
    msg = re.search(r'role="alert">([^<]+)<', r1.text).group(1)
    assert msg in r2.text


def test_lockout_after_5_failures_blocks_correct_password(client, Session):
    for _ in range(5):
        assert login(client, "cand", "bad-password").status_code == 401
    assert login(client, "cand", PW).status_code == 401  # 올바른 비밀번호도 잠금 중에는 거부
    with Session() as db:
        u = db.scalar(select(User).where(User.login_id == "cand"))
        assert u.locked_until > utcnow() + timedelta(minutes=9)
        actions = [a.action for a in db.scalars(select(AuditLog).order_by(AuditLog.id))]
        assert "ACCOUNT_LOCKED" in actions and "LOGIN_BLOCKED" in actions
        u.locked_until = utcnow() - timedelta(seconds=1)  # 10분 경과 시뮬레이션
        db.commit()
    assert login(client, "cand", PW).status_code == 303


def test_failures_below_threshold_do_not_lock_and_success_resets(client, Session):
    for _ in range(4):
        login(client, "cand", "bad-password")
    assert login(client, "cand", PW).status_code == 303
    with Session() as db:
        assert db.scalar(select(User).where(User.login_id == "cand")).failed_login_count == 0


def test_lock_is_per_account(client):
    for _ in range(5):
        login(client, "cand", "bad-password")
    client.cookies.clear()
    assert login(client, "cand2", PW).status_code == 303


def test_logout_requires_post_and_csrf(client):
    login(client)
    assert client.get("/logout").status_code == 405
    assert client.post("/logout").status_code == 403
    r = client.post("/logout", data={"csrf_token": page_token(client)}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_session_rotates_on_login(client):
    client.get("/login")
    before = client.cookies.get("cbt_session")
    login(client)
    assert client.cookies.get("cbt_session") != before


# ================= CSRF / 보안 헤더 / 쿠키 =================
def test_csrf_required_on_post(client):
    login(client)
    assert client.post("/exam/start").status_code == 403
    assert client.post("/exam/start", data={"csrf_token": "forged"}).status_code == 403


def test_csrf_header_accepted_for_htmx(client):
    login(client)
    r = client.post("/exam/start", headers={"X-CSRF-Token": page_token(client)}, follow_redirects=False)
    assert r.status_code == 303


def test_login_post_without_csrf_rejected(client):
    assert client.post("/login", data={"login_id": "cand", "password": PW}).status_code == 403


def test_security_headers(client):
    r = client.get("/login")
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "unsafe-inline" not in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"


def test_session_cookie_flags(env):
    build, _ = env
    app = build()
    app.state.settings = Settings(cookie_secure=False, scheduler_enabled=False, secret_key="x")
    with TestClient(app) as c:
        sc = c.get("/login").headers["set-cookie"].lower()
        assert "httponly" in sc and "samesite=lax" in sc and "secure" not in sc


def test_session_cookie_secure_in_production_mode(env):
    _, Session = env
    app = create_app(Settings(cookie_secure=True, scheduler_enabled=False, secret_key="prod-secret-value"))
    with TestClient(app, base_url="https://testserver") as c:
        sc = c.get("/login").headers["set-cookie"].lower()
        assert "secure" in sc and "httponly" in sc and "samesite=lax" in sc


def test_production_refuses_default_secret():
    with pytest.raises(RuntimeError):
        create_app(Settings(cookie_secure=True, scheduler_enabled=False))


def test_pages_escape_html_in_user_content(client, Session):
    with Session() as db:
        u = db.scalar(select(User).where(User.login_id == "cand"))
        u.name = "<script>alert(1)</script>"
        db.commit()
    login(client)
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


# ================= 역할 =================
def test_candidate_cannot_open_admin_and_admin_cannot_take_exam(client):
    login(client)
    assert client.get("/admin").status_code == 403
    client.cookies.clear()
    login(client, "admin")
    r = client.get("/", follow_redirects=False)  # 관리자는 / 에서 관리자 화면으로 이동
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    t = token(client.get("/admin").text) if "csrf_token" in client.get("/admin").text else "none"
    assert client.post("/exam/start", data={"csrf_token": t}).status_code == 403


# ================= 응시 흐름 =================
def test_start_resume_and_dashboard(client):
    login(client)
    a1 = start_exam(client)
    a2 = start_exam(client)  # 이어하기
    assert a1 == a2
    html = client.get("/").text
    assert "이어서 응시" in html and "시험 시작" not in html


def test_exam_page_renders_question_navigator_and_timer(client):
    login(client)
    aid = start_exam(client)
    html = client.get(f"/exam/{aid}").text
    assert 'id="timer"' in html and "문제 1 / 40" in html
    assert html.count('class="nav-item') == 40
    html7 = client.get(f"/exam/{aid}?q=7").text
    assert "문제 7 / 40" in html7
    assert client.get(f"/exam/{aid}?q=41").status_code == 404


def test_panel_partial_for_htmx_and_redirect_for_plain(client):
    login(client)
    aid = start_exam(client)
    part = client.get(f"/exam/{aid}/q/3", headers=HX)
    assert part.status_code == 200 and "문제 3 / 40" in part.text and "<html" not in part.text
    plain = client.get(f"/exam/{aid}/q/3", follow_redirects=False)
    assert plain.status_code == 303 and plain.headers["location"] == f"/exam/{aid}?q=3"


def _first_item_keys(Session, aid, seq):
    from app.models import AttemptItem

    with Session() as db:
        ai = db.get(AttemptItem, (aid, seq))
        return list(ai.option_order), list(ai.item.correct_keys), ai.item.explanation


def test_answer_save_persists_and_updates_navigator(client, Session):
    login(client)
    aid = start_exam(client)
    order, correct, _ = _first_item_keys(Session, aid, 1)
    r = client.put(f"/exam/{aid}/q/1/answer", data={"selected": [order[0]]}, headers={**HX, "X-CSRF-Token": page_token(client)})
    assert r.status_code == 200 and "저장됨" in r.text and 'hx-swap-oob="true"' in r.text
    assert 'data-unanswered="39"' in r.text
    html = client.get(f"/exam/{aid}?q=1").text
    assert re.search(rf'value="{order[0]}"\s+checked', html)
    # 선택 해제
    client.put(f"/exam/{aid}/q/1/answer", data={}, headers={**HX, "X-CSRF-Token": page_token(client)})
    assert 'data-unanswered="40"' in client.get(f"/exam/{aid}").text


def test_answer_invalid_key_rejected_400(client):
    login(client)
    aid = start_exam(client)
    r = client.put(f"/exam/{aid}/q/1/answer", data={"selected": ["Z"]}, headers={**HX, "X-CSRF-Token": page_token(client)})
    assert r.status_code == 400


def test_answer_put_requires_csrf(client):
    login(client)
    aid = start_exam(client)
    assert client.put(f"/exam/{aid}/q/1/answer", data={"selected": ["A"]}, headers=HX).status_code == 403


def test_flag_toggle_via_htmx(client):
    login(client)
    aid = start_exam(client)
    h = {**HX, "X-CSRF-Token": page_token(client)}
    r = client.put(f"/exam/{aid}/q/2/flag", data={"flagged": "true"}, headers=h)
    assert "검토 표시 해제" in r.text and "nav-item" in r.text
    assert "flagged" in client.get(f"/exam/{aid}").text
    r = client.put(f"/exam/{aid}/q/2/flag", data={"flagged": "false"}, headers=h)
    assert "검토 표시 해제" not in r.text


def test_full_flow_submit_and_result_page(client, Session):
    login(client)
    aid = start_exam(client)
    h = {**HX, "X-CSRF-Token": page_token(client)}
    for seq in range(1, 41):
        _, correct, _ = _first_item_keys(Session, aid, seq)
        client.put(f"/exam/{aid}/q/{seq}/answer", data={"selected": correct}, headers=h)
    r = client.post(f"/exam/{aid}/submit", data={"csrf_token": page_token(client)}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/exam/{aid}/result"
    html = client.get(f"/exam/{aid}/result").text
    assert "100.0" in html and "합격" in html and "<svg" in html and "오답이 없습니다" in html
    _assert_svg_points_numeric(html)
    assert client.get(f"/exam/{aid}", follow_redirects=False).headers["location"] == f"/exam/{aid}/result"
    # 제출 후 저장 시도는 결과 화면으로 유도
    r = client.put(f"/exam/{aid}/q/1/answer", data={"selected": ["A"]}, headers={**HX, "X-CSRF-Token": page_token(client)})
    assert r.headers.get("HX-Redirect") == f"/exam/{aid}/result"


def test_result_shows_wrong_items_and_fail_cutoff(client):
    login(client)
    aid = start_exam(client)
    client.post(f"/exam/{aid}/submit", data={"csrf_token": page_token(client)})
    html = client.get(f"/exam/{aid}/result").text
    assert "불합격" in html and "과락" in html and html.count('class="explain"') == 40
    _assert_svg_points_numeric(html)


def test_other_users_attempt_is_404(client):
    login(client, "cand")
    aid = start_exam(client)
    client.cookies.clear()
    login(client, "cand2")
    for path in (f"/exam/{aid}", f"/exam/{aid}/result", f"/exam/{aid}/q/1"):
        assert client.get(path, headers=HX).status_code == 404
    t = page_token(client)
    assert client.post(f"/exam/{aid}/submit", data={"csrf_token": t}).status_code == 404
    assert client.put(f"/exam/{aid}/q/1/answer", data={"selected": ["A"]}, headers={**HX, "X-CSRF-Token": t}).status_code == 404


def test_result_before_submit_redirects_back_to_exam(client):
    login(client)
    aid = start_exam(client)
    r = client.get(f"/exam/{aid}/result", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/exam/{aid}"


def test_expired_attempt_auto_submitted_on_access(client, Session):
    login(client)
    aid = start_exam(client)
    with Session() as db:
        a = db.get(ExamAttempt, aid)
        a.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    r = client.get(f"/exam/{aid}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/result")
    with Session() as db:
        assert db.get(ExamAttempt, aid).status == AttemptStatus.EXPIRED
    assert "시간 만료" in client.get(f"/exam/{aid}/result").text


def test_save_after_expiry_returns_hx_redirect(client, Session):
    login(client)
    aid = start_exam(client)
    with Session() as db:
        a = db.get(ExamAttempt, aid)
        a.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()
    r = client.put(f"/exam/{aid}/q/1/answer", data={"selected": ["A"]}, headers={**HX, "X-CSRF-Token": page_token(client)})
    assert r.status_code == 200 and r.headers["HX-Redirect"] == f"/exam/{aid}/result"


def test_htmx_request_without_session_gets_hx_redirect_to_login(client):
    r = client.get("/exam/1/q/1", headers=HX, follow_redirects=False)
    assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"


def test_start_blocked_when_pool_insufficient_shows_generic_message(client, Session):
    with Session() as db:
        for it in db.scalars(select(Item)):
            it.status = ItemStatus.RETIRED
        db.commit()
    login(client)
    r = client.post("/exam/start", data={"csrf_token": page_token(client)})
    assert r.status_code == 503 and "관리자에게 문의" in r.text
    assert "ARCH" not in r.text and "필요" not in r.text  # 부족 셀 상세는 응시자에게 비공개
    with Session() as db:
        assert db.scalar(select(AuditLog).where(AuditLog.action == "EXAM_CREATE_REFUSED")) is not None


def test_unknown_route_renders_error_page(client):
    login(client)
    r = client.get("/nope")
    assert r.status_code == 404 and "오류 404" in r.text


def test_docs_endpoints_disabled(client):
    for p in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(p).status_code == 404


# ================= 정답 비노출 (HTTP 응답 수준) =================
def test_in_progress_http_responses_never_leak_answers(client, Session):
    login(client)
    aid = start_exam(client)
    h = {**HX, "X-CSRF-Token": page_token(client)}
    bodies = []
    from app.models import AttemptItem

    with Session() as db:
        meta = [(ai.seq, list(ai.item.correct_keys), ai.item.explanation, ai.item.reference)
                for ai in db.get(ExamAttempt, aid).items]
    for seq, correct, _, _ in meta:
        bodies.append(client.get(f"/exam/{aid}?q={seq}").text)
        bodies.append(client.get(f"/exam/{aid}/q/{seq}", headers=HX).text)
        bodies.append(client.put(f"/exam/{aid}/q/{seq}/answer", data={"selected": correct[:1]}, headers=h).text)
        bodies.append(client.put(f"/exam/{aid}/q/{seq}/flag", data={"flagged": "true"}, headers=h).text)
    bodies.append(client.get("/").text)
    blob = "\n".join(bodies)

    for seq, correct, expl, ref in meta:
        assert expl not in blob, f"해설 노출: {expl}"
    for forbidden in ("correct_keys", "is_correct", "earned_points", "정답", "total_score"):
        assert forbidden not in blob, forbidden
    # 점수/합격 정보는 제출 전 어떤 화면에도 없음
    assert "합격" not in client.get(f"/exam/{aid}").text
    assert client.get(f"/exam/{aid}/result", follow_redirects=False).status_code == 303


def test_no_json_api_exposes_items(client):
    login(client)
    aid = start_exam(client)
    for path in (f"/exam/{aid}/answers", "/api/items", "/items", f"/exam/{aid}.json"):
        r = client.get(path)
        assert 400 <= r.status_code < 500 and "correct" not in r.text


def test_explanations_hidden_in_result_when_setting_off(client, Session):
    from app.services.app_settings import set_setting

    with Session() as db:
        set_setting(db, "show_explanations", False)
        db.commit()
    login(client)
    aid = start_exam(client)
    client.post(f"/exam/{aid}/submit", data={"csrf_token": page_token(client)})
    html = client.get(f"/exam/{aid}/result").text
    assert "공개되지 않습니다" in html and 'class="explain"' not in html
    with Session() as db:
        expl = [ai.item.explanation for ai in db.get(ExamAttempt, aid).items]
    assert not any(e in html for e in expl)


# ================= 스케줄러(만료 자동 제출) =================
def test_scheduler_job_expires_overdue_attempts(env, monkeypatch):
    build, Session = env
    import app.db as dbmod

    monkeypatch.setattr(dbmod, "SessionLocal", Session)  # 작업이 테스트 DB를 쓰도록
    with Session() as db:
        u = db.scalar(select(User).where(User.login_id == "cand"))
        from app.services import exam as exam_svc

        a, _ = exam_svc.start_attempt(db, u, utcnow() - timedelta(hours=3))
        aid = a.id

    app = create_app(Settings(cookie_secure=False, scheduler_enabled=True, secret_key="test-secret"))
    with TestClient(app):  # lifespan 에서 스케줄러 시작/종료
        from apscheduler.schedulers.base import STATE_RUNNING  # noqa: F401
    # lifespan 이 끝나 scheduler 는 종료됨. 작업 함수만 직접 호출해 검증
    from app.main import _start_scheduler

    sched = _start_scheduler()
    try:
        sched.get_job("expire_overdue").func()
    finally:
        sched.shutdown(wait=False)
    with Session() as db:
        assert db.get(ExamAttempt, aid).status == AttemptStatus.EXPIRED


def test_static_assets_have_executable_mime_types(client):
    """nosniff + CSP 환경에서 스크립트/스타일이 올바른 MIME 으로 서빙되어야 실제 브라우저에서 동작한다."""
    js = client.get("/static/js/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert "javascript" in client.get("/static/vendor/htmx.min.js").headers["content-type"]
    assert "text/css" in client.get("/static/css/app.css").headers["content-type"]


def test_static_revalidates_but_pages_are_not_stored(client):
    assert client.get("/static/js/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/login").headers["cache-control"] == "no-store"


def test_templates_have_no_inline_styles_or_scripts():
    """CSP(style-src/script-src 'self')와 충돌하는 인라인 style=/on*=/<script> 를 템플릿에서 금지한다.
    (SVG 프레젠테이션 속성은 CSP 대상이 아니므로 허용)"""
    from pathlib import Path

    bad = []
    for f in Path("app/templates").glob("*.html"):
        text = f.read_text(encoding="utf-8")
        if re.search(r'\sstyle\s*=', text) or re.search(r'\son[a-z]+\s*=\s*["\']', text) or re.search(r'<script(?![^>]*\bsrc=)', text):
            bad.append(f.name)
    assert bad == []
