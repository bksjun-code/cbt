"""관리자: 응시 결과 조회와 CSV/Excel 내보내기.

내보내기 보안
  - 수식 인젝션 방지: 셀 값이 = + - @ TAB CR 로 시작하는 문자열이면 앞에 ' 를 붙인다
    (CSV 뿐 아니라 openpyxl 도 '=' 로 시작하는 문자열을 수식으로 저장하므로 xlsx 에도 동일하게 적용)
  - CSV 는 Excel 한글 호환을 위해 UTF-8 BOM
  - 내보내기 자체를 audit_logs 에 기록한다(개인정보 반출 추적)
"""
import csv
import io
from dataclasses import dataclass
from datetime import date, timedelta

from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AttemptStatus, Blueprint, Domain, ExamAttempt, Product, User
from app.services.timeutil import fmt_kst, kst_day_start_utc

PER_PAGE = 30
EXPORT_LIMIT = 50_000
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(v):
    """스프레드시트 수식으로 해석될 수 있는 문자열을 무력화한다."""
    if isinstance(v, str) and v.startswith(FORMULA_PREFIXES):
        return "'" + v
    return v


@dataclass
class Filters:
    product: str = ""       # 제품 코드
    status: str = ""
    passed: str = ""        # "", "1"(합격), "0"(불합격)
    q: str = ""             # login_id / 이름 부분 일치
    date_from: date | None = None  # KST 기준 시작일(포함)
    date_to: date | None = None    # KST 기준 종료일(포함)

    def as_query(self) -> str:
        parts = {"product": self.product, "status": self.status, "passed": self.passed, "q": self.q,
                 "date_from": self.date_from.isoformat() if self.date_from else "",
                 "date_to": self.date_to.isoformat() if self.date_to else ""}
        from urllib.parse import urlencode

        return urlencode({k: v for k, v in parts.items() if v})


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _stmt(f: Filters):
    stmt = select(ExamAttempt, User, Blueprint.name).join(User, User.id == ExamAttempt.user_id) \
        .join(Blueprint, Blueprint.id == ExamAttempt.blueprint_id)
    if f.product:
        stmt = stmt.join(Product, Product.id == Blueprint.product_id).where(Product.code == f.product)
    if f.status in {s.value for s in AttemptStatus}:
        stmt = stmt.where(ExamAttempt.status == f.status)
    if f.passed in ("0", "1"):
        stmt = stmt.where(ExamAttempt.passed.is_(f.passed == "1"))
    if f.q.strip():
        like = _like(f.q.strip())
        stmt = stmt.where(User.login_id.ilike(like, escape="\\") | User.name.ilike(like, escape="\\"))
    if f.date_from:
        stmt = stmt.where(ExamAttempt.started_at >= kst_day_start_utc(f.date_from))
    if f.date_to:
        stmt = stmt.where(ExamAttempt.started_at < kst_day_start_utc(f.date_to + timedelta(days=1)))
    return stmt


def list_attempts(db: Session, f: Filters, page: int = 1, per_page: int = PER_PAGE):
    stmt = _stmt(f)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(stmt.order_by(ExamAttempt.id.desc()).limit(per_page).offset(max(page - 1, 0) * per_page)).all()
    return rows, total


def summary(db: Session, f: Filters) -> dict:
    """필터 조건의 종료된 응시 요약: 건수, 합격률, 평균 점수."""
    sub = _stmt(f).where(ExamAttempt.status != AttemptStatus.IN_PROGRESS).subquery()
    n, passed, avg = db.execute(
        select(func.count(), func.sum(func.coalesce(sub.c.passed, 0)), func.avg(sub.c.total_score))
    ).one()
    n = n or 0
    return {"n": n, "passed": int(passed or 0), "pass_rate": (int(passed or 0) / n * 100) if n else None,
            "avg": float(avg) if avg is not None else None}


# ---------- 내보내기 ----------
def _domain_codes(db: Session, product: str = "") -> list[str]:
    """내보내기 영역 열. 시험 필터가 있으면 그 시험의 영역만."""
    q = select(Domain.code).order_by(Domain.id)
    if product:
        q = q.join(Product, Product.id == Domain.product_id).where(Product.code == product)
    return list(db.scalars(q))


STATUS_KO = {"IN_PROGRESS": "진행 중", "SUBMITTED": "제출", "EXPIRED": "시간 만료"}


def export_table(db: Session, f: Filters) -> tuple[list[str], list[list]]:
    codes = _domain_codes(db, f.product)
    header = ["회차", "로그인 ID", "이름", "시험", "Blueprint", "시작(KST)", "제출(KST)", "소요(분)", "상태", "총점", "합격"] + \
             [f"{c} 득점률(%)" for c in codes]
    rows = []
    for a, u, bp_name in db.execute(_stmt(f).order_by(ExamAttempt.id).limit(EXPORT_LIMIT)):
        mins = round((a.submitted_at - a.started_at).total_seconds() / 60, 1) if a.submitted_at else ""
        scores = a.domain_scores or {}
        ratios = [round(scores[c]["ratio"] * 100, 1) if c in scores else "" for c in codes]
        passed = "" if a.passed is None else ("합격" if a.passed else "불합격")
        rows.append([a.id, u.login_id, u.name, a.blueprint.product.name, bp_name, fmt_kst(a.started_at, "%Y-%m-%d %H:%M:%S"),
                     fmt_kst(a.submitted_at, "%Y-%m-%d %H:%M:%S") if a.submitted_at else "", mins,
                     STATUS_KO[a.status.value], a.total_score if a.total_score is not None else "", passed, *ratios])
    return header, rows


def to_csv(header: list[str], rows: list[list]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([safe_cell(h) for h in header])
    for r in rows:
        w.writerow([safe_cell(v) for v in r])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def to_xlsx(header: list[str], rows: list[list], sheet: str = "응시 결과") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append([safe_cell(h) for h in header])
    for r in rows:
        ws.append([safe_cell(v) for v in r])
    for col in ws.columns:  # 열 너비 자동 조정(상한 40)
        width = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 8), 40)
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
