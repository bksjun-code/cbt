"""합격증: 합격자만 발급, 본인만 다운로드, 인증번호 진위 확인, 보정·삭제 시 무효."""
import re

import pymupdf
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import AuditLog, ExamAttempt, User
from app.services import certificates, exam
from tests.test_web import HX, PW, env, login, page_token, token  # noqa: F401

BASE = "https://cbt.example.com"


def pdf_text(raw: bytes) -> str:
    return "\n".join(p.get_text() for p in pymupdf.open(stream=raw, filetype="pdf"))


def take_exam(Session, login_id: str, correct: bool) -> int:
    with Session() as db:
        u = db.scalar(select(User).where(User.login_id == login_id))
        a, _ = exam.start_attempt(db, u)
        if correct:
            for ai in a.items:
                exam.save_answer(db, a, ai.seq, list(ai.item.correct_keys))
        exam.submit_attempt(db, a)
        return a.id


@pytest.fixture()
def web(env):  # noqa: F811
    build, Session = env
    with TestClient(build(public_base_url=BASE)) as c:
        yield c, Session


def test_passed_candidate_downloads_a_korean_pdf_certificate(web):
    c, Session = web
    aid = take_exam(Session, "cand", correct=True)
    login(c, "cand")
    assert f'href="/exam/{aid}/certificate.pdf"' in c.get("/").text          # 응시 이력의 '합격증 PDF'
    assert f'href="/exam/{aid}/certificate.pdf"' in c.get(f"/exam/{aid}/result").text
    r = c.get(f"/exam/{aid}/certificate.pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    m = re.search(r'filename="certificate_(RP-OGG-\d{8}-[0-9A-F]{6})\.pdf"', r.headers["content-disposition"])
    number = m.group(1)
    text = pdf_text(r.content)
    for expected in ("합  격  증", "응시자", "Rockplace DI팀", "Oracle GoldenGate 사내 자격시험", number,
                     f"{BASE}/verify/{number}", "공식 자격증이 아닙니다"):
        assert expected in text, expected
    assert "Damien" not in text and "Oracle University" not in text          # 공식 인증서 문구·서명 미사용
    with Session() as db:
        assert db.get(ExamAttempt, aid).certificate_no == number
        assert db.scalars(select(AuditLog).where(AuditLog.action == "CERTIFICATE_ISSUED")).one().detail == {"certificate_no": number}


def test_number_is_issued_once_and_reused(web):
    c, Session = web
    aid = take_exam(Session, "cand", correct=True)
    login(c, "cand")
    first = c.get(f"/exam/{aid}/certificate.pdf").headers["content-disposition"]
    second = c.get(f"/exam/{aid}/certificate.pdf").headers["content-disposition"]
    assert first == second
    with Session() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "CERTIFICATE_ISSUED").count() == 1


def test_failed_or_in_progress_exam_has_no_certificate(web):
    c, Session = web
    failed = take_exam(Session, "cand", correct=False)
    login(c, "cand")
    assert "certificate.pdf" not in c.get("/").text
    assert c.get(f"/exam/{failed}/certificate.pdf").status_code == 403
    r = c.post("/exam/start", data={"csrf_token": page_token(c)}, follow_redirects=False)
    running = int(r.headers["location"].rsplit("/", 1)[1])
    assert c.get(f"/exam/{running}/certificate.pdf").status_code in (303, 403)  # 진행 중: 시험 화면으로 보내거나 거부
    with Session() as db:
        assert db.get(ExamAttempt, running).certificate_no is None


def test_other_candidates_cannot_download_someone_elses_certificate(web):
    c, Session = web
    aid = take_exam(Session, "cand", correct=True)
    login(c, "cand2")
    assert c.get(f"/exam/{aid}/certificate.pdf").status_code == 404
    c.cookies.clear()
    assert c.get(f"/exam/{aid}/certificate.pdf", follow_redirects=False).status_code == 303  # 비로그인


def test_admin_can_download_from_result_detail(web):
    c, Session = web
    aid = take_exam(Session, "cand", correct=True)
    login(c, "admin")
    assert f'href="/admin/results/{aid}/certificate.pdf"' in c.get(f"/admin/results/{aid}").text
    r = c.get(f"/admin/results/{aid}/certificate.pdf")
    assert r.status_code == 200 and "응시자" in pdf_text(r.content)
    assert "인증번호 RP-OGG-" in c.get(f"/admin/results/{aid}").text


def test_public_verification_page(web):
    c, Session = web
    aid = take_exam(Session, "cand", correct=True)
    login(c, "cand")
    number = re.search(r"(RP-OGG-[0-9A-F-]+)\.pdf", c.get(f"/exam/{aid}/certificate.pdf").headers["content-disposition"]).group(1)
    c.cookies.clear()                                                         # 로그인 없이 누구나 확인
    page = c.get(f"/verify/{number}").text
    assert "유효한 합격증입니다" in page and "응시자" in page and number in page
    assert "cand" not in page.replace("candidate", "")                        # 아이디(이메일)는 노출하지 않음
    assert "유효한 합격증입니다" in c.get("/verify", params={"no": number.lower()}).text  # 소문자 입력도 허용
    for bad in ("RP-OGG-20990101-000000", "nope", "' OR 1=1 --", "x" * 100):
        r = c.get("/verify", params={"no": bad})
        assert r.status_code == 200 and "확인되지 않는 합격증입니다" in r.text


def test_certificate_becomes_invalid_when_override_flips_the_result(web):
    from app.models import OverrideMode
    from app.services.grade_overrides import set_override

    c, Session = web
    aid = take_exam(Session, "cand", correct=True)
    login(c, "cand")
    number = re.search(r"(RP-OGG-[0-9A-F-]+)\.pdf", c.get(f"/exam/{aid}/certificate.pdf").headers["content-disposition"]).group(1)
    with Session() as db:  # 모든 문항을 채점 제외 → 총점 0 → 불합격
        a = db.get(ExamAttempt, aid)
        admin = db.scalar(select(User).where(User.login_id == "admin"))
        for ai in a.items:
            set_override(db, ai.item, OverrideMode.EXCLUDE, "테스트", admin)
        db.commit()
        assert db.get(ExamAttempt, aid).passed is False
    assert c.get(f"/exam/{aid}/certificate.pdf").status_code == 403
    assert "확인되지 않는 합격증입니다" in c.get(f"/verify/{number}").text


def test_verification_is_rate_limited(web):
    c, _ = web
    codes = [c.get("/verify/RP-OGG-20260101-ABCDEF").status_code for _ in range(32)]
    assert codes[-1] == 429 and codes.count(200) == 30


def test_ensure_number_rejects_non_passed_attempts(db):
    import types

    for status, passed in (("IN_PROGRESS", None), ("SUBMITTED", False)):
        from app.models import AttemptStatus

        a = types.SimpleNamespace(status=AttemptStatus(status), passed=passed, certificate_no=None)
        with pytest.raises(certificates.CertificateError):
            certificates.ensure_number(db, a, None)
    assert certificates.find(db, "") is None and certificates.find(db, "XX-1") is None
