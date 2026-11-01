from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import IntegrityError

from app.models import (
    AttemptStatus, Blueprint, Difficulty, Domain, ExamAttempt, Item, ItemStatus,
    Product, Role, User, utcnow,
)
from app.schemas.item import ItemIn
from app.seed import DEFAULT_BLUEPRINT_NAME, run_seed
from app.services.blueprint_validation import BlueprintError, validate_blueprint
from app.services.security import hash_password, verify_password

ROOT = Path(__file__).resolve().parent.parent


# ---------- seed ----------
def test_seed_domains_and_blueprint(seeded):
    by_product = {}
    for d in seeded.scalars(select(Domain)):
        by_product.setdefault(d.product.code, set()).add(d.code)
    assert by_product["OGG"] == {"ARCH", "INSTALL", "EXTRACT", "REPLICAT", "MAPPING", "OPS", "TROUBLE"}
    assert len(by_product["ORADB"]) == 8 and all(c.startswith("DB_") for c in by_product["ORADB"])
    assert len(by_product["WLS"]) == 8 and all(c.startswith("WLS_") for c in by_product["WLS"])
    bp = seeded.scalar(select(Blueprint).where(Blueprint.name == DEFAULT_BLUEPRINT_NAME))
    assert bp.product.code == "OGG"  # 기본 Blueprint 는 OGG 만. 다른 제품은 문항이 쌓인 뒤 관리자가 만든다
    assert bp.is_active and bp.total_items == 40 and bp.time_limit_min == 60
    assert bp.pass_score == 70 and bp.domain_min_ratio == 0.4
    validate_blueprint(bp)  # 문항 수 40, 배점 합계 100


def test_default_blueprint_difficulty_ratio(seeded):
    bp = seeded.scalar(select(Blueprint))
    by_diff = Counter()
    for r in bp.rules:
        by_diff[r.difficulty] += r.item_count
    assert by_diff == {Difficulty.LOW: 12, Difficulty.MID: 20, Difficulty.HIGH: 8}


def test_sample_items_cover_every_cell_at_least_3(seeded):
    counts = Counter(
        (i.domain_id, i.difficulty)
        for i in seeded.scalars(select(Item).where(Item.status == ItemStatus.APPROVED))
    )
    assert len(counts) == 21 and min(counts.values()) >= 3


def test_sample_items_cover_default_blueprint_needs(seeded):
    bp = seeded.scalar(select(Blueprint))
    counts = Counter((i.domain_id, i.difficulty) for i in seeded.scalars(select(Item)))
    for r in bp.rules:
        assert counts[(r.domain_id, r.difficulty)] >= r.item_count


def test_sample_items_are_marked_and_unique_variant_groups(seeded):
    items = list(seeded.scalars(select(Item)))
    assert all(i.is_sample for i in items)
    assert len({i.variant_group for i in items}) == len(items)


def test_seed_is_idempotent(seeded):
    before = seeded.query(Item).count()
    out = run_seed(seeded, samples=True)
    assert out["sample_items_added"] == 0 and seeded.query(Item).count() == before
    assert seeded.query(Product).count() == 3
    assert seeded.query(Domain).count() == 23 and seeded.query(Blueprint).count() == 1


def test_seed_without_samples_adds_no_items(db):
    run_seed(db, samples=False)
    assert db.query(Item).count() == 0


# ---------- 샘플 문항 정합성 ----------
def test_all_sample_items_pass_schema(seeded):
    for i in seeded.scalars(select(Item)):
        ItemIn.model_validate(
            {
                "domain": i.domain.code, "difficulty": i.difficulty, "item_type": i.item_type,
                "architecture": i.architecture, "stem": i.stem, "options": i.options,
                "correct_keys": i.correct_keys, "explanation": i.explanation,
                "variant_group": i.variant_group,
            }
        )


def test_correct_key_positions_are_varied(seeded):
    keys = Counter(k for i in seeded.scalars(select(Item)) for k in i.correct_keys)
    assert len(keys) >= 4  # 정답이 특정 위치에 쏠리지 않음


# ---------- Blueprint 검증 ----------
def test_blueprint_validation_rejects_bad_points(seeded):
    bp = seeded.scalar(select(Blueprint))
    bp.rules[0].points_per_item += 1
    with pytest.raises(BlueprintError, match="배점 합계"):
        validate_blueprint(bp)


