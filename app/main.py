"""FastAPI 앱 팩토리. 실행: uvicorn app.main:create_app --factory"""
import logging
import mimetypes
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from app.config import DEV_SECRET, Settings, get_settings
from app.routers import (
    account, admin, certificate, admin_analytics, admin_blueprints, admin_items, admin_results, admin_system, admin_users, auth, exam,
)
from app.web.deps import LoginRequired, csrf_protect, is_htmx
from app.web.ratelimit import RateLimiter
from app.web.templating import render

# Windows 레지스트리 설정에 따라 .js 가 text/plain 으로 서빙되면 nosniff 때문에 스크립트가 차단된다
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

log = logging.getLogger("cbt")
BASE = Path(__file__).resolve().parent

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    from app.db import SessionLocal
    from app.services.exam import expire_overdue

    def sweep():
        with SessionLocal() as db:
            n = expire_overdue(db)
            if n:
                log.info("만료 시험 자동 제출 %d건", n)

    sched = BackgroundScheduler(timezone="UTC")
    sched.add_job(sweep, "interval", seconds=60, id="expire_overdue", max_instances=1, coalesce=True)
    sched.start()
    return sched


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    if settings.cookie_secure and settings.secret_key.get_secret_value() == DEV_SECRET:
        raise RuntimeError("운영 모드(COOKIE_SECURE=true)에서는 SECRET_KEY 를 반드시 설정해야 합니다")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        sched = _start_scheduler() if settings.scheduler_enabled else None
        try:
            yield
        finally:
            if sched:
                sched.shutdown(wait=False)

    app = FastAPI(title="OGG-CBT", lifespan=lifespan, dependencies=[Depends(csrf_protect)],
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.account_limiter = RateLimiter(limit=5, window_sec=600)  # 가입 신청·재설정 요청: IP 당 10분에 5회
    app.state.verify_limiter = RateLimiter(limit=30, window_sec=600)  # 합격증 진위 확인: 번호 대입 방지

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/static"):
            resp.headers["Cache-Control"] = "no-cache"  # ETag 로 매번 재검증(배포 후 낡은 JS/CSS 방지)
        else:
            resp.headers.setdefault("Cache-Control", "no-store")  # 문항/결과 캐시 방지
        return resp

    # 세션 쿠키: HttpOnly(기본), SameSite=Lax, Secure(운영)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.secret_key.get_secret_value(), session_cookie="cbt_session",
        max_age=settings.session_max_age_sec, same_site="lax", https_only=settings.cookie_secure,
    )

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        if is_htmx(request):
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException):
        if request.url.path.startswith("/static") or is_htmx(request):
            return Response(content=str(exc.detail), status_code=exc.status_code, media_type="text/plain")
        user = None
        try:
            if request.session.get("uid"):
                user = True
        except AssertionError:
            pass
        return render(request, "error.html", status_code=exc.status_code, title=f"오류 {exc.status_code}",
                      message=str(exc.detail), user=user)

    @app.exception_handler(OverflowError)
    async def _overflow(request: Request, _exc: OverflowError):
        # DB 정수 범위를 넘는 ID/번호(예: /exam/99999999999999999999)는 존재할 수 없는 값이다
        return await _http_error(request, HTTPException(status_code=404, detail="요청한 항목을 찾을 수 없습니다"))

    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(account.router)
    app.include_router(certificate.router)
    app.include_router(exam.router)
    app.include_router(admin.router)
    for r in (admin_items, admin_blueprints, admin_results, admin_analytics, admin_users, admin_system):
        app.include_router(r.router)
    return app
