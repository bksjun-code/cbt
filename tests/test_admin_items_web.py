import json

import pytest
from sqlalchemy import select

from app.models import AttemptItem, AuditLog, Item, ItemStatus
from tests.test_web import HX, PW, client, env, login, page_token, token  # noqa: F401  (픽스처/헬퍼 재사용)


@pytest.fixture()
def Session(env):  # noqa: F811
    return env[1]


@pytest.fixture()
def adm(client):  # noqa: F811
    login(client, "admin")
    return client


def tok(c):
    return token(c.get("/admin").text)


def form_data(c, **over):
    d = {"csrf_token": tok(c), "domain": "EXTRACT", "difficulty": "MID", "item_type": "SINGLE",
         "architecture": "BOTH", "ogg_version": "19c", "stem": "웹에서 등록한 지문",
         "option_A": "가", "option_B": "나", "option_C": "다", "option_D": "", "option_E": "",
         "correct_keys": ["B"], "explanation": "해설입니다", "reference": "문서", "variant_group": "WEB-G1"}
    d.update(over)
    return d


def create(c, **over) -> int:
    r = c.post("/admin/items/new", data=form_data(c, **over), follow_redirects=False)
    assert r.status_code == 303, r.text[:500]
    return int(r.headers["location"].rsplit("/", 1)[1])


def act(c, item_id, action, **extra):
    return c.post(f"/admin/items/{item_id}/{action}", data={"csrf_token": tok(c), **extra}, follow_redirects=True)


# ================= 접근 제어 / CSRF =================
ADMIN_PATHS = ["/admin", "/admin/items", "/admin/items/new", "/admin/items/upload", "/admin/items/1"]


