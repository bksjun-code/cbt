"""한 계정 한 곳 로그인: 새로 로그인하면 이전 PC 의 세션은 끊긴다. 로그아웃·비밀번호 변경도 다른 세션을 무효화한다."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, User
from tests.test_web import HX, PW, env, login, page_token, token  # noqa: F401


def two_pcs(env):  # noqa: F811
    build, Session = env
    app = build()
    return TestClient(app), TestClient(app), Session


def test_second_login_logs_out_the_first_pc_with_a_message(env):  # noqa: F811
    pc1, pc2, Session = two_pcs(env)
    with pc1, pc2:
        login(pc1, "cand")
        assert pc1.get("/", follow_redirects=False).status_code == 200
        login(pc2, "cand")                                         # 같은 아이디로 다른 PC 로그인
        assert pc2.get("/", follow_redirects=False).status_code == 200
        r = pc1.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        page = pc1.get("/login").text
        assert "다른 곳에서 같은 아이디로 로그인하여" in page       # 왜 로그아웃됐는지 안내
        assert "다른 곳에서" not in pc1.get("/login").text           # 안내는 한 번만
        assert pc2.get("/", follow_redirects=False).status_code == 200  # 새 PC 는 그대로
        with Session() as db:
            assert db.scalars(select(AuditLog).where(AuditLog.action == "SESSION_REPLACED")).first() is not None


def test_kicked_pc_in_the_middle_of_an_exam_is_sent_to_login_and_cannot_save(env):  # noqa: F811
    pc1, pc2, Session = two_pcs(env)
    with pc1, pc2:
        login(pc1, "cand")
        r = pc1.post("/exam/start", data={"csrf_token": page_token(pc1)}, follow_redirects=False)
        aid = int(r.headers["location"].rsplit("/", 1)[1])
        hdr = {**HX, "X-CSRF-Token": page_token(pc1)}
        assert pc1.put(f"/exam/{aid}/q/1/answer", data={"selected": "A"}, headers=hdr).status_code == 200

        login(pc2, "cand")
        r = pc1.put(f"/exam/{aid}/q/1/answer", data={"selected": "B"}, headers=hdr)  # 자동 저장 시도
        assert r.status_code == 200 and r.headers["HX-Redirect"] == "/login"
        with Session() as db:
            from app.models import AttemptItem

            assert db.get(AttemptItem, (aid, 1)).selected_keys == ["A"]  # 끊긴 PC 의 저장은 반영되지 않음
        # 새 PC 는 같은 시험을 이어서 본다
        r = pc2.post("/exam/start", data={"csrf_token": page_token(pc2)}, follow_redirects=False)
        assert r.headers["location"] == f"/exam/{aid}"


def test_logout_invalidates_copies_of_the_old_cookie(env):  # noqa: F811
    pc1, pc2, _ = two_pcs(env)
    with pc1, pc2:
        login(pc1, "cand")
        stolen = dict(pc1.cookies)                                 # 쿠키를 복사해 둔 경우(탈취·다른 PC)
        pc1.post("/logout", data={"csrf_token": page_token(pc1)})
        pc2.cookies.update(stolen)
        r = pc2.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_changing_password_keeps_this_pc_and_logs_out_others(env):  # noqa: F811
    pc1, pc2, _ = two_pcs(env)
    with pc1, pc2:
        login(pc1, "cand")
        other = dict(pc1.cookies)
        pc2.cookies.update(other)                                  # 같은 세션을 다른 곳에서 쓰는 상황
        r = pc1.post("/account/password", data={"current": PW, "new": "changed-password-1", "confirm": "changed-password-1",
                                                "csrf_token": token(pc1.get("/account/password").text)}, follow_redirects=False)
        assert r.status_code == 303
        assert pc1.get("/", follow_redirects=False).status_code == 200   # 변경한 화면은 유지
        assert pc2.get("/", follow_redirects=False).status_code == 303   # 이전 쿠키는 무효


def test_admin_password_reset_logs_the_candidate_out(env):  # noqa: F811
    pc1, admin, Session = two_pcs(env)
    with pc1, admin:
        login(pc1, "cand")
        login(admin, "admin")
        with Session() as db:
            uid = db.scalar(select(User).where(User.login_id == "cand")).id
        admin.post(f"/admin/users/{uid}/reset-password", data={"csrf_token": token(admin.get("/admin").text)})
        assert pc1.get("/", follow_redirects=False).status_code == 303


def test_admin_single_session_too(env):  # noqa: F811
    pc1, pc2, _ = two_pcs(env)
    with pc1, pc2:
        login(pc1, "admin")
        login(pc2, "admin")
        assert pc1.get("/admin", follow_redirects=False).status_code == 303
        assert pc2.get("/admin", follow_redirects=False).status_code == 200


def test_relogin_on_the_same_pc_works_normally(env):  # noqa: F811
    pc1, _, _ = two_pcs(env)
    with pc1:
        login(pc1, "cand")
        login(pc1, "cand")
        assert pc1.get("/", follow_redirects=False).status_code == 200
        assert "다른 곳에서" not in pc1.get("/").text