def test_blueprint_validation_rejects_bad_count(seeded):
    bp = seeded.scalar(select(Blueprint))
    bp.total_items = 41
    with pytest.raises(BlueprintError, match="total_items"):
        validate_blueprint(bp)


# ---------- 문항 스키마 ----------
def _valid(**over):
    base = {
        "domain": "EXTRACT", "difficulty": "MID", "item_type": "SINGLE",
        "stem": "질문", "options": {"A": "a", "B": "b"}, "correct_keys": ["B"],
        "explanation": "해설", "variant_group": "G1",
    }
    base.update(over)
    return base


def test_item_schema_accepts_valid():
    assert ItemIn.model_validate(_valid()).correct_keys == ["B"]


@pytest.mark.parametrize(
    "over",
    [
        {"domain": "NOPE"},
        {"correct_keys": ["C"]},
        {"correct_keys": ["A", "B"]},  # SINGLE 에 복수 정답
        {"item_type": "MULTI", "correct_keys": ["A"]},  # MULTI 에 단일 정답
        {"options": {"A": "same", "B": "same"}},
        {"options": {"A": "a", "Z": "z"}},
        {"stem": "  "},
        {"correct_keys": []},
    ],
)
def test_item_schema_rejects_invalid(over):
    with pytest.raises(ValidationError):
        ItemIn.model_validate(_valid(**over))


# ---------- DB 제약 ----------
def _user(db, login="u1"):
    u = User(login_id=login, name="홍길동", password_hash=hash_password("pw-long-enough"), role=Role.CANDIDATE)
    db.add(u)
    db.flush()
    return u


def _attempt(u, bp, status=AttemptStatus.IN_PROGRESS):
    return ExamAttempt(
        user_id=u.id, blueprint_id=bp.id, seed="s", expires_at=utcnow() + timedelta(minutes=60), status=status
    )


def test_only_one_in_progress_attempt_per_user(seeded):
    bp = seeded.scalar(select(Blueprint))
    u = _user(seeded)
    seeded.add(_attempt(u, bp))
    seeded.flush()
    seeded.add(_attempt(u, bp))
    with pytest.raises(IntegrityError):
        seeded.flush()


def test_submitted_attempts_do_not_block_new_one(seeded):
    bp = seeded.scalar(select(Blueprint))
    u = _user(seeded)
    seeded.add_all([_attempt(u, bp, AttemptStatus.SUBMITTED), _attempt(u, bp, AttemptStatus.SUBMITTED)])
    seeded.add(_attempt(u, bp))
    seeded.flush()  # 예외 없음


def test_login_id_unique(db):
    _user(db, "dup")
    db.add(User(login_id="dup", name="x", password_hash="h"))
    with pytest.raises(IntegrityError):
        db.flush()


def test_item_json_roundtrip(seeded):
    i = seeded.scalar(select(Item).where(Item.variant_group == "ARCH-INTEGRATED-REPLICAT"))
    assert isinstance(i.options, dict) and len(i.correct_keys) == 2


# ---------- 비밀번호 ----------
def test_password_hash_roundtrip():
    h = hash_password("correct horse battery")
    assert h != "correct horse battery" and verify_password("correct horse battery", h)
    assert not verify_password("wrong", h) and not verify_password("x", "not-a-hash")


