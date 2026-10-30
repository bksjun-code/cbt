"""관리자: 응시자 계정 관리 — 비밀번호 재설정, 잠금 해제, 목록.

응시자 계정은 이메일 가입 신청 → 관리자 승인으로 만들어진다(services/accounts.py).
관리자가 재설정한 비밀번호는 서버가 무작위로 생성하고 **응답에서 한 번만** 보여 준다.
DB 에는 bcrypt 해시만 저장되며 비밀번호는 로그/감사 기록에 남기지 않는다.
관리 대상은 CANDIDATE 계정뿐이다(ADMIN 계정은 CLI 로만 관리).
"""
import secrets
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.models import AttemptItem, AuditLog, ExamAttempt, PasswordResetToken, Role, User, UserStatus
from app.services.audit import log_audit
from app.services.security import hash_password

PW_LEN = 12
# 혼동 문자(0 O 1 l I) 제외
_UP, _LOW, _DIG = "ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789"
_ALL = _UP + _LOW + _DIG
PER_PAGE = 30


class UserAdminError(Exception):
    pass


def generate_password() -> str:
    while True:
        pw = "".join(secrets.choice(_ALL) for _ in range(PW_LEN))
        if any(c in _UP for c in pw) and any(c in _LOW for c in pw) and any(c in _DIG for c in pw):
            return pw


@dataclass
class Credential:
    login_id: str
    name: str
    password: str


def _managed(user: User, actor: User) -> None:  # actor 는 항상 ADMIN 이고 대상은 CANDIDATE 만 허용하므로 본인 변경은 구조적으로 불가능
    if user.role != Role.CANDIDATE:
        raise UserAdminError("관리자 계정은 이 화면에서 관리할 수 없습니다(CLI 사용)")
    if user.status != UserStatus.ACTIVE:
        raise UserAdminError("승인된 계정만 처리할 수 있습니다(승인 대기·반려 계정은 승인/반려로 처리)")


def reset_password(db: Session, user: User, actor: User) -> Credential:
    _managed(user, actor)
    pw = generate_password()
    user.password_hash = hash_password(pw)
    user.failed_login_count = 0
    user.locked_until = None
    user.session_version += 1  # 기존 로그인 세션은 끊는다
    log_audit(db, actor.id, "USER_PASSWORD_RESET", f"user:{user.id}", {"login_id": user.login_id})
    db.commit()
    return Credential(user.login_id, user.name, pw)


def unlock(db: Session, user: User, actor: User) -> None:
    _managed(user, actor)
    user.failed_login_count = 0
    user.locked_until = None
    log_audit(db, actor.id, "USER_UNLOCK", f"user:{user.id}", {"login_id": user.login_id})
    db.commit()


def list_users(db: Session, q: str = "", role: str = "", page: int = 1, status: str = ""):
    stmt = select(User)
    if role in (Role.ADMIN.value, Role.CANDIDATE.value):
        stmt = stmt.where(User.role == role)
    if status in UserStatus.__members__:
        stmt = stmt.where(User.status == status)
    if q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(User.login_id.ilike(like, escape="\\") | User.name.ilike(like, escape="\\"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(User.id.desc()).limit(PER_PAGE).offset(max(page - 1, 0) * PER_PAGE))
    return list(rows), total


def pending_users(db: Session) -> list[User]:
    return list(db.scalars(select(User).where(User.status == UserStatus.PENDING).order_by(User.created_at)))


def attempt_count(db: Session, user: User) -> int:
    return db.scalar(select(func.count()).select_from(ExamAttempt).where(ExamAttempt.user_id == user.id)) or 0


def delete_user(db: Session, user: User, actor: User) -> int:
    """응시자 계정과 그 사람의 응시 기록(답안 포함)·재설정 링크를 완전히 삭제한다. 되돌릴 수 없다.

    결과 목록·합격률·문항 분석에서도 빠진다. 감사 로그는 관리 이력이므로 남기되 계정 연결(user_id)만 끊는다.
    반환: 삭제한 응시 기록 수
    """
    if user.role != Role.CANDIDATE:
        raise UserAdminError("관리자 계정은 삭제할 수 없습니다(CLI 로 관리)")
    attempt_ids = list(db.scalars(select(ExamAttempt.id).where(ExamAttempt.user_id == user.id)))
    if attempt_ids:
        db.execute(delete(AttemptItem).where(AttemptItem.attempt_id.in_(attempt_ids)))
        db.execute(delete(ExamAttempt).where(ExamAttempt.id.in_(attempt_ids)))
    db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id))
    db.execute(update(AuditLog).where(AuditLog.user_id == user.id).values(user_id=None))
    login_id, uid = user.login_id, user.id
    db.delete(user)
    log_audit(db, actor.id, "USER_DELETED", f"user:{uid}", {"login_id": login_id, "attempts_deleted": len(attempt_ids)})
    db.commit()
    return len(attempt_ids)
