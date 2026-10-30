"""견고성/보안 테스트: 예상 못 한 입력에서 500 오류·정보 노출·세션 위조가 없는지 확인한다."""
import base64
import csv
import json

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from itsdangerous import TimestampSigner
from sqlalchemy import select, text

from app.models import Item, User
from app.schemas.item import ItemIn
from app.services.item_import import ImportFormatError, import_items, parse_upload
from tests.helpers import make_user
from tests.test_web import HX, PW, client, env, login, page_token, token  # noqa: F401

NASTY = [
    "' OR '1'='1", "'; DROP TABLE users;--", '" OR 1=1 --', "1; SELECT * FROM users", "%", "_", "\\", "%00", "\x00",
    "{{7*7}}", "${7*7}", "#{7*7}", "<script>alert(1)</script>", "\"><img src=x onerror=alert(1)>", "../../etc/passwd",
    "𝒳" * 40, "😀", "‮‭", "a" * 5000, "x\r\nSet-Cookie: pwn=1", "null", "NaN", "-1", "9" * 40, "[]", "{}", " ", "",
]
# 서버 내부 정보가 응답에 새는지 본다. (사용자가 넣은 값이 화면에 되돌려 보이는 것은 누출이 아니므로 'SELECT ' 같은 입력 문자열은 검사하지 않는다)
LEAK_WORDS = ("Traceback", "sqlite3.", "OperationalError", "StatementError", "LookupError", "OverflowError", "[SQL:", "sqlalchemy.exc")


@pytest.fixture()
def Session(env):  # noqa: F811
    return env[1]


@pytest.fixture()
def adm(client):  # noqa: F811
    login(client, "admin")
    return client


def clean(r):
    assert r.status_code < 500, (r.status_code, r.text[:300])
    for w in LEAK_WORDS:
        assert w not in r.text, (w, r.request.url)


# =============== GET 필터에 이상한 값 ===============
FILTERS = {
    "/admin/items": ["q", "domain", "difficulty", "status", "item_type", "page"],
    "/admin/results": ["q", "status", "passed", "date_from", "date_to", "page"],
    "/admin/users": ["q", "role", "page"],
    "/admin/audit": ["q", "action", "date_from", "date_to", "page"],
    "/admin/analytics": ["domain", "difficulty", "status", "warn", "sort", "page"],
}


@pytest.mark.parametrize("path,param", [(p, k) for p, ks in FILTERS.items() for k in ks])
def test_filters_never_500_or_leak(adm, path, param):
    for value in NASTY:
        clean(adm.get(path, params={param: value}))


def test_sql_injection_attempts_do_not_alter_the_database(adm, Session):
    with Session() as db:
        before = (db.query(User).count(), db.query(Item).count())
    for payload in ("'; DROP TABLE users;--", "' OR '1'='1' --", "1); DELETE FROM items;--"):
        for path, param in (("/admin/items", "q"), ("/admin/users", "q"), ("/admin/results", "q"), ("/admin/audit", "q")):
            clean(adm.get(path, params={param: payload}))
    adm.post("/login", data={"login_id": "' OR '1'='1' --", "password": "x", "csrf_token": "x"})
    with Session() as db:
        assert (db.query(User).count(), db.query(Item).count()) == before
        assert db.execute(text("select count(*) from sqlite_master where name='users'")).scalar() == 1


def test_template_injection_strings_are_not_evaluated(adm):
    html = adm.get("/admin/items", params={"q": "{{7*7}}"}).text
    assert 'value="{{7*7}}"' in html and 'value="49"' not in html


def test_login_with_nasty_credentials_returns_401_not_500(client):  # noqa: F811
    for value in NASTY:
        t = token(client.get("/login").text)
        r = client.post("/login", data={"login_id": value, "password": value, "csrf_token": t})
        assert r.status_code in (401, 422), (r.status_code, value[:20])
        assert "Traceback" not in r.text


def test_password_longer_than_bcrypt_limit_is_a_clean_failure(client):  # noqa: F811
    t = token(client.get("/login").text)
    r = client.post("/login", data={"login_id": "cand", "password": "p" * 150, "csrf_token": t})
    assert r.status_code == 401


# =============== 폼 POST 에 이상한 값 ===============
def item_form(c, **over):
    d = {"csrf_token": token(c.get("/admin").text), "domain": "OPS", "difficulty": "LOW", "item_type": "SINGLE",
         "architecture": "BOTH", "ogg_version": "19c", "stem": "퍼징 지문", "option_A": "가", "option_B": "나",
         "correct_keys": ["A"], "explanation": "해설", "reference": "", "variant_group": "FZ-1"}
    d.update(over)
    return d


@pytest.mark.parametrize("field", ["stem", "option_A", "option_B", "explanation", "reference", "variant_group", "ogg_version"])
def test_item_form_text_fields_never_500(adm, field):
    for i, value in enumerate(NASTY):
        r = adm.post("/admin/items/new", data=item_form(adm, **{field: value, "stem": f"퍼징 {field} {i}"} if field != "stem" else {"stem": value}),
                     follow_redirects=False)
        assert r.status_code in (303, 409, 422), (field, value[:20], r.status_code)
        assert "Traceback" not in r.text