def test_anonymous_redirected(client):  # noqa: F811
    for p in ADMIN_PATHS:
        r = client.get(p, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", p


def test_candidate_forbidden_everywhere(client):  # noqa: F811
    login(client, "cand")
    for p in ADMIN_PATHS:
        assert client.get(p).status_code == 403, p
    t = page_token(client)
    for p in ("/admin/items/new", "/admin/items/1", "/admin/items/1/approve", "/admin/items/upload"):
        assert client.post(p, data={"csrf_token": t}).status_code == 403, p


def test_post_requires_csrf(adm):
    assert adm.post("/admin/items/new", data={"stem": "x"}).status_code == 403
    assert adm.post("/admin/items/1/approve").status_code == 403
    assert adm.post("/admin/items/upload", files={"file": ("a.json", b"[]")}).status_code == 403
    assert adm.post("/admin/items/new", data={"csrf_token": "forged"}).status_code == 403


def test_unknown_item_and_action(adm):
    assert adm.get("/admin/items/99999").status_code == 404
    assert adm.get("/admin/items/abc").status_code == 404
    assert adm.post("/admin/items/1/explode", data={"csrf_token": tok(adm)}).status_code == 404
    assert adm.post("/admin/items/99999/approve", data={"csrf_token": tok(adm)}).status_code == 404


# ================= 목록 =================
def test_dashboard_counts_and_list(adm):
    html = adm.get("/admin").text
    assert "문항 현황" in html and ">64<" in html
    lst = adm.get("/admin/items").text
    assert "(64건)" in lst and lst.count('href="/admin/items/') >= 20
    assert "1 / 4" in lst


def test_list_filters_pagination_and_search(adm):
    assert "(9건)" in adm.get("/admin/items?domain=ARCH").text
    assert "(3건)" in adm.get("/admin/items?domain=ARCH&difficulty=HIGH").text
    assert "(0건)" in adm.get("/admin/items?status=DRAFT").text
    assert "(1건)" in adm.get("/admin/items?q=ARCH-TRAIL").text
    page2 = adm.get("/admin/items?page=2&domain=OPS").text
    assert "2 /" not in page2  # OPS 9건은 1페이지
    assert adm.get("/admin/items?page=-5").status_code == 200  # 비정상 page 는 1로 보정
    assert adm.get("/admin/items?page=999").status_code == 200


def test_search_term_is_escaped_in_page(adm):
    html = adm.get('/admin/items?q="><script>alert(1)</script>').text
    assert "<script>alert(1)</script>" not in html


# ================= 등록 / 수정 =================
def test_create_form_and_post(adm, Session):
    assert "DRAFT 로 저장됩니다" in adm.get("/admin/items/new").text
    iid = create(adm)
    with Session() as db:
        it = db.get(Item, iid)
        assert it.status == ItemStatus.DRAFT and it.correct_keys == ["B"] and set(it.options) == {"A", "B", "C"}
        assert it.created_by is not None and it.variant_group == "WEB-G1"
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ITEM_CREATE")) is not None
    page = adm.get(f"/admin/items/{iid}").text
    assert "문항 등록을 DRAFT 로 등록했습니다" not in page  # 플래시는 리다이렉트 직후 1회만
    assert "웹에서 등록한 지문" in page


def test_flash_message_shown_once(adm):
    r = adm.post("/admin/items/new", data=form_data(adm), follow_redirects=True)
    assert "DRAFT 로 등록했습니다" in r.text
    assert "DRAFT 로 등록했습니다" not in adm.get(r.url.path).text


def test_create_validation_errors_keep_input(adm, Session):
    r = adm.post("/admin/items/new", data=form_data(adm, correct_keys=["A", "B"], stem="입력 유지 확인"))
    assert r.status_code == 422 and "입력을 확인해 주세요" in r.text
    assert "입력 유지 확인" in r.text and 'value="가"' in r.text
    r = adm.post("/admin/items/new", data=form_data(adm, domain=""))
    assert r.status_code == 422
    r = adm.post("/admin/items/new", data=form_data(adm, option_B="", correct_keys=["B"]))
    assert r.status_code == 422
    with Session() as db:
        assert db.query(Item).filter(Item.variant_group == "WEB-G1").count() == 0


def test_create_duplicate_returns_409(adm):
    create(adm)
    r = adm.post("/admin/items/new", data=form_data(adm, variant_group="OTHER"))
    assert r.status_code == 409 and "동일한 문항이 이미 존재" in r.text


def test_multi_item_requires_two_keys(adm):
    r = adm.post("/admin/items/new", data=form_data(adm, item_type="MULTI", correct_keys=["A"]))
    assert r.status_code == 422
    assert adm.post("/admin/items/new", data=form_data(adm, item_type="MULTI", correct_keys=["A", "C"]),
                    follow_redirects=False).status_code == 303


def test_user_content_is_html_escaped(adm):
    xss = "<script>alert('x')</script><img src=x onerror=alert(1)>"
    iid = create(adm, stem=xss, option_A=xss, explanation=xss, variant_group="XSS")
    for path in (f"/admin/items/{iid}", "/admin/items?q=XSS"):
        html = adm.get(path).text
        assert "<script>alert('x')</script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;" in adm.get(f"/admin/items/{iid}").text


def test_edit_draft_via_form(adm, Session):
    iid = create(adm)
    r = adm.post(f"/admin/items/{iid}", data=form_data(adm, stem="수정된 지문"), follow_redirects=True)
    assert "저장했습니다" in r.text
    with Session() as db:
        assert db.get(Item, iid).stem == "수정된 지문"


def test_edit_validation_error_and_duplicate(adm):
    a = create(adm)
    create(adm, stem="다른 지문", variant_group="B")
    r = adm.post(f"/admin/items/{a}", data=form_data(adm, stem="다른 지문"))
    assert r.status_code == 409
    r = adm.post(f"/admin/items/{a}", data=form_data(adm, correct_keys=[]))
    assert r.status_code == 422 and "문항 #%d" % a in r.text


# ================= 워크플로 =================
def test_review_workflow_through_ui(adm, Session):
    iid = create(adm)
    assert "검수 요청" in adm.get(f"/admin/items/{iid}").text
    assert "REVIEWED" in act(adm, iid, "submit-review").text
    page = adm.get(f"/admin/items/{iid}").text
    assert "승인 (REVIEWED" in page and "반려 사유" in page
    assert "처리했습니다: APPROVED" in act(adm, iid, "approve").text
    with Session() as db:
        it = db.get(Item, iid)
        assert it.status == ItemStatus.APPROVED and it.approved_by and it.approved_at
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ITEM_APPROVE", AuditLog.target == f"item:{iid}"))
    assert "승인:" in adm.get(f"/admin/items/{iid}").text


def test_reject_needs_reason_and_shows_in_history(adm, Session):
    iid = create(adm)
    act(adm, iid, "submit-review")
    r = act(adm, iid, "reject", reason="   ")
    assert "반려 사유를 입력" in r.text
    with Session() as db:
        assert db.get(Item, iid).status == ItemStatus.REVIEWED
    r = act(adm, iid, "reject", reason="보기 C 가 모호")
    assert "처리했습니다: DRAFT" in r.text and "보기 C 가 모호" in adm.get(f"/admin/items/{iid}").text


def test_invalid_transition_shows_error_flash(adm):
    iid = create(adm)
    r = act(adm, iid, "approve")  # DRAFT 에서 바로 승인 불가
    assert "할 수 없습니다" in r.text


def test_retire_and_new_version(adm, Session):
    iid = create(adm)
    act(adm, iid, "retire")
    page = adm.get(f"/admin/items/{iid}").text
    assert "폐기된 문항입니다" in page and "<fieldset class=\"plain\" disabled" in page
    r = adm.post(f"/admin/items/{iid}/new-version", data={"csrf_token": tok(adm)}, follow_redirects=False)
    new_id = int(r.headers["location"].rsplit("/", 1)[1])
    with Session() as db:
        n = db.get(Item, new_id)
        assert n.status == ItemStatus.DRAFT and n.version == 2 and n.previous_version_id == iid
    assert f"#{iid}" in adm.get(f"/admin/items/{new_id}").text  # 버전 이력 링크
    r = act(adm, iid, "new-version")
    assert "이미 신규 버전" in r.text


def test_item_with_exam_history_is_read_only_in_ui(adm, client, Session):
    # 응시자가 시험을 시작해 문항이 출제되도록 한다
    from fastapi.testclient import TestClient  # noqa: F401

    with Session() as db:
        from app.models import User
        from app.services import exam

        u = db.scalar(select(User).where(User.login_id == "cand"))
        a, _ = exam.start_attempt(db, u)
        used = db.get(Item, a.items[0].item_id).id
    page = adm.get(f"/admin/items/{used}").text
    assert "출제 이력이 있어 수정할 수 없습니다" in page and "<fieldset class=\"plain\" disabled" in page
    r = adm.post(f"/admin/items/{used}", data=form_data(adm, stem="변경 시도", domain="ARCH", variant_group="x"),
                 follow_redirects=True)
    assert "출제 이력이 있는 문항은 수정할 수 없습니다" in r.text
    with Session() as db:
        assert db.get(Item, used).stem != "변경 시도"
    # 신규 버전 경로는 열려 있다
    r = adm.post(f"/admin/items/{used}/new-version", data={"csrf_token": tok(adm)}, follow_redirects=False)
    assert r.status_code == 303


def test_editing_approved_item_warns_and_resets(adm, Session):
    iid = create(adm)
    act(adm, iid, "submit-review")
    act(adm, iid, "approve")
    assert "수정하면 DRAFT 로 되돌아가" in adm.get(f"/admin/items/{iid}").text
    r = adm.post(f"/admin/items/{iid}", data=form_data(adm, explanation="고친 해설"), follow_redirects=True)
    assert "재검수가 필요하여 DRAFT 로 되돌렸습니다" in r.text
    with Session() as db:
        assert db.get(Item, iid).status == ItemStatus.DRAFT


# ================= 업로드 =================
def upload(c, name, raw, tkn=None):
    return c.post("/admin/items/upload", data={"csrf_token": tkn or tok(c)}, files={"file": (name, raw)}, follow_redirects=False)


def item_json(**over):
    d = {"domain": "OPS", "difficulty": "LOW", "item_type": "SINGLE", "stem": "업로드 지문", "options": {"A": "a", "B": "b"},
         "correct_keys": ["A"], "explanation": "해설", "variant_group": "UP-1"}
    d.update(over)
    return d


def test_upload_json_success_redirects_to_drafts(adm, Session):
    raw = json.dumps([item_json(), item_json(stem="업로드 지문2", variant_group="UP-2")]).encode()
    r = upload(adm, "items.json", raw)
    assert r.status_code == 303 and r.headers["location"] == "/admin/items?status=DRAFT"
    page = adm.get("/admin/items?status=DRAFT").text
    assert "(2건)" in page
    with Session() as db:
        assert db.query(Item).filter(Item.status == ItemStatus.DRAFT).count() == 2
        assert db.scalar(select(AuditLog).where(AuditLog.action == "ITEM_IMPORT")).detail == {"created": 2}


def test_upload_csv_korean_cp949(adm):
    csv = "domain,difficulty,item_type,stem,option_A,option_B,correct_keys,explanation,variant_group\nOPS,LOW,SINGLE,한글 지문,가,나,A,해설,CSV-1\n"
    r = upload(adm, "q.csv", csv.encode("cp949"))
    assert r.status_code == 303


def test_upload_errors_listed_by_row_and_nothing_saved(adm, Session):
    raw = json.dumps([item_json(), item_json(stem="둘", variant_group="U2", correct_keys=["Z"]),
                      item_json(stem="셋", variant_group="U3", difficulty="EXTREME")]).encode()
    r = adm.post("/admin/items/upload", data={"csrf_token": tok(adm)}, files={"file": ("x.json", raw)})
    assert r.status_code == 422 and "아무것도 등록하지 않았습니다" in r.text
    assert "<b>2번</b>" in r.text and "<b>3번</b>" in r.text and "<b>1번</b>" not in r.text
    assert "EXTREME" not in r.text  # 입력값 비노출
    with Session() as db:
        assert db.query(Item).filter(Item.status == ItemStatus.DRAFT).count() == 0


@pytest.mark.parametrize("name,raw,needle", [("x.txt", b"hello", "지원하는 형식"), ("x.json", b"{bad", "JSON 문법"),
                                             ("x.json", b"", "빈 파일")])
def test_upload_format_errors_return_400(adm, name, raw, needle):
    r = adm.post("/admin/items/upload", data={"csrf_token": tok(adm)}, files={"file": (name, raw)})
    assert r.status_code == 400 and needle in r.text


def test_upload_oversize_rejected(adm, monkeypatch):
    from app.routers import admin_items
    from app.services import item_import

    monkeypatch.setattr(item_import, "MAX_BYTES", 200)
    monkeypatch.setattr(admin_items, "MAX_BYTES", 200)
    r = adm.post("/admin/items/upload", data={"csrf_token": tok(adm)}, files={"file": ("x.json", b"[" + b" " * 500 + b"]")})
    assert r.status_code == 400 and "너무 큽니다" in r.text


def test_upload_page_has_format_help(adm):
    html = adm.get("/admin/items/upload").text
    assert "enctype=\"multipart/form-data\"" in html and "option_A" in html and "A|C" in html

