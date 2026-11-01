"""관리자: 문항 목록/등록/수정, 검수 워크플로, 일괄 업로드."""
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Architecture, Difficulty, Domain, Item, ItemStatus, ItemType, OverrideMode, Product, User
from app.schemas.item import OPTION_KEYS, ItemIn
from app.services import grade_overrides as ovr
from app.services import item_admin as svc
from app.services.item_import import MAX_BYTES, ImportFormatError, import_items
from app.web.deps import require_admin
from app.web.templating import flash, render

router = APIRouter(prefix="/admin/items", dependencies=[Depends(require_admin)])

ACTIONS = {"submit-review": "submit_review", "approve": "approve", "reject": "reject", "retire": "retire"}


def _get_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="문항을 찾을 수 없습니다")
    return item


def _form_defaults(item: Item | None = None) -> dict:
    if item is None:
        return {"domain": "", "difficulty": "MID", "item_type": "SINGLE", "architecture": "BOTH", "product_version": "",
                "stem": "", "options": {}, "correct_keys": [], "explanation": "", "reference": "", "variant_group": ""}
    return {"domain": item.domain.code, "difficulty": item.difficulty.value, "item_type": item.item_type.value,
            "architecture": item.architecture.value, "product_version": item.product_version, "stem": item.stem,
            "options": dict(item.options), "correct_keys": list(item.correct_keys), "explanation": item.explanation,
            "reference": item.reference or "", "variant_group": item.variant_group}


def _form_from_request(form) -> dict:
    options = {k: (form.get(f"option_{k}") or "").strip() for k in OPTION_KEYS}
    return {
        "domain": form.get("domain", ""), "difficulty": form.get("difficulty", ""), "item_type": form.get("item_type", ""),
        "architecture": form.get("architecture", "BOTH"), "product_version": (form.get("product_version") or "").strip(),
        "stem": (form.get("stem") or "").strip(), "options": {k: v for k, v in options.items() if v},
        "correct_keys": [k for k in form.getlist("correct_keys") if k in OPTION_KEYS],
        "explanation": (form.get("explanation") or "").strip(), "reference": (form.get("reference") or "").strip() or None,
        "variant_group": (form.get("variant_group") or "").strip(),
    }


def _catalog(db: Session) -> dict:
    """제품별 영역 선택지(필터·등록 폼·업로드 안내 공용)."""
    return {"domains": list(db.scalars(select(Domain).order_by(Domain.id))),
            "products": list(db.scalars(select(Product).order_by(Product.sort_order, Product.id)))}


def _form_ctx(db: Session, user: User, **extra) -> dict:
    return {
        "user": user, **_catalog(db),
        "difficulties": [d.value for d in Difficulty], "types": [t.value for t in ItemType],
        "architectures": [a.value for a in Architecture], "keys": OPTION_KEYS,
        "groups": svc.variant_groups(db), **extra,
    }


def _errors(e: ValidationError) -> list[str]:
    return [f"{'.'.join(str(x) for x in err['loc']) or '문항'}: {err['msg']}" for err in e.errors()]