@pytest.mark.parametrize("field,value", [("domain", "<x>"), ("difficulty", "EXTREME"), ("item_type", ""), ("architecture", "ZZZ"),
                                         ("correct_keys", ["Z"]), ("correct_keys", ["A", "A"]), ("domain", "OPS\x00")])
def test_item_form_enum_fields_with_garbage_return_422(adm, field, value):
    r = adm.post("/admin/items/new", data=item_form(adm, **{field: value}))
    assert r.status_code == 422


def test_item_field_length_limits_match_column_definitions(adm):
    """DB 컬럼은 variant_group 100자, reference 300자이다. SQLite 는 길이를 강제하지 않지만 PostgreSQL 등은 오류가 난다."""
    assert adm.post("/admin/items/new", data=item_form(adm, variant_group="v" * 101)).status_code == 422
    assert adm.post("/admin/items/new", data=item_form(adm, reference="r" * 301)).status_code == 422
    assert adm.post("/admin/items/new", data=item_form(adm, stem="s" * 4001)).status_code == 422
    assert adm.post("/admin/items/new", data=item_form(adm, explanation="e" * 4001)).status_code == 422
    assert adm.post("/admin/items/new", data=item_form(adm, option_A="o" * 501)).status_code == 422
    ok = adm.post("/admin/items/new", data=item_form(adm, variant_group="v" * 100, reference="r" * 300, stem="s" * 4000),
                  follow_redirects=False)
    assert ok.status_code == 303


def test_html_special_chars_roundtrip_escaped_in_all_item_views(adm):
    evil = "<img src=x onerror=alert(1)>&\"'`"
    r = adm.post("/admin/items/new", data=item_form(adm, stem=evil, option_A=evil, explanation=evil, variant_group="ESC-1"),
                 follow_redirects=False)
    iid = r.headers["location"].rsplit("/", 1)[1]
    for path in (f"/admin/items/{iid}", "/admin/items?q=ESC-1"):
        html = adm.get(path).text
        assert "<img src=x" not in html and "&lt;img" in html


# =============== 세션 위조 ===============
def forged_cookie(secret: str, data: dict) -> str:
    """Starlette SessionMiddleware 와 같은 방식으로 세션 쿠키를 만든다."""
    payload = base64.b64encode(json.dumps(data).encode("utf-8"))
    return TimestampSigner(secret).sign(payload).decode("utf-8")


def test_forged_session_control_experiment_proves_the_forgery_method_is_valid(client, Session):  # noqa: F811
    """대조 실험: 올바른 키로 만든 위조 쿠키는 통과한다 → 아래의 '거부' 테스트가 의미 있는 검증임을 보장한다."""
    with Session() as db:
        admin = db.scalar(select(User).where(User.login_id == "admin"))
        admin_id, sv = admin.id, admin.session_version
    client.cookies.set("cbt_session", forged_cookie("test-secret", {"uid": admin_id, "sv": sv}))
    assert client.get("/admin", follow_redirects=False).status_code == 200


