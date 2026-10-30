"""이메일 가입 신청 → 관리자 승인(비밀번호 메일) / 비밀번호 재설정 링크 / 본인 비밀번호 변경."""
import re
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.models import AuditLog, PasswordResetToken, Role, User, UserStatus, utcnow
from app.services import accounts, mailer
from app.services.accounts import AccountError
from app.services.auth import authenticate
from app.services.security import verify_password
from app.web.ratelimit import RateLimiter
from tests.helpers import make_user
from tests.test_web import PW, env, login, token  # noqa: F401

BASE = "https://cbt.example.com"
MAIL = dict(smtp_host="smtp.test", smtp_from="cbt@test", public_base_url=BASE)


@pytest.fixture()
def settings():
    return Settings(cookie_secure=False, scheduler_enabled=False, **MAIL)


@pytest.fixture()
def outbox(monkeypatch):
    """실제 SMTP 대신 보낸 메일을 모은다."""
    sent = []

    def fake_send(settings, to, subject, body):
        if not mailer.is_configured(settings):
            raise mailer.MailError("not configured")
        sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(mailer, "send_mail", fake_send)
    return sent


@pytest.fixture()
def failing_mail(monkeypatch):
    def boom(*a, **k):
        raise mailer.MailError("SMTP down")

    monkeypatch.setattr(mailer, "send_mail", boom)


@pytest.fixture()
def admin(db):
    return make_user(db, role=Role.ADMIN)


def password_in(mail: dict) -> str:
    return re.search(r"비밀번호: (\S+)", mail["body"]).group(1)


def link_in(mail: dict) -> str:
    return re.search(rf"{re.escape(BASE)}/password/reset/(\S+)", mail["body"]).group(1)


def audit_blob(db) -> str:
    return " ".join(f"{a.action} {a.target} {a.detail}" for a in db.scalars(select(AuditLog)))


# ================= 가입 신청 =================
def test_registration_creates_pending_account_without_sending_mail(db, outbox):
    accounts.request_registration(db, "  Hong.GD@Example.COM ", "홍길동")
    u = db.scalar(select(User))
    assert u.login_id == "hong.gd@example.com" and u.name == "홍길동"
    assert u.status == UserStatus.PENDING and u.role == Role.CANDIDATE
    assert not verify_password("", u.password_hash) and u.password_hash == accounts.UNUSABLE_PASSWORD
    assert outbox == []  # 신청 단계에서는 메일을 보내지 않는다(남의 주소로 신청해도 스팸이 되지 않음)
    assert "USER_REGISTER_REQUEST" in audit_blob(db)


@pytest.mark.parametrize("email", ["", "not-an-email", "a@b", "a b@c.com", "x@" + "a" * 250 + ".com", "a@b..com"])
def test_registration_rejects_invalid_email(db, email):
    with pytest.raises(AccountError, match="이메일"):
        accounts.request_registration(db, email, "이름")


@pytest.mark.parametrize("name", ["", "   ", "가" * 101])
def test_registration_rejects_invalid_name(db, name):
    with pytest.raises(AccountError, match="이름"):
        accounts.request_registration(db, "a@b.com", name)


def test_registration_for_existing_accounts_changes_nothing(db, admin, outbox, settings):
    accounts.request_registration(db, "a@b.com", "원래 이름")
    accounts.request_registration(db, "A@B.COM", "다른 이름")  # 대기 중 재신청(대소문자 무시)
    u = db.scalar(select(User).where(User.login_id == "a@b.com"))
    assert db.query(User).filter(User.role == Role.CANDIDATE).count() == 1 and u.name == "원래 이름"
    accounts.approve(db, settings, u, admin)
    hash_before = u.password_hash
    accounts.request_registration(db, "a@b.com", "탈취 시도")  # 승인된 계정: 아무 변화 없음
    assert (u.status, u.name, u.password_hash) == (UserStatus.ACTIVE, "원래 이름", hash_before)


def test_rejected_user_can_apply_again(db, admin):
    accounts.request_registration(db, "r@b.com", "반려자")
    u = db.scalar(select(User).where(User.login_id == "r@b.com"))
    accounts.reject(db, u, admin)
    assert u.status == UserStatus.REJECTED
    accounts.request_registration(db, "r@b.com", "다시 신청")
    assert u.status == UserStatus.PENDING and u.name == "다시 신청"


