from pathlib import Path

from fastapi import Request
import json

from fastapi.templating import Jinja2Templates

from app.services.timeutil import fmt_kst
from app.web.deps import csrf_token

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.filters["kst"] = fmt_kst  # DB(UTC) → 화면(KST)
# 사람이 읽는 JSON. 문자열 그대로 반환하므로 Jinja 자동 이스케이프가 적용된다
templates.env.filters["pretty_json"] = lambda v: json.dumps(v, ensure_ascii=False, indent=2, default=str)


def render(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("csrf_token", csrf_token(request))
    ctx.setdefault("flash", request.session.pop("flash", None))  # 1회성 알림 {"kind", "text"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["flash"] = {"kind": kind, "text": text}
