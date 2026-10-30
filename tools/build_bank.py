"""영역별 문항 파일(question_bank/items_*.json)을 합쳐 일괄 업로드용 JSON/CSV 를 만들고, 실제 업로드 기능으로 시험 적재한다.

사용: python tools/build_bank.py
결과:
  question_bank/ogg_items_all.json   — 관리자 '문항 업로드' 에 그대로 올리는 파일
  question_bank/ogg_items_all.csv    — 같은 내용의 CSV(UTF-8 BOM, Excel 호환)
시험 적재는 임시 DB(기존 샘플 64문항 포함)에 업로드 서비스를 그대로 실행해 '오류 0건, 전부 DRAFT' 를 확인한다(실제 DB 는 건드리지 않음).
"""
import csv
import io
import json
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models  # noqa: E402,F401
from app.db import Base, make_engine  # noqa: E402
from app.models import Item, ItemStatus, Role, User  # noqa: E402
from app.schemas.item import OPTION_KEYS  # noqa: E402
from app.seed import run_seed  # noqa: E402
from app.services.item_import import MAX_BYTES, MAX_ROWS, import_items  # noqa: E402

BANK = ROOT / "question_bank"
ORDER = ["ARCH", "INSTALL", "EXTRACT", "REPLICAT", "MAPPING", "OPS", "TROUBLE"]
CSV_COLS = ["domain", "difficulty", "item_type", "stem", *[f"option_{k}" for k in OPTION_KEYS], "correct_keys",
            "explanation", "variant_group", "ogg_version", "architecture", "reference"]


def to_csv(items: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLS)
    for it in items:
        w.writerow([it["domain"], it["difficulty"], it["item_type"], it["stem"],
                    *[it["options"].get(k, "") for k in OPTION_KEYS], "|".join(it["correct_keys"]),
                    it["explanation"], it["variant_group"], it.get("ogg_version", "19c"), it.get("architecture", "BOTH"),
                    it.get("reference") or ""])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8")


def trial_import(name: str, raw: bytes) -> tuple:
    with tempfile.TemporaryDirectory() as d:
        eng = make_engine(f"sqlite:///{d}/trial.db")
        Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng, expire_on_commit=False)()
        run_seed(db, samples=True)  # 실제와 같이 기존 샘플 64문항이 있는 상태
        admin = User(login_id="bank-admin", name="bank", password_hash="!", role=Role.ADMIN)
        db.add(admin)
        db.commit()
        res = import_items(db, name, raw, admin)
        drafts = db.query(Item).filter(Item.status == ItemStatus.DRAFT).count()
        db.close()
        eng.dispose()
        return res, drafts


def main() -> int:
    items = []
    for dom in ORDER:
        p = BANK / f"items_{dom}.json"
        if not p.exists():
            print(f"[없음] {p.name}")
            continue
        part = json.loads(p.read_text(encoding="utf-8"))
        print(f"{p.name:22} {len(part):4}문항")
        items.extend(part)
    raw_json = json.dumps(items, ensure_ascii=False, indent=1).encode("utf-8")
    raw_csv = to_csv(items)
    (BANK / "ogg_items_all.json").write_bytes(raw_json)
    (BANK / "ogg_items_all.csv").write_bytes(raw_csv)
    print(f"합계 {len(items)}문항 | JSON {len(raw_json)/1024:.0f}KB, CSV {len(raw_csv)/1024:.0f}KB "
          f"(업로드 한도 {MAX_BYTES//1024//1024}MB, {MAX_ROWS}문항)")
    print("난이도:", dict(Counter(i["difficulty"] for i in items)), "| 유형:", dict(Counter(i["item_type"] for i in items)))
    print("정답 위치 분포:", dict(sorted(Counter(k for i in items for k in i["correct_keys"]).items())))

    ok = True
    for name, raw in (("ogg_items_all.json", raw_json), ("ogg_items_all.csv", raw_csv)):
        res, drafts = trial_import(name, raw)
        print(f"시험 적재 {name}: 등록 {res.created} / 전체 {res.total}, 오류 {len(res.errors)}건, DRAFT {drafts}건")
        for e in res.errors[:10]:
            print("   ", e.row, e.messages)
        ok &= res.ok and res.created == len(items) == drafts
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