# ---------- 마이그레이션 ----------
def test_alembic_upgrade_downgrade_matches_models(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    eng = create_engine(url)
    tables = set(inspect(eng).get_table_names())
    eng.dispose()
    assert {"users", "domains", "items", "blueprints", "blueprint_rules", "exam_attempts",
            "attempt_items", "audit_logs", "item_reviews", "app_settings"} <= tables
    command.downgrade(cfg, "base")
    eng = create_engine(url)
    left = set(inspect(eng).get_table_names())
    eng.dispose()
    assert left <= {"alembic_version"}


# ---------- CLI ----------
def test_cli_seed_and_create_admin(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app import cli
    from app.db import Base, make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'c.db'}")
    Base.metadata.create_all(eng)
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))

    assert cli.main(["seed"]) == 0
    pws = iter(["admin-password-1", "admin-password-1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": next(pws))
    assert cli.main(["create-admin", "admin", "관리자"]) == 0
    with cli.SessionLocal() as db:
        u = db.scalar(select(User).where(User.login_id == "admin"))
        assert u.role == Role.ADMIN and verify_password("admin-password-1", u.password_hash)
    assert cli.main(["create-admin", "admin", "관리자"]) == 1  # 중복
    eng.dispose()


def test_engine_creates_missing_sqlite_parent_directory(tmp_path):
    """새 환경(예: 새로 받은 저장소, 빈 볼륨)에서 data/ 폴더가 없어도 첫 실행이 실패하지 않아야 한다."""
    from app.db import make_engine

    target = tmp_path / "nested" / "dir" / "cbt.db"
    assert not target.parent.exists()
    eng = make_engine(f"sqlite:///{target}")
    with eng.connect() as conn:
        conn.exec_driver_sql("select 1")
    eng.dispose()
    assert target.parent.is_dir() and target.exists()


def test_engine_memory_url_does_not_create_directories(tmp_path, monkeypatch):
    from app.db import make_engine

    monkeypatch.chdir(tmp_path)
    make_engine("sqlite://").dispose()
    make_engine("sqlite:///:memory:").dispose()
    assert list(tmp_path.iterdir()) == []


def test_alembic_upgrade_creates_missing_sqlite_directory(tmp_path):
    """README 순서(alembic upgrade head 가 첫 명령)가 새 환경에서도 동작해야 한다."""
    target = tmp_path / "fresh" / "cbt.db"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{target}")
    command.upgrade(cfg, "head")
    assert target.exists()


def _cli_env(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app import cli
    from app.db import Base, make_engine

    eng = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    Base.metadata.create_all(eng)
    monkeypatch.setattr(cli, "SessionLocal", sessionmaker(bind=eng, expire_on_commit=False))
    return cli, eng


def test_cli_reset_password_for_admin_clears_lock_and_audits(tmp_path, monkeypatch):
    from datetime import timedelta

    from app.models import AuditLog

    cli, eng = _cli_env(tmp_path, monkeypatch)
    pws = iter(["first-password-1", "first-password-1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": next(pws))
    assert cli.main(["create-admin", "boss", "대표"]) == 0
    with cli.SessionLocal() as db:
        u = db.scalar(select(User).where(User.login_id == "boss"))
        u.failed_login_count, u.locked_until = 4, utcnow() + timedelta(minutes=9)
        db.commit()

    pws = iter(["brand-new-password", "brand-new-password"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": next(pws))
    assert cli.main(["reset-password", "boss"]) == 0
    with cli.SessionLocal() as db:
        u = db.scalar(select(User).where(User.login_id == "boss"))
        assert verify_password("brand-new-password", u.password_hash) and not verify_password("first-password-1", u.password_hash)
        assert u.failed_login_count == 0 and u.locked_until is None
        logs = {l.action: l for l in db.scalars(select(AuditLog))}
        assert set(logs) == {"CLI_ADMIN_CREATED", "CLI_PASSWORD_RESET"}
        assert "brand-new-password" not in str(logs["CLI_PASSWORD_RESET"].detail)
    eng.dispose()


def test_cli_reset_password_rejects_unknown_user_and_bad_password(tmp_path, monkeypatch, capsys):
    cli, eng = _cli_env(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": "x")
    assert cli.main(["reset-password", "ghost"]) == 1 and "존재하지 않는" in capsys.readouterr().err
    pws = iter(["valid-password-1", "valid-password-1"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": next(pws))
    cli.main(["create-admin", "boss", "대표"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda _="": "short")  # 너무 짧음
    assert cli.main(["reset-password", "boss"]) == 1
    with cli.SessionLocal() as db:
        assert verify_password("valid-password-1", db.scalar(select(User).where(User.login_id == "boss")).password_hash)
    eng.dispose()


def test_sample_item_stems_and_options_are_in_english():
    """요청 사항: 시험 문제의 질문과 선택지는 영어로 표기한다(해설은 한국어)."""
    import re

    from seeds.sample_items import build

    hangul = re.compile(r"[가-힣]")
    for item in build():
        assert not hangul.search(item["stem"]), item["variant_group"]
        assert not any(hangul.search(v) for v in item["options"].values()), item["variant_group"]
