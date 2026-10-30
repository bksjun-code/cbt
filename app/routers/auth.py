from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import Role
from app.services.audit import log_audit
from app.services.auth import authenticate
from app.web.deps import current_user, get_settings_dep, start_session
from app.web.templating import render

router = APIRouter()

LOGIN_FAIL_MSG = "아이디 또는 비밀번호가 올바르지 않거나, 계정이 일시 잠겨 있습니다."


def home_for(role: Role) -> str:
    return "/admin" if role == Role.ADMIN else "/"


@router.get("/login")
def login_page(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login")
def login(request: Request, login_id: str = Form(max_length=255), password: str = Form(max_length=200),
          db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    user = authenticate(db, login_id, password, settings)
    if user is None:
        return render(request, "login.html", status_code=401, error=LOGIN_FAIL_MSG, login_id=login_id)
    start_session(request, user)  # 다른 PC 의 세션은 이 시점에 무효가 된다
    db.commit()
    return RedirectResponse(home_for(user.role), status_code=303)


@router.post("/logout")
def logout(request: Request, user=Depends(current_user), db: Session = Depends(get_db)):
    log_audit(db, user.id, "LOGOUT", user.login_id)
    user.session_version += 1  # 이 쿠키를 복사해 둔 곳이 있어도 더는 쓸 수 없다
    db.commit()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
