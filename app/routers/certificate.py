"""합격증 진위 확인(공개, 로그인 불필요)."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services import certificates
from app.web.templating import render

router = APIRouter()


def _too_many(request: Request) -> bool:
    key = request.client.host if request.client else "unknown"
    return not request.app.state.verify_limiter.allow(key)


def _verify(request: Request, db: Session, number: str):
    if _too_many(request):
        return render(request, "verify.html", status_code=429, number=number, checked=False,
                      error="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.")
    attempt = certificates.find(db, number)
    valid = certificates.is_valid(attempt)
    holder = db.get(User, attempt.user_id) if valid else None
    return render(request, "verify.html", number=number.strip().upper(), checked=True, valid=valid and holder is not None,
                  holder=holder, attempt=attempt, issuer=request.app.state.settings.certificate_issuer,
                  title=request.app.state.settings.certificate_title,
                  passed_on=certificates.passed_on(attempt) if valid else None)


@router.get("/verify")
def verify_form(request: Request, no: str = "", db: Session = Depends(get_db)):
    if no.strip():
        return _verify(request, db, no[:60])
    return render(request, "verify.html", number="", checked=False)


@router.get("/verify/{number}")
def verify_number(number: str, request: Request, db: Session = Depends(get_db)):
    return _verify(request, db, number[:60])
