"""문항 JSON/CSV 일괄 업로드.

  - 전체를 먼저 검증하고, 한 행이라도 오류가 있으면 아무것도 등록하지 않는다(all-or-nothing)
  - 통과한 문항은 모두 DRAFT 로 등록된다
  - 오류 메시지에는 입력값을 되돌려 싣지 않는다(위치와 사유만)

CSV 열: domain, difficulty, item_type, stem, option_A, option_B, [option_C..E], correct_keys,
        explanation, variant_group, [ogg_version, architecture, reference]
        correct_keys 는 'A' 또는 'A|C' ('|' ',' ';' 공백 구분)
JSON:   문항 객체 배열 또는 {"items": [...]} (docs/문항 JSON 스키마와 동일)
"""
import csv
import io
import json
import re
from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.models import User
from app.schemas.item import OPTION_KEYS, ItemIn
from app.services.audit import log_audit
from app.services.item_admin import create_item, domain_by_code, find_duplicate, norm_stem

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 1000
CSV_REQUIRED = ("domain", "difficulty", "item_type", "stem", "option_A", "option_B",
                "correct_keys", "explanation", "variant_group")


class ImportFormatError(Exception):
    """파일 형식/크기 자체가 잘못됨(행 단위 오류가 아님)."""


@dataclass
class RowError:
    row: int  # JSON: 1부터 시작하는 항목 번호 / CSV: 헤더가 1행인 실제 행 번호
    messages: list[str]


@dataclass
class ImportResult:
    total: int = 0
    created: int = 0
    errors: list[RowError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "cp949"):  # Excel 에서 저장한 한글 CSV 대응
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ImportFormatError("문자 인코딩을 해석할 수 없습니다(UTF-8 또는 CP949 필요)")


def _parse_json(text: str) -> list[tuple[int, object]]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportFormatError(f"JSON 문법 오류: {e.msg} (줄 {e.lineno})") from None
    except (RecursionError, ValueError):  # 너무 깊게 중첩된 구조, 4300자리를 넘는 정수 등
        raise ImportFormatError("JSON 구조가 비정상입니다(너무 깊게 중첩되었거나 숫자가 너무 큽니다)") from None
    if isinstance(doc, dict) and isinstance(doc.get("items"), list):
        doc = doc["items"]
    if not isinstance(doc, list):
        raise ImportFormatError("JSON 은 문항 객체 배열이거나 {\"items\": [...]} 여야 합니다")
    return list(enumerate(doc, start=1))


def _parse_csv(text: str) -> list[tuple[int, object]]:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in CSV_REQUIRED if c not in header]
    if missing:
        raise ImportFormatError("CSV 필수 열이 없습니다: " + ", ".join(missing))
    out: list[tuple[int, object]] = []
    for lineno, row in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        if not any(row.values()):
            continue  # 빈 줄
        options = {k: row.get(f"option_{k}", "") for k in OPTION_KEYS if row.get(f"option_{k}", "")}
        data = {
            "domain": row["domain"].upper(), "difficulty": row["difficulty"].upper(),
            "item_type": row["item_type"].upper(), "stem": row["stem"], "options": options,
            "correct_keys": [k.upper() for k in re.split(r"[|,;\s]+", row["correct_keys"]) if k],
            "explanation": row["explanation"], "variant_group": row["variant_group"],
        }
        for opt in ("ogg_version", "architecture", "reference"):
            if row.get(opt):
                data[opt] = row[opt].upper() if opt == "architecture" else row[opt]
        out.append((lineno, data))
    return out


def parse_upload(filename: str, raw: bytes) -> list[tuple[int, object]]:
    if len(raw) > MAX_BYTES:
        raise ImportFormatError(f"파일이 너무 큽니다(최대 {MAX_BYTES // 1024 // 1024}MB)")
    if not raw.strip():
        raise ImportFormatError("빈 파일입니다")
    name = (filename or "").lower()
    try:
        if name.endswith(".json"):
            rows = _parse_json(_decode(raw))
        elif name.endswith(".csv"):
            rows = _parse_csv(_decode(raw))
        else:
            raise ImportFormatError("지원하는 형식은 .json, .csv 입니다")
    except csv.Error as e:  # 필드 크기 한도 초과, NUL 문자 등
        raise ImportFormatError(f"CSV 형식 오류: {e}") from None
    if not rows:
        raise ImportFormatError("등록할 문항이 없습니다")
    if len(rows) > MAX_ROWS:
        raise ImportFormatError(f"한 번에 최대 {MAX_ROWS}문항까지 업로드할 수 있습니다")
    return rows


def _messages(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "문항"
        out.append(f"{loc}: {err['msg']}")  # 입력값(input)은 싣지 않는다
    return out


def import_items(db: Session, filename: str, raw: bytes, actor: User) -> ImportResult:
    rows = parse_upload(filename, raw)
    result = ImportResult(total=len(rows))
    valid: list[ItemIn] = []
    seen: dict[tuple[str, str], int] = {}

    for no, obj in rows:
        if not isinstance(obj, dict):
            result.errors.append(RowError(no, ["문항은 객체(JSON object)여야 합니다"]))
            continue
        try:
            data = ItemIn.model_validate(obj)
        except ValidationError as e:
            result.errors.append(RowError(no, _messages(e)))
            continue
        key = (data.domain, norm_stem(data.stem))
        msgs = []
        if key in seen:
            msgs.append(f"파일 내 중복 문항입니다 ({seen[key]}번과 동일한 지문)")
        else:
            seen[key] = no
            dom = domain_by_code(db, data.domain)
            if dup := find_duplicate(db, dom.id, data.stem):
                msgs.append(f"이미 등록된 문항과 지문이 같습니다 (#{dup.id})")
        if msgs:
            result.errors.append(RowError(no, msgs))
            continue
        valid.append(data)

    if result.errors:
        log_audit(db, actor.id, "ITEM_IMPORT_REJECTED", filename[:200],
                  {"total": result.total, "error_rows": [e.row for e in result.errors][:50]})
        db.commit()
        return result

    for data in valid:
        create_item(db, data, actor, check_duplicate=False)
    result.created = len(valid)
    log_audit(db, actor.id, "ITEM_IMPORT", filename[:200], {"created": result.created})
    db.commit()
    return result
