"""관리자: 응시자 계정 관리(가입 승인·반려, 비밀번호 재설정, 잠금 해제)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import User, utcnow
from app.services import accounts
from app.services import user_admin as svc
from app.services.mailer import MailError, is_configured
from app.web.deps import get_settings_dep, require_admin
from app.web.templating import flash, render

router = APIRouter(prefix="/admin/users", dependencies=[Depends(require_admin)])


def _target(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="사용자를 찾을 수 없습니다")
    return u


@router.get("")
def user_list(request: Request, q: str = "", role: str = "", status: str = "", page: int = 1,
              user: User = Depends(require_admin), db: Session = Depends(get_db),
              settings: Settings = Depends(get_settings_dep)):
    page = min(max(page, 1), 100_000)  # 비정상적으로 큰 값은 OFFSET 오버플로 방지를 위해 제한
    users, total = svc.list_users(db, q[:100], role, page, status)
    pages = max((total + svc.PER_PAGE - 1) // svc.PER_PAGE, 1)
    return render(request, "admin_users.html", user=user, users=users, total=total, page=page, pages=pages,
                  q=q, role=role, status=status, now=utcnow(), pending=svc.pending_users(db),
                  mail_ready=is_configured(settings))


@router.post("/{user_id:int}/approve")
def approve(user_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db),
            settings: Settings = Depends(get_settings_dep)):
    target = _target(db, user_id)
    try:
        accounts.approve(db, settings, target, user)
        flash(request, f"{target.login_id} 가입을 승인하고 비밀번호를 메일로 보냈습니다.")
    except accounts.AccountError as e:
        flash(request, str(e), "error")
    except MailError as e:
        flash(request, f"메일을 보내지 못해 승인하지 않았습니다: {e}", "error")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{user_id:int}/reject")
def reject(user_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = _target(db, user_id)
    try:
        accounts.reject(db, target, user)
        flash(request, f"{target.login_id} 가입 신청을 반려했습니다.")
    except accounts.AccountError as e:
        flash(request, str(e), "error")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/{user_id:int}/reset-password")
def reset_password(user_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = _target(db, user_id)
    try:
        cred = svc.reset_password(db, target, user)
    except svc.UserAdminError as e:
        flash(request, str(e), "error")
        return RedirectResponse("/admin/users", status_code=303)
    return render(request, "admin_credentials.html", user=user, creds=[cred], title=f"{cred.login_id} 비밀번호 재설정")


@router.post("/{user_id:int}/unlock")
def unlock(user_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = _target(db, user_id)
    try:
        svc.unlock(db, target, user)
        flash(request, f"{target.login_id} 계정의 잠금을 해제했습니다.")
    except svc.UserAdminError as e:
        flash(request, str(e), "error")
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/{user_id:int}/delete")
def delete_confirm(user_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = _target(db, user_id)
    return render(request, "admin_user_delete.html", user=user, target=target, attempts=svc.attempt_count(db, target))


@router.post("/{user_id:int}/delete")
def delete(user_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    target = _target(db, user_id)
    login_id = target.login_id
    try:
        n = svc.delete_user(db, target, user)
        flash(request, f"{login_id} 계정을 삭제했습니다(응시 기록 {n}건 함께 삭제).")
    except svc.UserAdminError as e:
        flash(request, str(e), "error")
    return RedirectResponse("/admin/users", status_code=303)
