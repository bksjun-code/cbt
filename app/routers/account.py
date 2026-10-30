"""공개 계정 화면: 가입 신청, 비밀번호 재설정(메일 링크), 본인 비밀번호 변경."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import get_db
from app.models import User
from app.services import accounts
from app.services.accounts import AccountError
from app.services.mailer import MailError
from app.web.deps import current_user, get_settings_dep
from app.web.templating import flash, render

router = APIRouter()

REGISTER_DONE = ("가입 신청이 접수되었습니다", "관리자가 승인하면 로그인 비밀번호가 입력하신 이메일로 발송됩니다. "
                 "이미 가입된 이메일이라면 기존 비밀번호로 로그인하시거나 비밀번호 재발급을 이용해 주세요.")
FORGOT_DONE = ("메일을 확인해 주세요", "가입된 이메일이라면 비밀번호 재설정 링크를 보냈습니다. 링크는 30분 동안 한 번만 사용할 수 있습니다. "
               "메일이 오지 않으면 스팸함을 확인하거나 잠시 후 다시 시도해 주세요.")


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many(request: Request) -> bool:
    return not request.app.state.account_limiter.allow(_client_key(request))


@router.get("/register")
def register_page(request: Request):
    return render(request, "register.html", form={})


@router.post("/register")
def register(request: Request, email: str = Form("", max_length=300), name: str = Form("", max_length=200),
             db: Session = Depends(get_db)):
    if _too_many(request):
        return render(request, "register.html", status_code=429, form={"email": email, "name": name},
                      error="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    try:
        accounts.request_registration(db, email, name)
    except AccountError as e:
        return render(request, "register.html", status_code=422, form={"email": email, "name": name}, error=str(e))
    return render(request, "notice.html", title=REGISTER_DONE[0], message=REGISTER_DONE[1])


@router.get("/password/forgot")
def forgot_page(request: Request):
    return render(request, "forgot.html")


@router.post("/password/forgot")
def forgot(request: Request, email: str = Form("", max_length=300), db: Session = Depends(get_db),
           settings: Settings = Depends(get_settings_dep)):
    if _too_many(request):
        return render(request, "forgot.html", status_code=429, error="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    accounts.request_password_reset(db, settings, email)
    return render(request, "notice.html", title=FORGOT_DONE[0], message=FORGOT_DONE[1])


INVALID_LINK = ("링크를 사용할 수 없습니다", "만료되었거나 이미 사용한 링크입니다. 로그인 화면의 '비밀번호를 잊으셨나요?'에서 다시 요청해 주세요.")


@router.get("/password/reset/{token}")
def reset_page(token: str, request: Request, db: Session = Depends(get_db)):
    # 링크를 여는 것만으로는 아무것도 바꾸지 않는다(메일 보안 스캐너의 자동 클릭 대비) → 버튼(POST)으로 처리
    if accounts.valid_token(db, token) is None:
        return render(request, "notice.html", status_code=410, title=INVALID_LINK[0], message=INVALID_LINK[1])
    return render(request, "reset_confirm.html", token=token)


@router.post("/password/reset/{token}")
def reset(token: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)):
    try:
        ok = accounts.complete_password_reset(db, settings, token)
    except MailError:
        return render(request, "notice.html", status_code=503, title="메일을 보내지 못했습니다",
                      message="잠시 후 같은 링크로 다시 시도해 주세요. 비밀번호는 바뀌지 않았습니다.")
    if not ok:
        return render(request, "notice.html", status_code=410, title=INVALID_LINK[0], message=INVALID_LINK[1])
    return render(request, "notice.html", title="새 비밀번호를 메일로 보냈습니다",
                  message="메일에 있는 새 비밀번호로 로그인해 주세요. 로그인 후 '비밀번호 변경'에서 바꿀 수 있습니다.")


@router.get("/account/password")
def password_page(request: Request, user: User = Depends(current_user)):
    return render(request, "account_password.html", user=user)


@router.post("/account/password")
def change_password(request: Request, current: str = Form("", max_length=200), new: str = Form("", max_length=200),
                    confirm: str = Form("", max_length=200), user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    try:
        accounts.change_password(db, user, current, new, confirm)
    except AccountError as e:
        return render(request, "account_password.html", status_code=422, user=user, error=str(e))
    request.session["sv"] = user.session_version  # 변경한 이 화면은 로그인 유지
    flash(request, "비밀번호를 변경했습니다.")
    return RedirectResponse("/admin" if user.role.value == "ADMIN" else "/", status_code=303)