# ================= 승인 / 반려 =================
def test_approval_mails_a_working_password_and_activates(db, admin, outbox, settings):
    accounts.request_registration(db, "new@corp.com", "신규")
    u = db.scalar(select(User).where(User.login_id == "new@corp.com"))
    accounts.approve(db, settings, u, admin)
    assert u.status == UserStatus.ACTIVE and len(outbox) == 1
    mail = outbox[0]
    assert mail["to"] == "new@corp.com" and "승인" in mail["subject"]
    assert f"{BASE}/login" in mail["body"] and "new@corp.com" in mail["body"]
    pw = password_in(mail)
    assert verify_password(pw, u.password_hash)
    assert authenticate(db, "NEW@corp.com", pw, settings) is not None  # 이메일 대소문자 무시 로그인
    assert pw not in audit_blob(db)  # 비밀번호는 감사 기록에 남지 않는다


def test_approval_is_rolled_back_when_mail_fails(db, admin, failing_mail, settings):
    accounts.request_registration(db, "fail@corp.com", "실패")
    u = db.scalar(select(User).where(User.login_id == "fail@corp.com"))
    with pytest.raises(mailer.MailError):
        accounts.approve(db, settings, u, admin)
    db.refresh(u)
    assert u.status == UserStatus.PENDING and u.password_hash == accounts.UNUSABLE_PASSWORD
    assert "USER_APPROVED" not in audit_blob(db)


def test_only_pending_candidates_can_be_approved_or_rejected(db, admin, outbox, settings):
    active = make_user(db)  # 이미 ACTIVE
    with pytest.raises(AccountError):
        accounts.approve(db, settings, active, admin)
    with pytest.raises(AccountError):
        accounts.reject(db, active, admin)
    with pytest.raises(AccountError):
        accounts.approve(db, settings, admin, admin)
    assert outbox == []


def test_pending_and_rejected_accounts_cannot_log_in(db, admin, settings):
    accounts.request_registration(db, "p@corp.com", "대기")
    assert authenticate(db, "p@corp.com", "", settings) is None
    assert authenticate(db, "p@corp.com", accounts.UNUSABLE_PASSWORD, settings) is None
    u = db.scalar(select(User).where(User.login_id == "p@corp.com"))
    accounts.reject(db, u, admin)
    assert authenticate(db, "p@corp.com", "anything", settings) is None
    assert accounts.pending_count(db) == 0


# ================= 비밀번호 재설정 =================
@pytest.fixture()
def member(db, admin, outbox, settings):
    accounts.request_registration(db, "m@corp.com", "회원")
    u = db.scalar(select(User).where(User.login_id == "m@corp.com"))
    accounts.approve(db, settings, u, admin)
    pw = password_in(outbox[-1])
    outbox.clear()
    return u, pw


def test_reset_request_mails_a_one_time_link_and_stores_only_a_hash(db, member, outbox, settings):
    u, _ = member
    accounts.request_password_reset(db, settings, "M@CORP.COM")
    assert len(outbox) == 1 and outbox[0]["to"] == "m@corp.com"
    raw = link_in(outbox[0])
    t = db.scalar(select(PasswordResetToken))
    assert t.token_hash != raw and len(t.token_hash) == 64 and raw not in audit_blob(db)
    assert accounts.valid_token(db, raw) is not None
    assert timedelta(minutes=29) < t.expires_at - utcnow() <= timedelta(minutes=30)


@pytest.mark.parametrize("who", ["unknown@corp.com", "pending", "admin", "not an email"])
def test_reset_request_for_non_members_sends_nothing(db, admin, outbox, settings, who):
    if who == "pending":
        accounts.request_registration(db, "pend@corp.com", "대기")
        who = "pend@corp.com"
    elif who == "admin":
        admin.login_id = "boss@corp.com"
        db.commit()
        who = "boss@corp.com"  # 관리자는 메일 재설정 대상이 아니다(CLI 사용)
    accounts.request_password_reset(db, settings, who)
    assert outbox == [] and db.query(PasswordResetToken).count() == 0


