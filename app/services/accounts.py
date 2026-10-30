"""응시자 계정: 이메일 가입 신청 → 관리자 승인(비밀번호 메일 발송), 비밀번호 재설정 링크, 본인 비밀번호 변경.

보안 원칙
  - 가입 신청 단계에서는 메일을 보내지 않는다. 메일은 관리자가 승인할 때만 나가므로 남의 주소로 신청해도 스팸이 되지 않는다.
  - 가입 신청·재설정 요청은 계정 존재 여부와 관계없이 같은 결과를 보여 준다(계정 존재 노출 방지).
  - 재설정 토큰은 원문을 메일로만 보내고 DB 에는 SHA-256 해시만 저장한다. 1회용, 만료 있음, 새로 발급하면 이전 링크는 무효.
  - 비밀번호는 로그·감사 기록에 남기지 않는다.
"""
import hashlib
import re
import secrets
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import PasswordResetToken, Role, User, UserStatus, utcnow
from app.services import mailer
from app.services.audit import log_audit
from app.services.security import hash_password, verify_password
from app.services.user_admin import generate_password

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
UNUSABLE_PASSWORD = "!"  # bcrypt 해시가 아니므로 어떤 비밀번호로도 로그인할 수 없다(승인 대기 계정)
MIN_PASSWORD_LEN = 10
MAX_PASSWORD_BYTES = 72


class AccountError(ValueError):
    """사용자에게 그대로 보여 줘도 되는 입력 오류."""


def normalize_email(raw: str) -> str:
    email = (raw or "").strip().lower()
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AccountError("올바른 이메일 주소를 입력해 주세요")
    return email


def _by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.login_id) == email))


# ---------------- 가입 신청 / 승인 ----------------
def request_registration(db: Session, email: str, name: str) -> None:
    email = normalize_email(email)
    name = (name or "").strip()
    if not 1 <= len(name) <= 100:
        raise AccountError("이름은 1~100자로 입력해 주세요")
    user = _by_email(db, email)
    if user is None:
        db.add(User(login_id=email, name=name, password_hash=UNUSABLE_PASSWORD, role=Role.CANDIDATE,
                    status=UserStatus.PENDING))
        result = "new"
    elif user.status == UserStatus.REJECTED:  # 반려된 사람은 다시 신청할 수 있다
        user.status, user.name, user.created_at = UserStatus.PENDING, name, utcnow()
        result = "re_request"
    else:  # 이미 승인됐거나 대기 중: 아무것도 바꾸지 않는다(화면 안내는 동일)
        result = "ignored"
    log_audit(db, None, "USER_REGISTER_REQUEST", email, {"result": result})
    db.commit()


def _require_pending_candidate(user: User) -> None:
    if user.role != Role.CANDIDATE or user.status != UserStatus.PENDING:
        raise AccountError("승인 대기 중인 응시자 신청만 처리할 수 있습니다")


def approve(db: Session, settings: Settings, user: User, actor: User) -> None:
    """승인: 비밀번호를 만들어 메일로 보낸다. 메일 발송에 실패하면 승인하지 않는다(대기 상태 유지)."""
    _require_pending_candidate(user)
    pw = generate_password()
    user.password_hash = hash_password(pw)
    user.status = UserStatus.ACTIVE
    user.failed_login_count, user.locked_until = 0, None
    db.flush()
    try:
        mailer.send_mail(settings, user.login_id, "[OGG 자격시험] 가입이 승인되었습니다",
                         f"{user.name}님, OGG 자격시험 가입이 승인되었습니다.\n\n"
                         f"접속 주소: {mailer.base_url(settings)}/login\n"
                         f"아이디: {user.login_id}\n"
                         f"비밀번호: {pw}\n\n"
                         "로그인 후 오른쪽 위 '비밀번호 변경'에서 원하는 비밀번호로 바꿀 수 있습니다.\n"
                         "본인이 신청하지 않았다면 이 메일을 무시해 주세요.\n")
    except mailer.MailError:
        db.rollback()
        raise
    log_audit(db, actor.id, "USER_APPROVED", f"user:{user.id}", {"login_id": user.login_id})
    db.commit()


def reject(db: Session, user: User, actor: User) -> None:
    _require_pending_candidate(user)
    user.status = UserStatus.REJECTED
    log_audit(db, actor.id, "USER_REJECTED", f"user:{user.id}", {"login_id": user.login_id})
    db.commit()


def pending_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.status == UserStatus.PENDING)) or 0


