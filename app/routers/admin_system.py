"""관리자: 시스템 설정과 감사 로그 조회."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.services import audit_admin
from app.services.app_settings import get_setting, set_setting
from app.services.audit import log_audit
from app.services.timeutil import parse_date
from app.web.deps import require_admin
from app.web.templating import flash, render

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

# 키 → (표시 이름, 설명, 기본값)
SETTINGS = {
    "show_explanations": ("오답 해설 공개", "제출 후 결과 화면에서 응시자에게 오답 문항의 정답과 해설을 보여 줍니다.", True),
    "require_separate_approver": ("작성자·승인자 분리", "켜면 문항을 만든 관리자가 본인 문항을 승인할 수 없습니다.", False),
}


@router.get("/settings")
def settings_page(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    values = {k: bool(get_setting(db, k, d)) for k, (_, _, d) in SETTINGS.items()}
    return render(request, "admin_settings.html", user=user, items=SETTINGS, values=values)


@router.post("/settings")
async def save_settings(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = await request.form()
    before = {k: bool(get_setting(db, k, d)) for k, (_, _, d) in SETTINGS.items()}
    after = {k: form.get(k) == "on" for k in SETTINGS}  # 체크박스: 체크 해제 시 필드가 전송되지 않음
    for k, v in after.items():
        set_setting(db, k, v)
    changed = {k: [before[k], after[k]] for k in SETTINGS if before[k] != after[k]}
    if changed:
        log_audit(db, user.id, "SETTINGS_UPDATE", "app_settings", {"changed": changed})
    db.commit()
    flash(request, "설정을 저장했습니다." if changed else "변경된 내용이 없습니다.")
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/audit")
def audit_page(request: Request, action: str = "", q: str = "", date_from: str = "", date_to: str = "", page: int = 1,
               user: User = Depends(require_admin), db: Session = Depends(get_db)):
    page = min(max(page, 1), 100_000)  # 비정상적으로 큰 값은 OFFSET 오버플로 방지를 위해 제한
    df, dt = parse_date(date_from), parse_date(date_to)
    rows, total = audit_admin.list_audit(db, action, q[:100], df, dt, page)
    pages = max((total + audit_admin.PER_PAGE - 1) // audit_admin.PER_PAGE, 1)
    from urllib.parse import urlencode

    qs = urlencode({k: v for k, v in {"action": action, "q": q, "date_from": date_from if df else "",
                                        "date_to": date_to if dt else ""}.items() if v})
    return render(request, "admin_audit.html", user=user, rows=rows, total=total, page=page, pages=pages,
                  actions=audit_admin.distinct_actions(db), action=action, q=q,
                  date_from=date_from if df else "", date_to=date_to if dt else "", qs=qs)