@router.get("")
def item_list(request: Request, product: str = "", domain: str = "", difficulty: str = "", status: str = "", item_type: str = "",
              q: str = "", page: int = 1, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    page = min(max(page, 1), 100_000)  # 비정상적으로 큰 값은 OFFSET 오버플로 방지를 위해 제한
    items, total = svc.list_items(db, product_code=product or None, domain_code=domain or None, difficulty=difficulty or None, status=status or None,
                                  item_type=item_type or None, q=q or None, page=page)
    pages = max((total + svc.PER_PAGE - 1) // svc.PER_PAGE, 1)
    filters = {"product": product, "domain": domain, "difficulty": difficulty, "status": status, "item_type": item_type, "q": q}
    return render(request, "admin_items.html", **_form_ctx(db, user), items=items, total=total, page=page, pages=pages,
                  filters=filters, statuses=[s.value for s in ItemStatus], counts=svc.status_counts(db),
                  qs="&".join(f"{k}={v}" for k, v in filters.items() if v and k != "q") + (f"&q={q}" if q else ""))


@router.get("/new")
def new_form(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin_item_form.html", **_form_ctx(db, user), item=None, form=_form_defaults(),
                  errors=[], editable=True, history=[], has_history=False, lineage=None)


@router.post("/new")
async def create(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    form = _form_from_request(await request.form())
    try:
        item = svc.create_item(db, ItemIn.model_validate(form), user)
        db.commit()
    except ValidationError as e:
        return render(request, "admin_item_form.html", status_code=422, **_form_ctx(db, user), item=None, form=form,
                      errors=_errors(e), editable=True, history=[], has_history=False, lineage=None)
    except svc.DuplicateItem as e:
        db.rollback()
        return render(request, "admin_item_form.html", status_code=409, **_form_ctx(db, user), item=None, form=form,
                      errors=[str(e)], editable=True, history=[], has_history=False, lineage=None)
    flash(request, "문항을 DRAFT 로 등록했습니다.")
    return RedirectResponse(f"/admin/items/{item.id}", status_code=303)


@router.get("/upload")
def upload_form(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    return render(request, "admin_upload.html", user=user, result=None, error=None, max_mb=MAX_BYTES // 1024 // 1024,
                  **_catalog(db))


@router.post("/upload")
async def upload(request: Request, file: UploadFile, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    raw = await file.read(MAX_BYTES + 1)  # 상한 초과분은 읽지 않는다
    try:
        result = import_items(db, file.filename or "", raw, user)
    except ImportFormatError as e:
        return render(request, "admin_upload.html", status_code=400, user=user, result=None, error=str(e),
                      max_mb=MAX_BYTES // 1024 // 1024, **_catalog(db))
    if result.ok:
        flash(request, f"{result.created}개 문항을 DRAFT 로 등록했습니다.")
        return RedirectResponse("/admin/items?status=DRAFT", status_code=303)
    return render(request, "admin_upload.html", status_code=422, user=user, result=result, error=None,
                  max_mb=MAX_BYTES // 1024 // 1024, **_catalog(db))


def _detail_ctx(db: Session, user: User, item: Item, **extra) -> dict:
    return {**_form_ctx(db, user), "item": item, "history": svc.review_history(db, item.id),
            "has_history": svc.has_history(db, item.id), "editable": svc.is_editable(db, item),
            "lineage": svc.lineage(db, item), "override": ovr.get_override(db, item.id),
            "override_modes": [m.value for m in OverrideMode], **extra}


@router.get("/{item_id:int}")
def detail(item_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = _get_item(db, item_id)
    return render(request, "admin_item_form.html", **_detail_ctx(db, user, item, form=_form_defaults(item), errors=[]))


@router.post("/{item_id:int}")
async def update(item_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = _get_item(db, item_id)
    form = _form_from_request(await request.form())
    try:
        before = item.status
        svc.update_item(db, item, ItemIn.model_validate(form), user)
        db.commit()
    except ValidationError as e:
        return render(request, "admin_item_form.html", status_code=422, **_detail_ctx(db, user, item, form=form, errors=_errors(e)))
    except svc.DuplicateItem as e:
        db.rollback()
        return render(request, "admin_item_form.html", status_code=409, **_detail_ctx(db, user, item, form=form, errors=[str(e)]))
    except svc.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
        return RedirectResponse(f"/admin/items/{item_id}", status_code=303)
    flash(request, "저장했습니다." + (" 재검수가 필요하여 DRAFT 로 되돌렸습니다." if before != item.status else ""))
    return RedirectResponse(f"/admin/items/{item_id}", status_code=303)


def _override_flash(request: Request, res: ovr.OverrideResult, verb: str) -> None:
    flash(request, f"{verb}: 재채점 {res.regraded}건 · 점수 변경 {res.score_changed}건 · 합불 변경 {res.flipped}건")


@router.post("/{item_id:int}/override")
async def set_override(item_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = _get_item(db, item_id)
    form = await request.form()
    try:
        mode = OverrideMode(form.get("mode") or "")
        res = ovr.set_override(db, item, mode, form.get("reason") or "", user)
        db.commit()
        _override_flash(request, res, "채점 보정을 적용했습니다")
    except ValueError:
        db.rollback()
        flash(request, "보정 유형이 올바르지 않습니다", "error")
    except ovr.OverrideError as e:
        db.rollback()
        flash(request, str(e), "error")
    return RedirectResponse(f"/admin/items/{item_id}", status_code=303)


@router.post("/{item_id:int}/override/revoke")
async def revoke_override(item_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    item = _get_item(db, item_id)
    form = await request.form()
    try:
        res = ovr.revoke_override(db, item, form.get("reason") or "", user)
        db.commit()
        _override_flash(request, res, "채점 보정을 해제했습니다")
    except ovr.OverrideError as e:
        db.rollback()
        flash(request, str(e), "error")
    return RedirectResponse(f"/admin/items/{item_id}", status_code=303)


@router.post("/{item_id:int}/{action}")
async def workflow(item_id: int, action: str, request: Request, user: User = Depends(require_admin),
                   db: Session = Depends(get_db)):
    item = _get_item(db, item_id)
    form = await request.form()
    try:
        if action == "new-version":
            new = svc.new_version(db, item, user)
            db.commit()
            flash(request, f"신규 버전(v{new.version})을 DRAFT 로 만들었습니다. 이전 버전은 RETIRED 처리되었습니다.")
            return RedirectResponse(f"/admin/items/{new.id}", status_code=303)
        if action not in ACTIONS:
            raise HTTPException(status_code=404, detail="알 수 없는 작업입니다")
        svc.transition(db, item, ACTIONS[action], user, reason=form.get("reason"))
        db.commit()
        flash(request, f"처리했습니다: {item.status.value}")
    except svc.WorkflowError as e:
        db.rollback()
        flash(request, str(e), "error")
    return RedirectResponse(f"/admin/items/{item_id}", status_code=303)