def test_reset_requests_are_throttled_per_account(db, member, outbox, settings):
    for _ in range(5):
        accounts.request_password_reset(db, settings, "m@corp.com")
    assert len(outbox) == settings.password_reset_max_per_hour == 3  # 메일 폭탄 방지


def test_new_reset_link_invalidates_older_links(db, member, outbox, settings):
    accounts.request_password_reset(db, settings, "m@corp.com")
    accounts.request_password_reset(db, settings, "m@corp.com")
    first, second = link_in(outbox[0]), link_in(outbox[1])
    assert accounts.valid_token(db, first) is None and accounts.valid_token(db, second) is not None


def test_completing_reset_mails_new_password_and_burns_the_link(db, member, outbox, settings):
    u, old_pw = member
    u.failed_login_count, u.locked_until = 4, utcnow() + timedelta(minutes=5)
    db.commit()
    accounts.request_password_reset(db, settings, "m@corp.com")
    raw = link_in(outbox[-1])
    assert accounts.complete_password_reset(db, settings, raw) is True
    new_pw = password_in(outbox[-1])
    assert new_pw != old_pw and verify_password(new_pw, u.password_hash) and not verify_password(old_pw, u.password_hash)
    assert u.failed_login_count == 0 and u.locked_until is None
    assert accounts.complete_password_reset(db, settings, raw) is False  # 1회용
    assert new_pw not in audit_blob(db)


def test_expired_or_garbage_tokens_are_rejected(db, member, outbox, settings):
    accounts.request_password_reset(db, settings, "m@corp.com")
    raw = link_in(outbox[-1])
    db.scalar(select(PasswordResetToken)).expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert accounts.valid_token(db, raw) is None and accounts.complete_password_reset(db, settings, raw) is False
    for bad in ("", "x", "A" * 500, raw[:-1]):
        assert accounts.valid_token(db, bad) is None


def test_reset_completion_changes_nothing_when_mail_fails(db, member, outbox, settings, monkeypatch):
    u, old_pw = member
    accounts.request_password_reset(db, settings, "m@corp.com")
    raw = link_in(outbox[-1])
    monkeypatch.setattr(mailer, "send_mail", lambda *a, **k: (_ for _ in ()).throw(mailer.MailError("down")))
    with pytest.raises(mailer.MailError):
        accounts.complete_password_reset(db, settings, raw)
    db.refresh(u)
    assert verify_password(old_pw, u.password_hash) and accounts.valid_token(db, raw) is not None  # 다시 시도 가능


# ================= 본인 비밀번호 변경 =================
@pytest.mark.parametrize("current,new,confirm,needle", [
    ("wrong-password", "new-password-1", "new-password-1", "현재 비밀번호"),
    (None, "short", "short", "10자"),
    (None, "new-password-1", "new-password-2", "일치"),
    (None, None, None, "다른 비밀번호"),
    (None, "가" * 25, "가" * 25, "너무 깁니다"),
])
def test_change_password_validations(db, member, current, new, confirm, needle):
    u, pw = member
    with pytest.raises(AccountError, match=needle):
        accounts.change_password(db, u, current or pw, new or pw, confirm or pw)
    assert verify_password(pw, u.password_hash)


def test_change_password_success(db, member, settings):
    u, pw = member
    accounts.change_password(db, u, pw, "my-new-password", "my-new-password")
    assert authenticate(db, "m@corp.com", "my-new-password", settings) is not None
    assert authenticate(db, "m@corp.com", pw, settings) is None
    assert "my-new-password" not in audit_blob(db)


# ================= 메일 발송 모듈 =================
class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, **kw):
        self.host, self.port, self.calls = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(("login", user, pw))

    def send_message(self, msg):
        self.calls.append(("send", msg["To"], msg["Subject"], msg.get_content()))


def test_send_mail_uses_starttls_login_and_proper_headers(monkeypatch):
    FakeSMTP.instances.clear()
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    s = Settings(smtp_host="smtp.gmail.com", smtp_port=587, smtp_user="u@gmail.com", smtp_password="app-pass",
                 smtp_from="u@gmail.com", public_base_url=BASE)
    mailer.send_mail(s, "to@corp.com", "제목", "본문 한글")
    calls = FakeSMTP.instances[0].calls
    assert calls[0] == "starttls" and calls[1] == ("login", "u@gmail.com", "app-pass")
    assert calls[2][:3] == ("send", "to@corp.com", "제목") and "본문 한글" in calls[2][3]


