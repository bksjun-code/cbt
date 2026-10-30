import re

import pytest
from sqlalchemy import select

from app.models import AuditLog, Blueprint, Item, User
from tests.test_item_analysis import add
from tests.test_web import HX, PW, client, env, login, page_token, token  # noqa: F401


@pytest.fixture()
def Session(env):  # noqa: F811
    return env[1]


@pytest.fixture()
def adm(client):  # noqa: F811
    login(client, "admin")
    return client


def populate(Session, item_id=1, good=True, n=40):
    """item_id 문항에 종료 응시 n건을 직접 만든다. good=True 면 변별도 높음, False 면 변별도 낮음(경고)."""
    with Session() as db:
        u = db.scalar(select(User).where(User.login_id == "cand"))
        bp = db.get(Blueprint, 1)
        item = db.get(Item, item_id)
        right = item.correct_keys[0]
        wrong = next(k for k in item.options if k not in item.correct_keys)
        specs = [(s, [right] if (s > n // 2 if good else s % 2 == 0) else [wrong]) for s in range(1, n + 1)]
        add(db, u, bp, item, specs)
        db.commit()


ANALYTICS_GET = ["/admin/analytics", "/admin/analytics/items/1", "/admin/analytics/export.csv"]


# ================= 접근 제어 =================
def test_anonymous_redirected(client):  # noqa: F811
    for p in ANALYTICS_GET:
        r = client.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", p


def test_candidate_forbidden(client):  # noqa: F811
    login(client, "cand")
    for p in ANALYTICS_GET:
        assert client.get(p).status_code == 403, p


# ================= 목록 =================
def test_empty_state_before_any_exam(adm):
    html = adm.get("/admin/analytics").text
    assert "(0문항)" in html and "분석할 응시 데이터가 없습니다" in html


def test_list_shows_stats_warning_and_small_sample_badge(adm, Session):
    populate(Session, 1, good=True)
    populate(Session, 2, good=False)
    populate(Session, 3, good=True, n=5)
    html = adm.get("/admin/analytics").text
    assert "(3문항)" in html and "변별도 경고" in html and "표본 부족" in html
    # 경고 문항(#2)이 정상 문항(#1)보다 앞에 온다
    assert html.index('href="/admin/analytics/items/2"') < html.index('href="/admin/analytics/items/1"')
    assert html.index('href="/admin/analytics/items/1"') < html.index('href="/admin/analytics/items/3"')
    assert 'class="bad">1</b>' in html  # 경고 KPI


def test_filters_warn_only_sort_and_bad_values(adm, Session):
    populate(Session, 1, good=True)
    populate(Session, 2, good=False)
    only = adm.get("/admin/analytics?warn=1").text
    assert "(1문항)" in only and 'items/2"' in only and 'items/1"' not in only
    assert "(0문항)" in adm.get("/admin/analytics?domain=OPS&warn=1").text
    assert adm.get("/admin/analytics?sort=<script>&domain=%00&page=-9").status_code == 200
    assert adm.get("/admin/analytics?page=999").status_code == 200


def test_pagination_links_keep_filters(adm, Session):
    for i in range(1, 36):
        populate(Session, i, good=True, n=5)
    p1 = adm.get("/admin/analytics?sort=n").text
    assert "(35문항)" in p1 and "1 / 2" in p1 and p1.count('href="/admin/analytics/items/') == 30
    assert "/admin/analytics?sort=n&amp;page=2" in p1 or "/admin/analytics?sort=n&page=2" in p1
    p2 = adm.get("/admin/analytics?sort=n&page=2").text
    assert "2 / 2" in p2 and p2.count('href="/admin/analytics/items/') == 5


# ================= 상세 =================
def test_item_detail_distribution_and_flags(adm, Session):
    populate(Session, 1, good=False)
    html = adm.get("/admin/analytics/items/1").text
    assert "문항 #1 분석" in html and "보기별 선택 분포" in html and "<svg" in html
    assert "변별도가 2" not in html and "변별도가 0.2 미만" in html  # 경고 문구
    assert html.count('class="badge pass">정답') == 1
    assert "무응답" in html and "상위 14건" not in html and "상위 11건" in html  # 40건 → 27% = 11


def test_item_detail_sample_warning_and_negative(adm, Session):
    populate(Session, 4, good=True, n=6)
    assert "표본 부족" in adm.get("/admin/analytics/items/4").text
    with Session() as db:
        u = db.scalar(select(User).where(User.login_id == "cand"))
        item = db.get(Item, 5)
        right = item.correct_keys[0]
        wrong = next(k for k in item.options if k not in item.correct_keys)
        add(db, u, db.get(Blueprint, 1), item, [(s, [wrong] if s > 20 else [right]) for s in range(1, 41)])
        db.commit()
    assert "변별도가 <b>음수</b>입니다" in adm.get("/admin/analytics/items/5").text


def test_item_detail_unknown_or_unanalyzed_is_404(adm):
    assert adm.get("/admin/analytics/items/1").status_code == 404  # 응시 데이터 없음
    assert adm.get("/admin/analytics/items/999999").status_code == 404
    assert adm.get("/admin/analytics/items/abc").status_code == 404


def test_override_notice_on_detail_and_list(adm, Session):
    populate(Session, 1, good=True)
    tok = token(adm.get("/admin").text)
    adm.post("/admin/items/1/override", data={"csrf_token": tok, "mode": "CREDIT_ALL", "reason": "테스트 보정"})
    assert "채점 보정이 적용되어 있습니다" in adm.get("/admin/analytics/items/1").text
    assert "보정 정답 인정" in adm.get("/admin/analytics").text


def test_user_content_is_escaped(adm, Session):
    xss = "<script>alert('x')</script>"
    with Session() as db:
        it = db.get(Item, 1)
        it.stem = xss
        it.options = {**it.options, "A": xss}
        db.commit()
    populate(Session, 1, good=True)
    for p in ("/admin/analytics", "/admin/analytics/items/1"):
        html = adm.get(p).text
        assert "<script>alert('x')</script>" not in html and "&lt;script&gt;" in html, p


def test_links_between_item_page_analysis_and_nav(adm, Session):
    populate(Session, 1, good=True)
    assert "문항 분석 보기" not in adm.get("/admin/items/2").text  # 출제 이력 없는 문항에는 링크가 없다
    assert 'href="/admin/analytics/items/1"' in adm.get("/admin/items/1").text  # 응시 데이터가 생긴 문항
    assert 'href="/admin/analytics"' in adm.get("/admin").text


# ================= 내보내기 =================
def test_export_csv_content_audit_and_filters(adm, Session):
    populate(Session, 1, good=True)
    populate(Session, 2, good=False)
    r = adm.get("/admin/analytics/export.csv?warn=1")
    assert r.status_code == 200 and r.content.startswith(b"\xef\xbb\xbf") and r.headers["content-type"].startswith("text/csv")
    assert re.match(r'attachment; filename="item_analysis_\d{8}_\d{4}\.csv"', r.headers["content-disposition"])
    lines = r.content.decode("utf-8-sig").strip().splitlines()
    assert lines[0].startswith("문항 ID,영역,난이도") and len(lines) == 2 and lines[1].startswith("2,")
    assert ",Y," in lines[1]  # 표본 충분/경고
    with Session() as db:
        log = db.scalars(select(AuditLog).where(AuditLog.action == "ANALYSIS_EXPORT")).one()
        assert log.detail == {"rows": 1, "filters": "warn=1"}


def test_export_neutralizes_formula_in_stem(adm, Session):
    with Session() as db:
        db.get(Item, 1).stem = "=HYPERLINK(\"http://evil\",\"x\")"
        db.commit()
    populate(Session, 1, good=True)
    text = adm.get("/admin/analytics/export.csv").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and ',=HYPERLINK' not in text


def test_kpi_numbers_are_rendered_not_python_reprs(adm, Session):
    populate(Session, 1, good=True)
    populate(Session, 2, good=False)
    html = adm.get("/admin/analytics").text
    kpi = dict(re.findall(r'<div class="kpi">([^<]+)<b[^>]*>([^<]+)</b>', html))
    assert kpi["분석 문항"] == "2" and kpi["표본 충분"] == "2" and kpi["변별도 경고"] == "1"
    assert re.fullmatch(r"\d\.\d\d", kpi["평균 정답률(p)"]) and re.fullmatch(r"-?\d\.\d\d", kpi["평균 변별도(D)"])


LEAK_MARKERS = ("built-in method", "<bound method", "object at 0x", "Undefined", "<function ", "None</td>")


def test_no_admin_page_leaks_python_reprs(adm, Session):
    """Jinja 에서 dict 키가 dict 메서드(items/values/keys…)와 충돌하면 repr 문자열이 화면에 그대로 찍힌다.
    데이터가 있는 상태에서 모든 관리자 화면을 렌더링해 그런 흔적이 없는지 확인한다."""
    populate(Session, 1, good=True)
    populate(Session, 2, good=False)
    pages = ["/admin", "/admin/items", "/admin/items/1", "/admin/items/new", "/admin/items/upload", "/admin/blueprints",
             "/admin/blueprints/1", "/admin/blueprints/new", "/admin/results", "/admin/users", "/admin/settings",
             "/admin/audit", "/admin/analytics", "/admin/analytics/items/1", "/admin/analytics/items/2"]
    for p in pages:
        r = adm.get(p)
        assert r.status_code == 200, p
        for marker in LEAK_MARKERS:
            assert marker not in r.text, (p, marker)
