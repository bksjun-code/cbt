"""웹 계층 공통 의존성: 세션 사용자, 역할 검사, CSRF."""
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import Role, User
from app.services.audit import log_audit

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class LoginRequired(Exception):
    pass


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        request.session["csrf"] = tok
    return tok


async def csrf_protect(request: Request) -> None:
    """상태 변경 요청(POST/PUT/DELETE…)은 세션 토큰과 일치하는 CSRF 토큰이 있어야 한다."""
    if request.method in SAFE_METHODS:
        return
    expected = request.session.get("csrf")
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            supplied = (await request.form()).get("csrf_token")
    if not expected or not supplied or not secrets.compare_digest(str(supplied), expected):
        raise HTTPException(status_code=403, detail="CSRF 검증 실패")


KICKED_MSG = "다른 곳에서 같은 아이디로 로그인하여 이 화면은 로그아웃되었습니다."


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if user is None:
        request.session.clear()
        raise LoginRequired()
    if request.session.get("sv") != user.session_version:
        # 한 계정 한 곳 로그인: 다른 곳에서 새로 로그인했거나, 로그아웃·비밀번호 변경으로 이 세션이 무효가 됐다
        log_audit(db, user.id, "SESSION_REPLACED", user.login_id)
        db.commit()
        request.session.clear()
        request.session["flash"] = {"kind": "error", "text": KICKED_MSG}
        raise LoginRequired()
    return user


def start_session(request: Request, user: User) -> None:
    """로그인 성공 시 호출: 세션 버전을 올려 다른 곳의 세션을 모두 끊고, 이 브라우저에 새 버전을 준다."""
    user.session_version += 1
    request.session.clear()  # 세션 고정 방지(CSRF 토큰도 새로 발급)
    request.session["uid"] = user.id
    request.session["sv"] = user.session_version


def require_candidate(user: User = Depends(current_user)) -> User:
    if user.role != Role.CANDIDATE:
        raise HTTPException(status_code=403, detail="응시자 전용 화면입니다")
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=403, detail="관리자 전용 화면입니다")
    return user


def is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def certificate_response(db, attempt, user, settings, actor_id):
    """합격증 PDF 다운로드 응답(인증번호가 없으면 발급)."""
    from fastapi.responses import Response

    from app.services import certificates

    number = certificates.ensure_number(db, attempt, actor_id)
    pdf = certificates.render_pdf(attempt, user, settings)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="certificate_{number}.pdf"'})