def test_send_mail_errors_are_wrapped_and_header_injection_is_blocked(monkeypatch):
    s = Settings(smtp_host="smtp.test", smtp_from="a@test", public_base_url=BASE)
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    with pytest.raises(mailer.MailError):
        mailer.send_mail(s, "to@corp.com", "제목\r\nBcc: victim@x.com", "본문")
    with pytest.raises(mailer.MailError, match="설정"):
        mailer.send_mail(Settings(), "to@corp.com", "제목", "본문")

    def refuse(*a, **k):
        raise ConnectionRefusedError()

    monkeypatch.setattr(mailer.smtplib, "SMTP", refuse)
    with pytest.raises(mailer.MailError, match="ConnectionRefusedError"):
        mailer.send_mail(s, "to@corp.com", "제목", "본문")


def test_rate_limiter_window():
    rl = RateLimiter(limit=2, window_sec=10)
    assert rl.allow("ip", 0) and rl.allow("ip", 1) and not rl.allow("ip", 2)
    assert rl.allow("other", 2)
    assert rl.allow("ip", 11)  # 창이 지나면 다시 허용


# ================= 웹 화면 =================
@pytest.fixture()
def web(env, outbox):  # noqa: F811
    build, Session = env
    with TestClient(build(**MAIL)) as c:
        yield c, Session, outbox


def csrf(c, path="/login"):
    return token(c.get(path).text)


def test_login_page_links_to_register_and_password_reset(web):
    c, _, _ = web
    html = c.get("/login").text
    assert 'href="/register"' in html and 'href="/password/forgot"' in html and "가입한 이메일" in html


def test_full_flow_register_approve_login_change_password(web):
    c, Session, outbox = web
    r = c.post("/register", data={"email": "Kim@Corp.com", "name": "김응시", "csrf_token": csrf(c, "/register")})
    assert r.status_code == 200 and "가입 신청이 접수되었습니다" in r.text and outbox == []

    login(c, "admin")
    page = c.get("/admin/users").text
    assert "kim@corp.com" in page and "승인(비밀번호 메일 발송)" in page
    assert "가입 승인 대기 <b>1</b>명" in c.get("/admin").text
    with Session() as db:
        uid = db.scalar(select(User).where(User.login_id == "kim@corp.com")).id
    r = c.post(f"/admin/users/{uid}/approve", data={"csrf_token": token(c.get("/admin").text)}, follow_redirects=True)
    assert "비밀번호를 메일로 보냈습니다" in r.text and len(outbox) == 1
    pw = password_in(outbox[0])

    c.cookies.clear()
    r = login(c, "KIM@corp.com", pw)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert 'href="/account/password"' in c.get("/").text
    r = c.post("/account/password", data={"current": pw, "new": "brand-new-pass", "confirm": "brand-new-pass",
                                          "csrf_token": csrf(c, "/account/password")}, follow_redirects=True)
    assert "비밀번호를 변경했습니다" in r.text
    c.cookies.clear()
    assert login(c, "kim@corp.com", "brand-new-pass").status_code == 303


def test_register_errors_csrf_and_rate_limit(web):
    c, Session, _ = web
    assert c.post("/register", data={"email": "a@b.com", "name": "x"}).status_code == 403  # CSRF
    r = c.post("/register", data={"email": "bad", "name": "x", "csrf_token": csrf(c, "/register")})
    assert r.status_code == 422 and "이메일" in r.text and 'value="bad"' in r.text
    codes = [c.post("/register", data={"email": f"u{i}@b.com", "name": "x", "csrf_token": csrf(c, "/register")}).status_code
             for i in range(6)]
    assert codes[-1] == 429 and codes.count(200) == 4  # 위의 422 1회 포함 IP 당 10분에 5회
    with Session() as db:
        assert db.query(User).filter(User.status == UserStatus.PENDING).count() == 4


