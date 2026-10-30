"""문항 은행 파일(JSON) 검증 도구 — 일괄 업로드 전에 실행한다.

사용: python tools/validate_bank.py <items.json> [--domain ARCH]
  - 업로드와 같은 스키마 검증(app.schemas.item.ItemIn)
  - 질문·선택지에 한글이 없는지(영어 표기 규칙)
  - 파일 안 / 기존 샘플(question_bank/_existing_samples.json) 과의 지문 중복(같은 영역, 공백·대소문자 무시)
  - variant_group 이름 규칙(영역 접두사, 대문자), 그룹 쏠림, 난이도·유형 분포
오류가 있으면 종료 코드 1.
"""
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import ValidationError  # noqa: E402

from app.schemas.item import ItemIn  # noqa: E402

PREFIX = {"ARCH": "ARCH-", "INSTALL": "INST-", "EXTRACT": "EXT-", "REPLICAT": "REP-", "MAPPING": "MAP-", "OPS": "OPS-", "TROUBLE": "TR-"}
HANGUL = re.compile(r"[가-힣]")
GROUP_RE = re.compile(r"^[A-Z][A-Z0-9-]{2,99}$")
LETTER_REF = re.compile(r"(?:\b(?:option|options|보기|선택지)\s*[A-E]\b)|(?:\([A-E]\))|(?:(?<![A-Za-z0-9$@_.])[A-E](?:\)|는|은|이|가|도|와|과|를|을|의|,\s*[A-E]\b))")


def norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    path = Path(args[0])
    want_domain = args[args.index("--domain") + 1] if "--domain" in args else None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[치명] 파일을 읽을 수 없습니다: {e}")
        return 1
    if isinstance(data, dict):
        data = data.get("items", [])
    existing_path = ROOT / "question_bank" / "_existing_samples.json"
    existing = json.loads(existing_path.read_text(encoding="utf-8")) if existing_path.exists() else []
    existing_stems = {(e["domain"], norm(e["stem"])) for e in existing}

    errors, warns = [], []
    seen: dict = {}
    by_cell = defaultdict(list)
    groups = Counter()
    types = Counter()
    for no, raw in enumerate(data, start=1):
        tag = f"#{no}"
        if not isinstance(raw, dict):
            errors.append(f"{tag}: 객체가 아닙니다")
            continue
        try:
            it = ItemIn.model_validate(raw)
        except ValidationError as e:
            errors.append(f"{tag} ({raw.get('variant_group')}): " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
            continue
        tag = f"#{no} {it.variant_group}"
        if want_domain and it.domain != want_domain:
            errors.append(f"{tag}: 영역이 {want_domain} 가 아닙니다({it.domain})")
        if HANGUL.search(it.stem) or any(HANGUL.search(v) for v in it.options.values()):
            errors.append(f"{tag}: 질문/선택지에 한글이 있습니다(영어로 작성)")
        if not it.explanation.strip() or not HANGUL.search(it.explanation):
            warns.append(f"{tag}: 해설은 한국어로 작성합니다")
        if not GROUP_RE.match(it.variant_group) or not it.variant_group.startswith(PREFIX[it.domain]):
            errors.append(f"{tag}: variant_group 은 대문자이고 '{PREFIX[it.domain]}' 로 시작해야 합니다")
        if len(it.options) < 4:
            errors.append(f"{tag}: 보기는 4~5개로 작성합니다")
        if it.item_type.value == "MULTI" and "choose" not in it.stem.lower():
            errors.append(f"{tag}: MULTI 문항은 지문에 '(Choose all that apply.)' 또는 '(Choose two.)' 를 넣습니다")
        if it.item_type.value == "MULTI" and len(it.correct_keys) >= len(it.options):
            errors.append(f"{tag}: MULTI 문항에 오답 보기가 없습니다")
        key = (it.domain, norm(it.stem))
        if key in seen:
            errors.append(f"{tag}: 파일 안에서 지문 중복(#{seen[key]})")
        seen[key] = no
        if key in existing_stems:
            errors.append(f"{tag}: 기존 샘플 문항과 지문이 같습니다")
        if len(set(norm(v) for v in it.options.values())) != len(it.options):
            errors.append(f"{tag}: 보기 중복(대소문자/공백 무시)")
        # 응시 화면은 보기 순서를 섞어 1~5 번호로 보여 주므로 해설에서 보기 문자(A~E)를 가리키면 맞지 않는다
        if LETTER_REF.search(it.explanation):
            errors.append(f"{tag}: 해설에서 보기를 문자(A~E)로 가리키지 말고 보기 내용으로 언급하세요")
        if any(re.search(r"\b(all|none) of the above\b", v, re.I) for v in it.options.values()):
            errors.append(f"{tag}: 'all/none of the above' 보기는 순서가 섞이면 의미가 없습니다")
        by_cell[(it.domain, it.difficulty.value)].append(it.variant_group)
        groups[it.variant_group] += 1
        types[it.item_type.value] += 1

    for g, n in groups.items():
        if n > 3:
            warns.append(f"variant_group {g}: {n}문항 — 같은 개념이 너무 많습니다(최대 3 권장)")
    print(f"검사 {len(data)}문항 | 유형 {dict(types)}")
    for cell, gs in sorted(by_cell.items()):
        print(f"  {cell[0]:9} {cell[1]:5} 문항 {len(gs):3}  서로 다른 그룹 {len(set(gs)):3}")
    for w in warns[:40]:
        print("[경고]", w)
    for e in errors[:200]:
        print("[오류]", e)
    print(f"결과: 오류 {len(errors)}건, 경고 {len(warns)}건")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