def test_session_signed_with_a_different_key_is_rejected(client, Session):  # noqa: F811
    with Session() as db:
        admin_id = db.scalar(select(User).where(User.login_id == "admin")).id
    client.cookies.set("cbt_session", forged_cookie("attacker-secret", {"uid": admin_id}))
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_tampered_or_garbage_session_cookies_are_treated_as_anonymous(client):  # noqa: F811
    login(client, "admin")
    good = client.cookies.get("cbt_session")
    bad_values = [good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"), good[: len(good) // 2], "", "garbage", ".", "a.b.c",
                  good + "x", "\x00", "A" * 5000]
    for v in bad_values:
        client.cookies.clear()
        client.cookies.set("cbt_session", v)
        r = client.get("/admin", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", v[:20]


def test_session_for_nonexistent_user_is_anonymous(client):  # noqa: F811
    client.cookies.set("cbt_session", forged_cookie("test-secret", {"uid": 987654}))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_session_with_wrong_types_does_not_crash(client):  # noqa: F811
    for uid in ("abc", [1], {"a": 1}, None, -1, 0, 1.5, True):
        client.cookies.set("cbt_session", forged_cookie("test-secret", {"uid": uid}))
        assert client.get("/", follow_redirects=False).status_code in (303, 200)


def test_logout_clears_the_session_cookie_content(client):  # noqa: F811
    login(client, "cand")
    client.post("/logout", data={"csrf_token": page_token(client)})
    assert client.get("/", follow_redirects=False).status_code == 303


def test_unsupported_methods_and_paths_do_not_500(client):  # noqa: F811
    login(client, "cand")
    for method, path in [("DELETE", "/"), ("PATCH", "/exam/1"), ("PUT", "/"), ("POST", "/static/css/app.css"), ("GET", "/static/../app/config.py"),
                         ("GET", "/static/%2e%2e/app/config.py"), ("GET", "/exam/-1"), ("GET", "/exam/99999999999999999999"),
                         ("GET", "/exam/1/q/abc"), ("OPTIONS", "/"), ("HEAD", "/login")]:
        r = client.request(method, path, follow_redirects=False)
        assert r.status_code < 500, (method, path, r.status_code)
        assert "Traceback" not in r.text


def test_static_path_traversal_does_not_expose_source(client):  # noqa: F811
    for p in ("/static/../config.py", "/static/..%2fconfig.py", "/static/%2e%2e/%2e%2e/.env", "/static//etc/passwd"):
        r = client.get(p)
        assert r.status_code in (404, 400, 403) and "SECRET_KEY" not in r.text and "class Settings" not in r.text


def test_oversized_request_body_to_upload_is_rejected_cleanly(adm):
    big = b"[" + b" " * (2 * 1024 * 1024 + 10) + b"]"  # 실제 상한(2MB)보다 조금 큼
    r = adm.post("/admin/items/upload", data={"csrf_token": token(adm.get("/admin").text)}, files={"file": ("big.json", big)})
    assert r.status_code == 400 and "너무 큽니다" in r.text


# =============== 업로드 파서 견고성 ===============
def only_known_errors(fn, *args):
    try:
        fn(*args)
    except ImportFormatError:
        pass


ADVERSARIAL_JSON = [
    b"[" * 100000, b"{" * 100000, b'{"a":' * 50000, b"[" + b"1" * 5000 + b"]", b'{"items": ' + b"[" * 3000 + b"]" * 3000 + b"}",
    b"\xef\xbb\xbf[]", b"\xff\xfe", b"\x00" * 100, b'[{"domain": "OPS", "options": "not-a-dict"}]', b"[null, 1, true, [], {}]",
    b'[{"correct_keys": "A"}]', b'[{"options": {"A": 1, "B": [2]}}]', b"NaN", b"[NaN, Infinity]", b'"string"', b"123",
]


@pytest.mark.parametrize("raw", ADVERSARIAL_JSON, ids=lambda b: f"json-{len(b)}B-{b[:12]!r}")
def test_item_import_survives_adversarial_json(db, raw):
    from app.seed import seed_domains

    seed_domains(db)
    admin = make_user(db)
    try:
        res = import_items(db, "x.json", raw, admin)
        assert res.created == 0  # 유효한 문항이 하나도 없으므로 아무것도 등록되면 안 된다
    except ImportFormatError:
        pass


ITEM_CSV_HEADER = b"domain,difficulty,item_type,stem,option_A,option_B,correct_keys,explanation,variant_group\n"


@pytest.mark.parametrize(
    "raw",
    [
        ITEM_CSV_HEADER + b"OPS,LOW,SINGLE," + b"x" * 200000 + b",a,b,A,e,g\n",  # csv 필드 크기 한도(131072) 초과
        ITEM_CSV_HEADER + b"OPS,LOW,SINGLE,s\x00t,a,b,A,e,g\n",                 # NUL 문자
        ITEM_CSV_HEADER + b'OPS,LOW,SINGLE,"unterminated,a,b,A,e,g\n',
    ],
    ids=["huge-field", "nul", "unterminated"],
)
def test_item_import_survives_adversarial_csv(db, raw):
    """필수 열을 갖춘 CSV 여야 헤더 검사에서 걸러지지 않고 실제로 행 파싱 경로를 탄다."""
    only_known_errors(parse_upload, "x.csv", raw)


def test_adversarial_csv_cases_really_reach_the_row_parser():
    """위 케이스가 '필수 열 없음' 으로 일찍 걸러져 아무것도 검증하지 못하는 일을 막는 대조 실험."""
    ok = ITEM_CSV_HEADER + b"OPS,LOW,SINGLE,stem,a,b,A,e,g\n"
    assert parse_upload("x.csv", ok)  # 같은 헤더로 정상 행은 파싱된다
    with pytest.raises(ImportFormatError, match="CSV 형식 오류"):
        parse_upload("x.csv", ITEM_CSV_HEADER + b"OPS,LOW,SINGLE," + b"x" * 200000 + b",a,b,A,e,g\n")


@given(st.binary(max_size=2000), st.sampled_from(["a.json", "a.csv", "a.JSON", "a.txt", ""]))
@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_parse_upload_only_raises_known_errors_for_random_bytes(raw, name):
    only_known_errors(parse_upload, name, raw)


JSONISH = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), st.text(max_size=20)),
    lambda inner: st.one_of(st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=5)),
    max_leaves=25,
)


@given(JSONISH)
@settings(max_examples=300, deadline=None)
def test_item_schema_only_raises_validation_errors_for_arbitrary_structures(obj):
    from pydantic import ValidationError

    try:
        ItemIn.model_validate(obj)
    except ValidationError:
        pass


@given(st.dictionaries(st.sampled_from(["domain", "difficulty", "item_type", "stem", "options", "correct_keys", "explanation",
                                        "variant_group", "architecture", "reference", "ogg_version"]), JSONISH, max_size=11))
@settings(max_examples=300, deadline=None)
def test_item_schema_never_crashes_on_field_shaped_garbage(obj):
    from pydantic import ValidationError

    try:
        ItemIn.model_validate(obj)
    except ValidationError:
        pass
