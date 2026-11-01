"""합격증: 인증번호 발급, 유효성 판정, PDF 생성.

  - 인증번호는 처음 내려받을 때 발급해 응시 기록에 저장한다(RP-{제품코드}-YYYYMMDD-XXXXXX, 뒤 6자리는 무작위).
  - 자격명은 시험 제품에서 가져온다("{제품명} 사내 자격시험"). 제품이 달라도 양식은 같다.
  - 유효성은 발급 시점이 아니라 '지금' 판정한다. 채점 보정으로 불합격이 되거나 계정·기록이 삭제되면 무효가 된다.
  - 사내 자격 인증임을 명시하고, Oracle 의 로고·서명·문구는 쓰지 않는다(공식 자격증과의 혼동 방지).
  - 한글 글꼴은 나눔 글꼴(OFL)을 함께 배포한다(app/assets/fonts).
"""
import io
import math
import random
import secrets
from datetime import datetime
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AttemptStatus, ExamAttempt, User
from app.services.audit import log_audit
from app.services.timeutil import to_kst

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
PREFIX = "RP-"  # 뒤에 제품 코드가 붙는다(예: RP-OGG, RP-ORADB)
RED, GRAY = HexColor("#c74634"), HexColor("#6b7280")
FRAME_COLORS = [HexColor(x) for x in ("#e07b39", "#c9622b", "#2f5d62", "#24484c", "#8b2f25", "#a33a2c", "#d9c7a8", "#ef9a4b")]
_fonts_ready = False


class CertificateError(Exception):
    pass


def _register_fonts() -> None:
    global _fonts_ready
    if _fonts_ready:
        return
    pdfmetrics.registerFont(TTFont("Gothic", str(FONT_DIR / "NanumGothic-Regular.ttf")))
    pdfmetrics.registerFont(TTFont("GothicBold", str(FONT_DIR / "NanumGothic-Bold.ttf")))
    pdfmetrics.registerFont(TTFont("MyeongjoBold", str(FONT_DIR / "NanumMyeongjo-Bold.ttf")))
    _fonts_ready = True


def is_valid(attempt: ExamAttempt | None) -> bool:
    return attempt is not None and attempt.status != AttemptStatus.IN_PROGRESS and attempt.passed is True


def passed_on(attempt: ExamAttempt) -> datetime:
    return to_kst(attempt.submitted_at or attempt.started_at)


def ensure_number(db: Session, attempt: ExamAttempt, actor_id: int | None) -> str:
    """합격한 시험의 인증번호를 돌려준다(없으면 발급). 불합격·진행 중이면 CertificateError."""
    if not is_valid(attempt):
        raise CertificateError("합격한 시험만 합격증을 받을 수 있습니다")
    if attempt.certificate_no:
        return attempt.certificate_no
    day = passed_on(attempt).strftime("%Y%m%d")
    while True:
        number = f"{PREFIX}{attempt.blueprint.product.code}-{day}-{secrets.token_hex(3).upper()}"
        if db.scalar(select(ExamAttempt.id).where(ExamAttempt.certificate_no == number)) is None:
            break
    attempt.certificate_no = number
    log_audit(db, actor_id, "CERTIFICATE_ISSUED", f"attempt:{attempt.id}", {"certificate_no": number})
    db.commit()
    return number


def find(db: Session, number: str) -> ExamAttempt | None:
    number = (number or "").strip().upper()
    if not number.startswith(PREFIX) or len(number) > 40:
        return None
    return db.scalar(select(ExamAttempt).where(ExamAttempt.certificate_no == number))


def verify_url(settings: Settings, number: str) -> str | None:
    return f"{settings.public_base_url.rstrip('/')}/verify/{number}" if settings.public_base_url else None


def _clip_round_rect(c: canvas.Canvas, x: float, y: float, w: float, h: float, r: float) -> None:
    p = c.beginPath()
    p.roundRect(x, y, w, h, r)
    c.clipPath(p, stroke=0, fill=0)


