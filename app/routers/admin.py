"""관리자 영역 진입(대시보드). 문항 관리는 admin_items 라우터."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.accounts import pending_count
from app.services.item_admin import status_counts
from app.web.deps import require_admin
from app.web.templating import render

router = APIRouter(prefix="/admin")


@router.get("")
def admin_home(request: Request, user=Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin_home.html", user=user, counts=status_counts(db), pending_users=pending_count(db))
