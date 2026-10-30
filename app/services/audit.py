from sqlalchemy.orm import Session

from app.models import AuditLog


def log_audit(db: Session, user_id: int | None, action: str, target: str | None = None,
              detail: dict | None = None) -> AuditLog:
    """감사 로그 추가. commit 은 호출자의 트랜잭션에 맡긴다."""
    row = AuditLog(user_id=user_id, action=action, target=target, detail=detail)
    db.add(row)
    return row