def test_registration_response_is_identical_for_new_and_existing_emails(web):
    c, _, _ = web
    a = c.post("/register", data={"email": "cand@exists.com", "name": "x", "csrf_token": csrf(c, "/register")}).text
    b = c.post("/register", data={"email": "cand@exists.com", "name": "y", "csrf_token": csrf(c, "/register")}).text
    strip = lambda s: re.sub(r'value="[^"]+"', "", s)  # noqa: E731  (CSRF 토큰 값만 다름)
    assert strip(a) == strip(b)


def test_forgot_and_reset_via_web_with_scanner_safe_link(web):
    c, Session, outbox = web
    with Session() as db:
        accounts.request_registration(db, "w@corp.com", "웹")
        u = db.scalar(select(User).where(User.login_id == "w@corp.com"))
        accounts.approve(db, Settings(**MAIL), u, db.scalar(select(User).where(User.login_id == "admin")))
    old_pw = password_in(outbox[-1])
    r = c.post("/password/forgot", data={"email": "w@corp.com", "csrf_token": csrf(c, "/password/forgot")},
               headers={"Host": "evil.example"})  # Host 헤더를 조작해도 링크는 설정된 주소로 만들어진다
    assert "메일을 확인해 주세요" in r.text
    assert f"{BASE}/password/reset/" in outbox[-1]["body"] and "evil.example" not in outbox[-1]["body"]
    raw = link_in(outbox[-1])

    for _ in range(3):  # 메일 보안 스캐너가 링크를 미리 열어도 비밀번호가 바뀌지 않는다
        r = c.get(f"/password/reset/{raw}")
        assert r.status_code == 200 and "새 비밀번호 받기" in r.text
    with Session() as db:
        assert verify_password(old_pw, db.scalar(select(User).where(User.login_id == "w@corp.com")).password_hash)

    r = c.post(f"/password/reset/{raw}", data={"csrf_token": csrf(c, f"/password/reset/{raw}")})
    assert "새 비밀번호를 메일로 보냈습니다" in r.text
    new_pw = password_in(outbox[-1])
    assert login(c, "w@corp.com", new_pw).status_code == 303
    c.cookies.clear()
    assert c.get(f"/password/reset/{raw}").status_code == 410  # 1회용


def test_forgot_response_is_identical_for_unknown_email(web):
    c, _, outbox = web
    r = c.post("/password/forgot", data={"email": "nobody@corp.com", "csrf_token": csrf(c, "/password/forgot")})
    assert "메일을 확인해 주세요" in r.text and outbox == []
    assert c.get("/password/reset/not-a-real-token").status_code == 410