def _strokes(c: canvas.Canvas, rnd: random.Random, box: tuple, n: int, colors: list, width: float) -> None:
    """붓으로 그은 듯한 짧은 빗금 묶음(무늬)."""
    x0, y0, x1, y1 = box
    c.setLineWidth(width)
    c.setLineCap(1)
    for _ in range(n):
        cx, cy = rnd.uniform(x0, x1), rnd.uniform(y0, y1)
        ang = rnd.uniform(0, math.pi)
        dx, dy = math.cos(ang), math.sin(ang)
        nx, ny = -dy, dx
        length, gap = rnd.uniform(10, 26), rnd.uniform(2.2, 3.2)
        c.setStrokeColor(rnd.choice(colors))
        for k in range(rnd.randint(3, 6)):
            ox, oy = cx + nx * gap * k, cy + ny * gap * k
            c.line(ox - dx * length / 2, oy - dy * length / 2, ox + dx * length / 2, oy + dy * length / 2)


def _frame(c: canvas.Canvas, x: float, y: float, w: float, h: float, seed: int) -> None:
    """바깥 테두리: 주황·청록·적갈색 면을 섞은 추상 무늬."""
    rnd = random.Random(seed)
    c.saveState()
    _clip_round_rect(c, x, y, w, h, 22)
    c.setFillColor(HexColor("#e9dcc6"))
    c.rect(x, y, w, h, stroke=0, fill=1)
    step = 46
    for gx in range(int(x) - step, int(x + w) + step, step):
        for gy in range(int(y) - step, int(y + h) + step, step):
            c.setFillColor(rnd.choice(FRAME_COLORS))
            p = c.beginPath()
            p.moveTo(gx + rnd.uniform(-18, 18), gy + rnd.uniform(-18, 18))
            for px, py in ((step, 0), (step, step), (0, step)):
                p.lineTo(gx + px + rnd.uniform(-18, 18), gy + py + rnd.uniform(-18, 18))
            p.close()
            c.drawPath(p, stroke=0, fill=1)
    _strokes(c, rnd, (x, y, x + w, y + h), 260, [HexColor("#f4a261"), HexColor("#1f3b40"), HexColor("#fbe3c4")], 1.1)
    c.setFillColor(HexColor("#1b1b1b"))  # 오른쪽 위 점무늬
    for _ in range(40):
        c.circle(x + w - rnd.uniform(3, 14), rnd.uniform(y + h * 0.35, y + h * 0.8), 0.9, stroke=0, fill=1)
    c.restoreState()


def _panel(c: canvas.Canvas, x: float, y: float, w: float, h: float, seed: int) -> None:
    """안쪽 흰 판: 옅은 빗금 무늬."""
    c.saveState()
    _clip_round_rect(c, x, y, w, h, 16)
    c.setFillColor(HexColor("#ffffff"))
    c.rect(x, y, w, h, stroke=0, fill=1)
    _strokes(c, random.Random(seed + 1), (x, y, x + w, y + h), 520, [HexColor("#efefef"), HexColor("#f3f3f3")], 0.7)
    c.restoreState()


def _badge(c: canvas.Canvas, cx: float, cy: float, issuer: str) -> None:
    """오른쪽 위 메달(은색 테 + 금색 고리 + 리본)."""
    c.setFillColor(HexColor("#7a7a7a"))
    c.rect(cx - 26, cy - 62, 52, 40, stroke=0, fill=1)                    # 리본
    c.setFillColor(HexColor("#5f5f5f"))
    c.rect(cx - 26, cy - 62, 26, 40, stroke=0, fill=1)
    for r, col in ((52, "#8d8d8d"), (49, "#b9b9b9"), (45, "#d9d9d9"), (42, "#d2a65a"), (38, "#e6c27c"), (35, "#ffffff")):
        c.setFillColor(HexColor(col))
        c.circle(cx, cy, r, stroke=0, fill=1)
    c.setFillColor(RED)
    c.setFont("GothicBold", 10)
    c.drawCentredString(cx, cy + 9, issuer.split()[0])
    c.setFillColor(GRAY)
    c.setFont("Gothic", 8)
    c.drawCentredString(cx, cy - 2, "Certified")
    c.setFillColor(HexColor("#222222"))
    c.setFont("GothicBold", 9.5)
    c.drawCentredString(cx, cy - 15, "Professional")


def _qr(c: canvas.Canvas, url: str, x: float, y: float, size: float) -> None:
    qr = QrCodeWidget(url)
    x0, y0, x1, y1 = qr.getBounds()
    drawing = Drawing(size, size, transform=[size / (x1 - x0), 0, 0, size / (y1 - y0), 0, 0])
    drawing.add(qr)
    renderPDF.draw(drawing, c, x, y)


