"""로그인 인증과 계정 잠금(기본 5회 실패 → 10분)."""
from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import User, UserStatus, utcnow
from app.services import security
from app.services.audit import log_audit


@lru_cache
def _dummy_hash() -> str:
    # 존재하지 않는 계정/잠긴 계정에서도 bcrypt 비용을 동일하게 지불해 응답 시간 차이를 줄인다
    return security.hash_password("dummy-password-for-timing")


def authenticate(db: Session, login_id: str, password: str, settings: Settings,
                 now: datetime | None = None) -> User | None:
    """성공 시 User, 실패/잠금 시 None. 실패 사유는 호출자에게 구분해 알리지 않는다."""
    now = now or utcnow()
    login_id = login_id.strip()[:255]
    if "@" in login_id:  # 응시자 아이디는 이메일(소문자로 저장)
        login_id = login_id.lower()
    user = db.scalar(select(User).where(User.login_id == login_id))

    if user is None or user.status != UserStatus.ACTIVE:  # 승인 대기·반려 계정은 로그인 불가(사유는 노출하지 않음)
        security.verify_password(password, _dummy_hash())
        log_audit(db, user.id if user else None, "LOGIN_FAILED", login_id,
                  {"reason": "unknown_user" if user is None else f"status_{user.status.value.lower()}"})
        db.commit()
        return None

    if user.locked_until and user.locked_until > now:
        security.verify_password(password, _dummy_hash())
        log_audit(db, user.id, "LOGIN_BLOCKED", login_id, {"locked_until": user.locked_until.isoformat()})
        db.commit()
        return None

    if security.verify_password(password, user.password_hash):
        user.failed_login_count = 0
        user.locked_until = None
        log_audit(db, user.id, "LOGIN_OK", login_id)
        db.commit()
        return user

    user.failed_login_count += 1
    if user.failed_login_count >= settings.login_max_failures:
        user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
        user.failed_login_count = 0
        log_audit(db, user.id, "ACCOUNT_LOCKED", login_id, {"until": user.locked_until.isoformat()})
    else:
        log_audit(db, user.id, "LOGIN_FAILED", login_id, {"count": user.failed_login_count})
    db.commit()
    return None
