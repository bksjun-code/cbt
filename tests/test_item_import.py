import json

import pytest
from sqlalchemy import select

from app.models import AuditLog, Item, ItemStatus, Role
from app.services import item_import
from app.services.item_import import ImportFormatError, import_items, parse_upload
from tests.helpers import make_user


@pytest.fixture()
def admin(db):
    return make_user(db, role=Role.ADMIN)


def obj(**over):
    base = {"domain": "EXTRACT", "difficulty": "MID", "item_type": "SINGLE", "stem": "지문 1",
            "options": {"A": "a", "B": "b", "C": "c"}, "correct_keys": ["B"], "explanation": "해설",
            "variant_group": "G1"}
    base.update(over)
    return base


def as_json(items) -> bytes:
    return json.dumps(items, ensure_ascii=False).encode()


CSV_HEADER = "domain,difficulty,item_type,stem,option_A,option_B,option_C,option_D,option_E,correct_keys,explanation,variant_group,ogg_version,architecture,reference"


def csv_row(stem="지문", correct="B", vg="G1", dom="EXTRACT", diff="MID", typ="SINGLE", extra=""):
    return f'{dom},{diff},{typ},"{stem}","보기1","보기2","보기3","보기4","보기5",{correct},"해설",{vg},19c,BOTH,"참고"{extra}'


def csv_bytes(*rows, enc="utf-8") -> bytes:
    return ("\n".join([CSV_HEADER, *rows]) + "\n").encode(enc)


# ---------- 성공 ----------
def test_json_array_import_creates_drafts(db, admin):
    db.add_all([])  # 도메인 시드 필요
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.json", as_json([obj(), obj(stem="지문 2", variant_group="G2")]), admin)
    assert r.ok and r.created == 2 and r.total == 2
    items = list(db.scalars(select(Item)))
    assert {i.status for i in items} == {ItemStatus.DRAFT} and all(i.created_by == admin.id for i in items)
    log = db.scalar(select(AuditLog).where(AuditLog.action == "ITEM_IMPORT"))
    assert log.detail == {"created": 2} and log.target == "q.json"


def test_json_items_wrapper_accepted(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    assert import_items(db, "q.json", json.dumps({"items": [obj()]}).encode(), admin).created == 1


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "cp949"])
def test_csv_encodings(db, admin, enc):
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.csv", csv_bytes(csv_row(stem="한글 지문"), enc=enc), admin)
    assert r.ok and db.scalar(select(Item)).stem == "한글 지문"


def test_csv_multi_correct_keys_and_defaults(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.csv", csv_bytes(csv_row(typ="multi", correct="a|C", dom="extract", diff="mid")), admin)
    it = db.scalar(select(Item))
    assert r.ok and it.correct_keys == ["A", "C"] and it.item_type.value == "MULTI" and it.domain.code == "EXTRACT"
    assert it.reference == "참고" and it.architecture.value == "BOTH" and len(it.options) == 5


def test_csv_blank_lines_skipped_and_optional_columns(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    raw = "domain,difficulty,item_type,stem,option_A,option_B,correct_keys,explanation,variant_group\n\nEXTRACT,LOW,SINGLE,지문,가,나,A,해설,G9\n,,,,,,,,\n".encode()
    r = import_items(db, "q.csv", raw, admin)
    assert r.ok and r.total == 1 and db.scalar(select(Item)).options == {"A": "가", "B": "나"}


# ---------- 행 단위 오류: all-or-nothing ----------
def test_any_error_rejects_everything(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    rows = [obj(), obj(stem="지문 2", variant_group="G2", correct_keys=["Z"]), obj(stem="지문 3", domain="NOPE", variant_group="G3")]
    r = import_items(db, "q.json", as_json(rows), admin)
    assert not r.ok and r.created == 0 and r.total == 3
    assert [e.row for e in r.errors] == [2, 3]
    assert db.query(Item).count() == 0  # 유효했던 1번도 등록되지 않음
    assert db.scalar(select(AuditLog).where(AuditLog.action == "ITEM_IMPORT_REJECTED")).detail["error_rows"] == [2, 3]


def test_error_messages_do_not_echo_input_values(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    secret = "SECRET-INPUT-VALUE"
    r = import_items(db, "q.json", as_json([obj(difficulty=secret)]), admin)
    assert not r.ok
    assert secret not in " ".join(r.errors[0].messages)
    assert any("difficulty" in m for m in r.errors[0].messages)


def test_multiple_problems_in_one_row_all_reported(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.json", as_json([obj(stem=" ", options={"A": "x"}, difficulty="X")]), admin)
    msgs = " ".join(r.errors[0].messages)
    assert "difficulty" in msgs and "stem" in msgs  # 필드 단위 오류는 한 행에서 함께 보고
    # (모델 단위 검사인 보기 개수/정답 일관성은 필드 오류를 고친 뒤 다음 업로드에서 보고된다)


def test_non_object_element_reported(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.json", as_json([obj(), "문자열", 3]), admin)
    assert [e.row for e in r.errors] == [2, 3]


def test_duplicate_within_file(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.json", as_json([obj(), obj(stem="  지문   1 ", variant_group="G2")]), admin)
    assert [e.row for e in r.errors] == [2] and "1번" in r.errors[0].messages[0]


def test_duplicate_against_existing_db_item(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    assert import_items(db, "q.json", as_json([obj()]), admin).ok
    r = import_items(db, "q2.json", as_json([obj(variant_group="OTHER")]), admin)
    assert not r.ok and "이미 등록된" in r.errors[0].messages[0]
    assert db.query(Item).count() == 1


def test_csv_error_rows_use_real_line_numbers(db, admin):
    from app.seed import seed_domains

    seed_domains(db)
    r = import_items(db, "q.csv", csv_bytes(csv_row(), csv_row(stem="둘", vg="G2", correct="Q"), csv_row(stem="셋", vg="G3")), admin)
    assert [e.row for e in r.errors] == [3]  # 헤더 1행, 데이터 2·3·4행 중 두 번째 데이터 = 3행


# ---------- 형식 오류 ----------
@pytest.mark.parametrize(
    "name,raw,needle",
    [
        ("q.txt", b"x", "지원하는 형식"),
        ("q.json", b"   ", "빈 파일"),
        ("q.json", b"{not json", "JSON 문법"),
        ("q.json", b'{"foo": 1}', "배열"),
        ("q.json", b"[]", "등록할 문항이 없습니다"),
        ("q.csv", b"a,b\n1,2\n", "필수 열"),
        ("q.csv", (CSV_HEADER + "\n").encode(), "등록할 문항이 없습니다"),
        ("q.json", b"\xff\xfe\xff\xff\x00\x81", "인코딩"),
    ],
)
def test_format_errors(name, raw, needle):
    with pytest.raises(ImportFormatError, match=needle):
        parse_upload(name, raw)


def test_size_and_row_limits(monkeypatch):
    monkeypatch.setattr(item_import, "MAX_BYTES", 100)
    with pytest.raises(ImportFormatError, match="너무 큽니다"):
        parse_upload("q.json", b"[" + b" " * 200 + b"]")
    monkeypatch.setattr(item_import, "MAX_BYTES", 10_000_000)
    monkeypatch.setattr(item_import, "MAX_ROWS", 2)
    with pytest.raises(ImportFormatError, match="최대 2문항"):
        parse_upload("q.json", as_json([obj()] * 3))


def test_extension_check_is_case_insensitive():
    assert parse_upload("Q.JSON", as_json([obj()]))[0][0] == 1