def render_pdf(attempt: ExamAttempt, user: User, settings: Settings) -> bytes:
    if not is_valid(attempt) or not attempt.certificate_no:
        raise CertificateError("유효한 합격 기록이 아닙니다")
    _register_fonts()
    buf = io.BytesIO()
    w, h = landscape(letter)
    c = canvas.Canvas(buf, pagesize=(w, h))
    c.setTitle(f"합격증 {attempt.certificate_no}")
    c.setAuthor(settings.certificate_issuer)
    title = attempt.blueprint.product.certificate_title
    c.setSubject(title)
    seed = int.from_bytes(attempt.certificate_no.encode(), "big") % (2**32)

    c.setFillColor(HexColor("#fafafa"))
    c.rect(0, 0, w, h, stroke=0, fill=1)
    fx, fy, fw, fh = 50, 90, w - 100, h - 135                                # 바깥 무늬 테두리
    _frame(c, fx, fy, fw, fh, seed)
    px, py, pw, ph = fx + 13, fy + 15, fw - 26, fh - 30                      # 안쪽 흰 판
    _panel(c, px, py, pw, ph, seed)

    left = px + 49
    issuer_head, _, issuer_tail = settings.certificate_issuer.partition(" ")
    c.setFillColor(RED)                                                      # 왼쪽 위 발급 기관(로고 자리)
    c.setFont("GothicBold", 19)
    c.drawString(left, py + ph - 52, issuer_head.upper())
    c.setFillColor(HexColor("#555555"))
    c.setFont("Gothic", 11.5)
    c.drawString(left, py + ph - 67, issuer_tail or "Certification")

    c.setFillColor(HexColor("#3a3a3a"))
    c.setFont("MyeongjoBold", 30)
    c.drawString(left, py + ph - 125, "합  격  증")
    c.setFont("Times-Roman", 19)
    c.drawString(left, py + ph - 155, "Certificate of Recognition")

    c.setFillColor(HexColor("#111111"))
    c.setFont("Gothic", 12.5)
    c.drawString(left + 2, py + ph - 202, user.name)
    c.setFont("Gothic", 12)
    c.drawString(left + 2, py + ph - 232, f"{title} 합격")
    c.setFillColor(HexColor("#2b2b2b"))
    c.setFont("Gothic", 12.5)
    c.drawString(left, py + ph - 274, f"위 사람은 {settings.certificate_issuer}이 주관한 {title}에 합격하였음을 증명합니다.")

    d = passed_on(attempt)
    base = py + 92                                                           # 아래: 합격일(왼쪽) · 발급 기관(오른쪽)
    c.setFillColor(HexColor("#111111"))
    c.setFont("Gothic", 12)
    c.drawString(left - 2, base + 26, f"{d.year}년 {d.month}월 {d.day}일")
    c.setFillColor(HexColor("#8a8a8a"))
    c.setFont("Gothic", 11.5)
    c.drawString(left, base, "합격일")
    mid = px + pw * 0.47
    c.setFillColor(HexColor("#111111"))
    c.setFont("GothicBold", 17)
    c.drawString(mid, base + 26, settings.certificate_issuer)
    c.setFillColor(HexColor("#8a8a8a"))
    c.setFont("Gothic", 11.5)
    c.drawString(mid, base, "발급 기관")

    _badge(c, px + pw - 60, py + ph - 52, settings.certificate_issuer)

    url = verify_url(settings, attempt.certificate_no)
    if url:                                                                  # 오른쪽 아래: 진위 확인 QR(빨간 칸)
        c.setFillColor(RED)
        c.rect(px + pw - 98, py + 14, 56, 56, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.rect(px + pw - 94, py + 18, 48, 48, stroke=0, fill=1)
        _qr(c, url, px + pw - 94, py + 18, 48)

    c.setFillColor(HexColor("#333333"))                                      # 테두리 밖: 인증번호·진위 확인·고지
    c.setFont("Gothic", 7.5)
    c.drawRightString(fx + fw - 40, fy - 12, attempt.certificate_no)
    c.setFillColor(GRAY)
    c.setFont("Gothic", 7)
    if url:
        c.drawString(fx + 6, fy - 12, f"진위 확인  {url}")
    c.drawString(fx + 6, fy - 24, f"본 합격증은 {settings.certificate_issuer}의 사내 자격 인증이며 Oracle Corporation 의 공식 자격증이 아닙니다.")

    c.showPage()
    c.save()
    return buf.getvalue()
