"""관리자: Blueprint 편집과 셀별 가용 문항 대시보드."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Blueprint, Domain, Product, User
from app.services import blueprint_admin as svc
from app.services.blueprint_validation import BlueprintError
from app.web.deps import require_admin
from app.web.templating import flash, render

router = APIRouter(prefix="/admin/blueprints", dependencies=[Depends(require_admin)])


def _products(db: Session) -> list[Product]:
    return list(db.scalars(select(Product).order_by(Product.sort_order, Product.id)))


def _product(db: Session, code: str | None) -> Product:
    """코드로 제품을 찾는다. 없거나 잘못된 코드면 첫 제품."""
    products = _products(db)
    return next((p for p in products if p.code == code), products[0])


def _domains(db: Session, product_id: int) -> list[Domain]:
    return list(db.scalars(select(Domain).where(Domain.product_id == product_id).order_by(Domain.id)))


def _get(db: Session, bp_id: int) -> Blueprint:
    bp = db.get(Blueprint, bp_id)
    if bp is None:
        raise HTTPException(status_code=404, detail="Blueprint 를 찾을 수 없습니다")
    return bp


def _form_ctx(db: Session, user: User, form: svc.BlueprintForm, domains: list[Domain], product: Product, **extra) -> dict:
    return {"user": user, "form": form, "domains": domains, "product": product, "products": _products(db),
            "diffs": [d.value for d in svc.DIFFS],
            "matrix": svc.matrix(db, form, domains), "shortages": svc.shortages(db, form, domains), **extra}


@router.get("")
def blueprint_list(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    rows = []
    for bp in svc.list_blueprints(db):
        domains = _domains(db, bp.product_id)
        rows.append({"bp": bp, "short": svc.shortages(db, svc.form_from_blueprint(bp, domains), domains)})
    return render(request, "admin_blueprints.html", user=user, rows=rows, products=_products(db))


@router.get("/new")
def new_form(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    product = _product(db, request.query_params.get("product"))
    domains = _domains(db, product.id)
    base = db.scalar(select(Blueprint).where(Blueprint.is_active.is_(True), Blueprint.product_id == product.id))
    form = svc.form_from_blueprint(base, domains) if base else svc.BlueprintForm()
    form.name = ""  # 같은 제품의 활성 Blueprint 를 복사해 시작(이름은 새로 입력)
    return render(request, "admin_blueprint_form.html", **_form_ctx(db, user, form, domains, product), bp=None, errors=[])


@router.post("/new")
async def create(request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    data = await request.form()
    product = _product(db, data.get("product"))
    domains = _domains(db, product.id)
    form, errors = svc.parse_form(data, domains)
    if not errors:
        try:
            bp = svc.create_blueprint(db, form, user, domains, product)
            db.commit()
            flash(request, "Blueprint 를 만들었습니다(비활성). 가용 문항을 확인한 뒤 활성화하세요.")
            return RedirectResponse(f"/admin/blueprints/{bp.id}", status_code=303)
        except BlueprintError as e:
            db.rollback()
            errors = [str(e)]
    return render(request, "admin_blueprint_form.html", status_code=422, **_form_ctx(db, user, form, domains, product),
                  bp=None, errors=errors)


@router.get("/{bp_id:int}")
def detail(bp_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    bp = _get(db, bp_id)
    domains = _domains(db, bp.product_id)
    return render(request, "admin_blueprint_form.html",
                  **_form_ctx(db, user, svc.form_from_blueprint(bp, domains), domains, bp.product), bp=bp, errors=[])


@router.post("/{bp_id:int}")
async def update(bp_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    bp = _get(db, bp_id)
    domains = _domains(db, bp.product_id)
    form, errors = svc.parse_form(await request.form(), domains)
    if not errors:
        try:
            svc.update_blueprint(db, bp, form, user, domains)
            db.commit()
            flash(request, "저장했습니다." + (" (활성 Blueprint 이므로 이후 시작하는 시험부터 적용됩니다)" if bp.is_active else ""))
            return RedirectResponse(f"/admin/blueprints/{bp_id}", status_code=303)
        except BlueprintError as e:
            db.rollback()
            errors = [str(e)]
    return render(request, "admin_blueprint_form.html", status_code=422, **_form_ctx(db, user, form, domains, bp.product),
                  bp=bp, errors=errors)


@router.post("/{bp_id:int}/activate")
def activate(bp_id: int, request: Request, user: User = Depends(require_admin), db: Session = Depends(get_db)):
    bp = _get(db, bp_id)
    try:
        svc.activate(db, bp, user, _domains(db, bp.product_id))
        db.commit()
        flash(request, f"'{bp.name}' 을(를) 활성화했습니다({bp.product.name} 시험). 이후 시작하는 시험부터 적용됩니다.")
    except BlueprintError as e:
        db.rollback()
        flash(request, str(e), "error")
    return RedirectResponse(f"/admin/blueprints/{bp_id}", status_code=303)