# ---------------- 비밀번호 재설정(링크) ----------------
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def request_password_reset(db: Session, settings: Settings, raw_email: str) -> None:
    """재설정 링크를 메일로 보낸다. 대상이 아니거나 너무 자주 요청하면 조용히 무시한다(화면 안내는 동일)."""
    try:
        email = normalize_email(raw_email)
    except AccountError:
        return
    user = _by_email(db, email)
    if user is None or user.role != Role.CANDIDATE or user.status != UserStatus.ACTIVE:
        log_audit(db, None, "PASSWORD_RESET_REQUEST", email, {"result": "ignored"})
        db.commit()
        return
    now = utcnow()
    recent = db.scalar(select(func.count()).select_from(PasswordResetToken).where(
        PasswordResetToken.user_id == user.id, PasswordResetToken.created_at > now - timedelta(hours=1))) or 0
    if recent >= settings.password_reset_max_per_hour:
        log_audit(db, user.id, "PASSWORD_RESET_REQUEST", email, {"result": "throttled"})
        db.commit()
        return
    # 이전에 보낸 링크는 모두 무효로 만든다
    db.execute(update(PasswordResetToken).where(PasswordResetToken.user_id == user.id,
                                                PasswordResetToken.used_at.is_(None)).values(used_at=now))
    token = secrets.token_urlsafe(32)
    db.add(PasswordResetToken(user_id=user.id, token_hash=_hash_token(token), created_at=now,
                              expires_at=now + timedelta(minutes=settings.password_reset_minutes)))
    db.flush()
    try:
        mailer.send_mail(settings, user.login_id, "[OGG 자격시험] 비밀번호 재설정 안내",
                         f"{user.name}님, 비밀번호 재설정을 요청하셨습니다.\n\n"
                         f"아래 링크를 열고 '새 비밀번호 받기' 버튼을 누르면 새 비밀번호가 메일로 발송됩니다.\n"
                         f"{mailer.base_url(settings)}/password/reset/{token}\n\n"
                         f"이 링크는 {settings.password_reset_minutes}분 동안 한 번만 사용할 수 있습니다.\n"
                         "본인이 요청하지 않았다면 이 메일을 무시해 주세요. 비밀번호는 바뀌지 않습니다.\n")
    except mailer.MailError:
        db.rollback()
        log_audit(db, user.id, "PASSWORD_RESET_REQUEST", email, {"result": "mail_failed"})
        db.commit()
        return
    log_audit(db, user.id, "PASSWORD_RESET_REQUEST", email, {"result": "sent"})
    db.commit()


def valid_token(db: Session, token: str) -> PasswordResetToken | None:
    if not token or len(token) > 200:
        return None
    t = db.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == _hash_token(token)))
    if t is None or t.used_at is not None or t.expires_at <= utcnow():
        return None
    user = db.get(User, t.user_id)
    if user is None or user.status != UserStatus.ACTIVE:
        return None
    return t


def complete_password_reset(db: Session, settings: Settings, token: str) -> bool:
    """링크의 버튼을 누르면 새 비밀번호를 만들어 메일로 보낸다. 메일 발송에 실패하면 아무것도 바꾸지 않는다."""
    t = valid_token(db, token)
    if t is None:
        return False
    user = db.get(User, t.user_id)
    pw = generate_password()
    user.password_hash = hash_password(pw)
    user.failed_login_count, user.locked_until = 0, None
    user.session_version += 1
    t.used_at = utcnow()
    db.flush()
    try:
        mailer.send_mail(settings, user.login_id, "[OGG 자격시험] 새 비밀번호 안내",
                         f"{user.name}님, 새 비밀번호를 안내해 드립니다.\n\n"
                         f"접속 주소: {mailer.base_url(settings)}/login\n"
                         f"아이디: {user.login_id}\n"
                         f"새 비밀번호: {pw}\n\n"
                         "로그인 후 오른쪽 위 '비밀번호 변경'에서 원하는 비밀번호로 바꿀 수 있습니다.\n")
    except mailer.MailError:
        db.rollback()
        raise
    log_audit(db, user.id, "PASSWORD_RESET_DONE", user.login_id)
    db.commit()
    return True


# ---------------- 본인 비밀번호 변경 ----------------
def change_password(db: Session, user: User, current: str, new: str, confirm: str) -> None:
    if not verify_password(current or "", user.password_hash):
        raise AccountError("현재 비밀번호가 올바르지 않습니다")
    if len(new or "") < MIN_PASSWORD_LEN:
        raise AccountError(f"새 비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다")
    if len(new.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise AccountError("새 비밀번호가 너무 깁니다(UTF-8 72바이트 이하, 한글은 글자당 3바이트)")
    if new != confirm:
        raise AccountError("새 비밀번호 확인이 일치하지 않습니다")
    if new == current:
        raise AccountError("현재 비밀번호와 다른 비밀번호를 입력해 주세요")
    user.password_hash = hash_password(new)
    user.session_version += 1  # 다른 곳의 세션은 끊는다(지금 화면은 라우터가 새 버전으로 유지)
    log_audit(db, user.id, "USER_PASSWORD_CHANGED", user.login_id)
    db.commit()