def test_account_password_requires_login(web):
    c, _, _ = web
    r = c.get("/account/password", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_admin_reject_and_mail_not_configured_blocks_approval(env, outbox):  # noqa: F811
    build, Session = env
    with TestClient(build()) as c:  # 메일 설정 없음
        tok = csrf(c, "/register")
        c.post("/register", data={"email": "z@corp.com", "name": "제트", "csrf_token": tok})
        login(c, "admin")
        page = c.get("/admin/users").text
        assert "메일 설정(SMTP_HOST" in page and "disabled" in page
        with Session() as db:
            uid = db.scalar(select(User).where(User.login_id == "z@corp.com")).id
        r = c.post(f"/admin/users/{uid}/approve", data={"csrf_token": token(c.get("/admin").text)}, follow_redirects=True)
        assert "승인하지 않았습니다" in r.text
        r = c.post(f"/admin/users/{uid}/reject", data={"csrf_token": token(c.get("/admin").text)}, follow_redirects=True)
        assert "반려했습니다" in r.text
        with Session() as db:
            assert db.get(User, uid).status == UserStatus.REJECTED
    assert outbox == []


def test_bulk_registration_endpoints_are_gone(web):
    c, _, _ = web
    login(c, "admin")
    t = token(c.get("/admin").text)
    assert c.post("/admin/users/upload", data={"csrf_token": t}).status_code in (404, 405)
    assert c.post("/admin/users/credentials.csv", data={"csrf_token": t}).status_code in (404, 405)
    assert "일괄 등록" not in c.get("/admin/users").text


# ================= 가입자 삭제 =================
def test_delete_user_removes_account_attempts_answers_and_tokens(seeded, outbox, settings):
    from app.models import AttemptItem, ExamAttempt
    from app.services import exam, user_admin

    adm = make_user(seeded, role=Role.ADMIN)
    accounts.request_registration(seeded, "del@corp.com", "삭제대상")
    u = seeded.scalar(select(User).where(User.login_id == "del@corp.com"))
    accounts.approve(seeded, settings, u, adm)
    a, _ = exam.start_attempt(seeded, u)
    exam.submit_attempt(seeded, a)
    exam.start_attempt(seeded, u)  # 진행 중 시험도 있음
    accounts.request_password_reset(seeded, settings, "del@corp.com")
    uid = u.id
    assert user_admin.attempt_count(seeded, u) == 2

    assert user_admin.delete_user(seeded, u, adm) == 2
    assert seeded.get(User, uid) is None
    assert seeded.query(ExamAttempt).filter(ExamAttempt.user_id == uid).count() == 0
    assert seeded.query(AttemptItem).count() == 0
    assert seeded.query(PasswordResetToken).count() == 0
    assert seeded.query(AuditLog).filter(AuditLog.user_id == uid).count() == 0  # 감사 로그는 남기되 연결만 끊음
    log = seeded.scalars(select(AuditLog).where(AuditLog.action == "USER_DELETED")).one()
    assert log.user_id == adm.id and log.detail == {"login_id": "del@corp.com", "attempts_deleted": 2}
    accounts.request_registration(seeded, "del@corp.com", "다시 가입")  # 같은 이메일로 다시 신청 가능
    assert seeded.scalar(select(User).where(User.login_id == "del@corp.com")).status == UserStatus.PENDING


def test_admin_accounts_cannot_be_deleted(db):
    from app.services import user_admin

    a1, a2 = make_user(db, role=Role.ADMIN), make_user(db, role=Role.ADMIN)
    with pytest.raises(user_admin.UserAdminError, match="관리자"):
        user_admin.delete_user(db, a2, a1)
    assert db.get(User, a2.id) is not None


def test_delete_via_web_requires_confirmation_page_and_logs_out_the_user(web):
    c, Session, _ = web
    login(c, "cand")
    c.post("/exam/start", data={"csrf_token": token(c.get("/").text)})
    cand_cookies = dict(c.cookies)
    c.cookies.clear()
    login(c, "admin")
    with Session() as db:
        uid = db.scalar(select(User).where(User.login_id == "cand")).id
    assert f'href="/admin/users/{uid}/delete"' in c.get("/admin/users").text
    page = c.get(f"/admin/users/{uid}/delete").text
    assert "함께 삭제될 응시 기록" in page and "<b>1건</b>" in page and "되돌릴 수 없습니다" in page
    assert c.post(f"/admin/users/{uid}/delete").status_code == 403  # CSRF
    r = c.post(f"/admin/users/{uid}/delete", data={"csrf_token": token(c.get("/admin").text)}, follow_redirects=True)
    assert "계정을 삭제했습니다(응시 기록 1건 함께 삭제)" in r.text
    with Session() as db:
        assert db.get(User, uid) is None
    assert c.get(f"/admin/users/{uid}/delete").status_code == 404
    c.cookies.clear()
    c.cookies.update(cand_cookies)  # 삭제된 사람의 기존 세션은 더 이상 통하지 않는다
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_admin_delete_page_refuses_admin_and_candidates_cannot_delete(web):
    c, Session, _ = web
    with Session() as db:
        admin_id = db.scalar(select(User).where(User.login_id == "admin")).id
        cand2 = db.scalar(select(User).where(User.login_id == "cand2")).id
    login(c, "admin")
    assert "삭제할 수 없습니다" in c.get(f"/admin/users/{admin_id}/delete").text
    r = c.post(f"/admin/users/{admin_id}/delete", data={"csrf_token": token(c.get("/admin").text)}, follow_redirects=True)
    assert "삭제할 수 없습니다" in r.text
    c.cookies.clear()
    login(c, "cand")
    t = token(c.get("/").text)
    assert c.get(f"/admin/users/{cand2}/delete").status_code == 403
    assert c.post(f"/admin/users/{cand2}/delete", data={"csrf_token": t}).status_code == 403
    with Session() as db:
        assert db.get(User, cand2) is not None
