"""초기 데이터 적재 (멱등). 영역/기본 Blueprint 는 항상, 샘플 문항은 요청 시에만."""
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AppSetting, Blueprint, BlueprintRule, Difficulty, Domain, Item, ItemStatus,
)
from app.schemas.item import ItemIn
from app.services.blueprint_validation import validate_blueprint

DOMAINS = [
    ("ARCH", "아키텍처", "GoldenGate 구성요소, Classic/Microservices 아키텍처"),
    ("INSTALL", "설치·환경", "설치, DB 사전 설정, 보충 로깅, 자격증명"),
    ("EXTRACT", "Extract", "Extract/Integrated Extract/Data Pump"),
    ("REPLICAT", "Replicat", "Replicat 유형, 적용 옵션, 충돌 처리"),
    ("MAPPING", "매핑·변환", "MAP/TABLE, COLMAP, 변환 함수, DEFGEN"),
    ("OPS", "운영·모니터링", "상태/지연 모니터링, Manager 운영 파라미터"),
    ("TROUBLE", "트러블슈팅", "오류 분석, 장애 대응"),
]

DEFAULT_BLUEPRINT_NAME = "기본 40문항"
# 영역별 (LOW, MID, HIGH) 문항 수 — 합계 12/20/8 = 40문항 (LOW30%/MID50%/HIGH20%)
DEFAULT_CELLS = {
    "ARCH": (2, 3, 1),
    "INSTALL": (1, 2, 1),
    "EXTRACT": (2, 3, 2),
    "REPLICAT": (2, 4, 1),
    "MAPPING": (2, 3, 1),
    "OPS": (2, 2, 1),
    "TROUBLE": (1, 3, 1),
}
# 합계 12*2.0 + 20*2.5 + 8*3.25 = 100
DEFAULT_POINTS = {Difficulty.LOW: 2.0, Difficulty.MID: 2.5, Difficulty.HIGH: 3.25}
DEFAULT_SETTINGS = {"show_explanations": True}


def seed_domains(db: Session) -> dict[str, Domain]:
    existing = {d.code: d for d in db.scalars(select(Domain))}
    for code, name, desc in DOMAINS:
        if code not in existing:
            d = Domain(code=code, name=name, description=desc)
            db.add(d)
            existing[code] = d
    db.flush()
    return existing


def seed_blueprint(db: Session, domains: dict[str, Domain]) -> Blueprint:
    bp = db.scalar(select(Blueprint).where(Blueprint.name == DEFAULT_BLUEPRINT_NAME))
    if bp:
        return bp
    bp = Blueprint(
        name=DEFAULT_BLUEPRINT_NAME, total_items=40, time_limit_min=60,
        pass_score=70.0, domain_min_ratio=0.4, is_active=True,
    )
    for code, counts in DEFAULT_CELLS.items():
        for diff, n in zip((Difficulty.LOW, Difficulty.MID, Difficulty.HIGH), counts):
            if n:
                bp.rules.append(
                    BlueprintRule(domain_id=domains[code].id, difficulty=diff,
                                  item_count=n, points_per_item=DEFAULT_POINTS[diff])
                )
    validate_blueprint(bp)
    db.add(bp)
    db.flush()
    return bp


def seed_settings(db: Session) -> None:
    for k, v in DEFAULT_SETTINGS.items():
        if db.get(AppSetting, k) is None:
            db.add(AppSetting(key=k, value=v))


def seed_sample_items(db: Session, domains: dict[str, Domain]) -> int:
    """개발용 샘플 문항(APPROVED, is_sample=True). 이미 있으면 variant_group 기준으로 건너뜀."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from seeds.sample_items import build  # noqa: PLC0415

    have = set(db.scalars(select(Item.variant_group).where(Item.is_sample.is_(True))))
    added = 0
    for raw in build():
        data = ItemIn.model_validate(raw)  # 스키마 검증
        if data.variant_group in have:
            continue
        db.add(
            Item(
                domain_id=domains[data.domain].id, difficulty=data.difficulty,
                item_type=data.item_type, stem=data.stem, options=data.options,
                correct_keys=data.correct_keys, explanation=data.explanation,
                reference=data.reference, ogg_version=data.ogg_version,
                architecture=data.architecture, status=ItemStatus.APPROVED,
                variant_group=data.variant_group, is_sample=True,
            )
        )
        added += 1
    return added


def run_seed(db: Session, samples: bool = False) -> dict:
    domains = seed_domains(db)
    bp = seed_blueprint(db, domains)
    seed_settings(db)
    added = seed_sample_items(db, domains) if samples else 0
    db.commit()
    return {"domains": len(domains), "blueprint": bp.name, "sample_items_added": added}
