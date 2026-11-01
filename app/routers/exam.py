"""응시자 화면: 대시보드, 시험 시작/이어하기, 문항 이동, 임시 저장, 제출, 결과.

모든 응답은 schemas/exam.py 의 뷰 모델(정답 필드 없음)로만 문항을 렌더링한다.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import AttemptStatus, Domain, ExamAttempt, Product, Role, User, utcnow
from app.schemas.exam import attempt_view, question_view
from app.services import exam as exam_svc
from app.services.exam import AttemptClosed, InvalidAnswer, NoActiveBlueprint
from app.services.results import build_result
from app.services.selection import InsufficientItemsError
from app.web.charts import radar
from app.web.deps import certificate_response, current_user, is_htmx, require_candidate
from app.web.templating import render

router = APIRouter()


def _load_attempt(db: Session, user: User, attempt_id: int) -> ExamAttempt:
    """본인 시험만 조회(타인 시험은 존재 여부를 숨기고 404). 만료됐으면 자동 제출 처리."""
    attempt = db.get(ExamAttempt, attempt_id)
    if attempt is None or attempt.user_id != user.id:
        raise HTTPException(status_code=404, detail="시험을 찾을 수 없습니다")
    if attempt.status == AttemptStatus.IN_PROGRESS and exam_svc.is_expired(attempt, utcnow()):
        exam_svc.finalize_attempt(db, attempt, AttemptStatus.EXPIRED, attempt.expires_at)
    return attempt


def _closed_response(request: Request, attempt_id: int) -> Response:
    url = f"/exam/{attempt_id}/result"
    if is_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=303)


def _remaining(attempt: ExamAttempt, now: datetime | None = None) -> int:
    return max(0, int((attempt.expires_at - (now or utcnow())).total_seconds()))


def _panel_ctx(attempt: ExamAttempt, seq: int) -> dict:
    ai = next((i for i in attempt.items if i.seq == seq), None)
    if ai is None:
        raise HTTPException(status_code=404, detail="문항을 찾을 수 없습니다")
    av = attempt_view(attempt)
    answered, flagged = set(av.answered_seqs), set(av.flagged)
    total = av.total_questions
    return {
        "attempt": attempt, "q": question_view(ai), "total": total,
        "nav": [{"seq": i.seq, "answered": i.seq in answered, "flagged": i.seq in flagged, "current": i.seq == seq}
                for i in attempt.items],
        "unanswered": total - av.answered,
        "prev_seq": seq - 1 if seq > 1 else None,
        "next_seq": seq + 1 if seq < total else None,
        "remaining": _remaining(attempt),
    }


@router.get("/")
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role == Role.ADMIN:  # 관리자는 응시자 대시보드 대신 관리자 화면으로
        return RedirectResponse("/admin", status_code=303)
    attempts = list(db.scalars(select(ExamAttempt).where(ExamAttempt.user_id == user.id).order_by(ExamAttempt.id.desc())))
    for a in attempts:  # 열람 시점에 만료 처리
        if a.status == AttemptStatus.IN_PROGRESS and exam_svc.is_expired(a, utcnow()):
            exam_svc.finalize_attempt(db, a, AttemptStatus.EXPIRED, a.expires_at)
    active = next((a for a in attempts if a.status == AttemptStatus.IN_PROGRESS), None)
    return render(request, "dashboard.html", user=user, attempts=attempts, active=active,
                  choices=exam_svc.product_choices(db), error=request.query_params.get("error"))


@router.post("/exam/start")
def start(request: Request, product: str = Form(""), user: User = Depends(require_candidate),
          db: Session = Depends(get_db)):
    # product 가 비어 있으면 시험이 열린 첫 제품. 화면에 없는 값(직접 조작 등)은 시작 불가 안내와 같게 처리
    chosen = db.scalar(select(Product).where(Product.code == product)) if product else None
    try:
        if product and chosen is None:
            raise NoActiveBlueprint(f"알 수 없는 시험: {product[:20]}")
        attempt, _ = exam_svc.start_attempt(db, user, product_id=chosen.id if chosen else None)
    except (InsufficientItemsError, NoActiveBlueprint):
        # 부족 셀 상세는 관리자 전용 정보(audit_logs/대시보드). 응시자에게는 일반 안내만.
        return render(request, "error.html", status_code=503, user=user,
                      title="시험을 시작할 수 없습니다",
                      message="현재 시험을 구성할 수 없습니다. 관리자에게 문의해 주세요.")
    return RedirectResponse(f"/exam/{attempt.id}", status_code=303)


@router.get("/exam/{attempt_id}")
def exam_page(attempt_id: int, request: Request, q: int = 1, user: User = Depends(require_candidate),
              db: Session = Depends(get_db)):
    attempt = _load_attempt(db, user, attempt_id)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return RedirectResponse(f"/exam/{attempt.id}/result", status_code=303)
    return render(request, "exam.html", user=user, **_panel_ctx(attempt, q))


@router.get("/exam/{attempt_id}/q/{seq}")
def question_panel(attempt_id: int, seq: int, request: Request, user: User = Depends(require_candidate),
                   db: Session = Depends(get_db)):
    attempt = _load_attempt(db, user, attempt_id)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return _closed_response(request, attempt.id)
    if not is_htmx(request):
        return RedirectResponse(f"/exam/{attempt.id}?q={seq}", status_code=303)
    return render(request, "_panel.html", **_panel_ctx(attempt, seq))


@router.put("/exam/{attempt_id}/q/{seq}/answer")
def save_answer(attempt_id: int, seq: int, request: Request, selected: list[str] = Form(default=[]),
                user: User = Depends(require_candidate), db: Session = Depends(get_db)):
    attempt = _load_attempt(db, user, attempt_id)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return _closed_response(request, attempt.id)
    try:
        exam_svc.save_answer(db, attempt, seq, selected)
    except AttemptClosed:
        return _closed_response(request, attempt.id)
    except InvalidAnswer as e:
        raise HTTPException(status_code=400, detail=str(e))
    ctx = _panel_ctx(attempt, seq)
    return render(request, "_save_status.html", saved_at=utcnow(), **ctx)


@router.put("/exam/{attempt_id}/q/{seq}/flag")
def flag(attempt_id: int, seq: int, request: Request, flagged: bool = Form(),
         user: User = Depends(require_candidate), db: Session = Depends(get_db)):
    attempt = _load_attempt(db, user, attempt_id)
    if attempt.status != AttemptStatus.IN_PROGRESS:
        return _closed_response(request, attempt.id)
    try:
        exam_svc.set_flag(db, attempt, seq, flagged)
    except AttemptClosed:
        return _closed_response(request, attempt.id)
    except InvalidAnswer as e:
        raise HTTPException(status_code=400, detail=str(e))
    return render(request, "_flag_button.html", oob_nav=True, **_panel_ctx(attempt, seq))


@router.post("/exam/{attempt_id}/submit")
def submit(attempt_id: int, user: User = Depends(require_candidate), db: Session = Depends(get_db)):
    attempt = _load_attempt(db, user, attempt_id)
    exam_svc.submit_attempt(db, attempt)
    return RedirectResponse(f"/exam/{attempt.id}/result", status_code=303)


@router.get("/exam/{attempt_id}/result", response_class=HTMLResponse)
def result(attempt_id: int, request: Request, user: User = Depends(require_candidate),
           db: Session = Depends(get_db)):
    attempt = _load_attempt(db, user, attempt_id)
    if attempt.status == AttemptStatus.IN_PROGRESS:
        return RedirectResponse(f"/exam/{attempt.id}", status_code=303)
    view = build_result(db, attempt)
    names = {d.code: d.name for d in db.scalars(select(Domain))}
    chart = radar([names.get(d.code, d.code) for d in view.domains], [d.ratio for d in view.domains],
                  view.domain_min_ratio)
    return render(request, "result.html", user=user, r=view, chart=chart, product=attempt.blueprint.product)


@router.get("/exam/{attempt_id}/certificate.pdf")
def certificate(attempt_id: int, request: Request, user: User = Depends(require_candidate), db: Session = Depends(get_db)):
    from app.services.certificates import CertificateError, is_valid

    attempt = _load_attempt(db, user, attempt_id)  # 본인 시험만(타인 것은 404)
    if not is_valid(attempt):
        raise HTTPException(status_code=403, detail="합격한 시험만 합격증을 받을 수 있습니다")
    try:
        return certificate_response(db, attempt, user, request.app.state.settings, user.id)
    except CertificateError as e:
        raise HTTPException(status_code=403, detail=str(e))
