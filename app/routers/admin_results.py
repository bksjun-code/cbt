"""관리자: 응시 결과 목록/상세와 CSV·Excel 내보내기."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Blueprint, Domain, ExamAttempt, User, utcnow
from app.services import results_admin as svc
from app.services.audit import log_audit
from app.services.grading import load_overrides
from app.services.timeutil import parse_date, fmt_kst
from app.web.deps import certificate_response, require_admin
from app.web.templating import render

router = APIRouter(prefix="/admin/results", dependencies=[Depends(require_admin)])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _filters(status: str = "", passed: str = "", q: str = "", date_from: str = "", date_to: str = "") -> svc.Filters:
    return svc.Filters(status=status, passed=passed, q=q[:100], date_from=parse_date(date_from), date_to=parse_date(date_to))


@router.get("")
def result_list(request: Request, status: str = "", passed: str = "", q: str = "", date_from: str = "",
                date_to: str = "", page: int = 1, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    f = _filters(status, passed, q, date_from, date_to)
    page = min(max(page, 1), 100_000)  # 비정상적으로 큰 값은 OFFSET 오버플로 방지를 위해 제한
    rows, total = svc.list_attempts(db, f, page)
    pages = max((total + svc.PER_PAGE - 1) // svc.PER_PAGE, 1)
    return render(request, "admin_results.html", user=user, rows=rows, total=total, page=page, pages=pages, f=f,
                  qs=f.as_query(), summary=svc.summary(db, f),
                  raw={"date_from": date_from if f.date_from else "", "date_to": date_to if f.date_to else ""})


def _export(db: Session, user: User, fmt: str, f: svc.Filters) -> Response:
    header, rows = svc.export_table(db, f)
    log_audit(db, user.id, "RESULT_EXPORT", fmt, {"rows": len(rows), "filters": f.as_query()})
    db.commit()
    stamp = fmt_kst(utcnow(), "%Y%m%d_%H%M")
    if fmt == "csv":
        body, ctype = svc.to_csv(header, rows), "text/csv; charset=utf-8"
    else:
        body, ctype = svc.to_xlsx(header, rows), XLSX
    return Response(body, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="results_{stamp}.{fmt}"'})


@router.get("/export.csv")
def export_csv(status: str = "", passed: str = "", q: str = "", date_from: str = "", date_to: str = "",
               user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return _export(db, user, "csv", _filters(status, passed, q, date_from, date_to))


@router.get("/export.xlsx")
def export_xlsx(status: str = "", passed: str = "", q: str = "", date_from: str = "", date_to: str = "",
                user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return _export(db, user, "xlsx", _filters(status, passed, q, date_from, date_to))


@router.get("/{attempt_id:int}")
def result_detail(attempt_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    a = db.get(ExamAttempt, attempt_id)
    if a is None:
        raise HTTPException(status_code=404, detail="응시 결과를 찾을 수 없습니다")
    cand = db.get(User, a.user_id)
    bp = db.get(Blueprint, a.blueprint_id)
    overrides = load_overrides(db, [i.item_id for i in a.items])
    rows = []
    for ai in a.items:
        opts = ai.item.options
        rows.append({
            "ai": ai, "domain": ai.item.domain.code, "difficulty": ai.item.difficulty.value,
            "selected": [(k, opts.get(k, "")) for k in (ai.selected_keys or [])],
            "correct": [(k, opts.get(k, "")) for k in ai.item.correct_keys],
            "override": overrides.get(ai.item_id),
        })
    order = {d.code: (i, d.name) for i, d in enumerate(db.scalars(select(Domain).order_by(Domain.id)))}
    doms = sorted(((c, order.get(c, (99, c))[1], v) for c, v in (a.domain_scores or {}).items()),
                  key=lambda t: order.get(t[0], (99,))[0])
    return render(request, "admin_result_detail.html", user=user, a=a, cand=cand, bp=bp, rows=rows, domains=doms)


@router.get("/{attempt_id:int}/certificate.pdf")
def certificate(attempt_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    from app.services.certificates import is_valid

    a = db.get(ExamAttempt, attempt_id)
    if a is None:
        raise HTTPException(status_code=404, detail="응시 결과를 찾을 수 없습니다")
    if not is_valid(a):
        raise HTTPException(status_code=403, detail="합격한 시험만 합격증을 받을 수 있습니다")
    return certificate_response(db, a, db.get(User, a.user_id), request.app.state.settings, user.id)
