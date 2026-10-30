"""감사 로그 조회."""
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, User
from app.services.timeutil import kst_day_start_utc

PER_PAGE = 50


def list_audit(db: Session, action: str = "", q: str = "", date_from: date | None = None,
               date_to: date | None = None, page: int = 1):
    stmt = select(AuditLog, User.login_id).outerjoin(User, User.id == AuditLog.user_id)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if q.strip():
        like = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        stmt = stmt.where(User.login_id.ilike(like, escape="\\") | AuditLog.target.ilike(like, escape="\\"))
    if date_from:
        stmt = stmt.where(AuditLog.created_at >= kst_day_start_utc(date_from))
    if date_to:
        stmt = stmt.where(AuditLog.created_at < kst_day_start_utc(date_to + timedelta(days=1)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(AuditLog.id.desc()).limit(PER_PAGE).offset(max(page - 1, 0) * PER_PAGE)).all()
    return rows, total


def distinct_actions(db: Session) -> list[str]:
    return list(db.scalars(select(AuditLog.action).distinct().order_by(AuditLog.action)))
