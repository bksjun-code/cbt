"""관리자: 문항 분석(난이도 지수, 변별도, 보기별 선택 분포)."""
import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Domain, User, utcnow
from app.services import item_analysis as svc
from app.services.audit import log_audit
from app.services.results_admin import safe_cell
from app.services.timeutil import fmt_kst
from app.web.deps import require_admin
from app.web.templating import render

router = APIRouter(prefix="/admin/analytics", dependencies=[Depends(require_admin)])
PER_PAGE = 30
SORTS = {"d", "p", "n", "id"}


def _filters(domain: str, difficulty: str, status: str, warn: str, sort: str) -> svc.Filters:
    return svc.Filters(domain=domain, difficulty=difficulty, status=status, warn_only=warn == "1",
                       sort=sort if sort in SORTS else "d")


@router.get("")
def analytics(request: Request, domain: str = "", difficulty: str = "", status: str = "", warn: str = "",
              sort: str = "d", page: int = 1, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    f = _filters(domain, difficulty, status, warn, sort)
    stats = svc.list_stats(db, f)
    summ = svc.summary(stats)
    pages = max((len(stats) + PER_PAGE - 1) // PER_PAGE, 1)
    page = min(max(page, 1), pages)
    return render(request, "admin_analytics.html", user=user, rows=stats[(page - 1) * PER_PAGE: page * PER_PAGE],
                  total=len(stats), summary=summ, page=page, pages=pages, f=f, qs=f.as_query(),
                  domains=list(db.scalars(select(Domain).order_by(Domain.id))), min_sample=svc.MIN_SAMPLE,
                  d_warn=svc.D_WARN)


@router.get("/export.csv")
def export_csv(domain: str = "", difficulty: str = "", status: str = "", warn: str = "", sort: str = "d",
               user: User = Depends(require_admin), db: Session = Depends(get_db)):
    f = _filters(domain, difficulty, status, warn, sort)
    stats = svc.list_stats(db, f)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["문항 ID", "영역", "난이도", "유형", "상태", "variant group", "응시 수", "정답 수", "무응답", "정답률(p)",
                "상위27% 정답률", "하위27% 정답률", "변별도(D)", "표본 충분", "변별도 경고", "채점 보정", "지문"])
    for s in stats:
        w.writerow([safe_cell(v) for v in [
            s.item.id, s.item.domain.code, s.item.difficulty.value, s.item.item_type.value, s.item.status.value,
            s.item.variant_group, s.n, s.correct, s.blank, round(s.p, 4), round(s.p_upper, 4), round(s.p_lower, 4),
            s.d, "Y" if s.enough else "N", "Y" if s.warn else "", s.override.value if s.override else "", s.item.stem[:80]]])
    log_audit(db, user.id, "ANALYSIS_EXPORT", "csv", {"rows": len(stats), "filters": f.as_query()})
    db.commit()
    return Response(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="item_analysis_{fmt_kst(utcnow(), "%Y%m%d_%H%M")}.csv"'})


@router.get("/items/{item_id:int}")
def item_detail(item_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    stat = svc.analyze(db, item_ids=[item_id]).get(item_id)
    if stat is None:
        raise HTTPException(status_code=404, detail="분석할 응시 데이터가 없는 문항입니다")
    return render(request, "admin_analytics_item.html", user=user, s=stat, min_sample=svc.MIN_SAMPLE,
                  d_warn=svc.D_WARN, low_pick=svc.LOW_PICK)
