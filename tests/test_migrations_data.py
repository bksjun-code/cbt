"""마이그레이션이 '이미 데이터가 있는 DB'에서도 안전한지 확인한다(운영 중 업데이트 시나리오)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

ENUMS = {"role": "CANDIDATE", "difficulty": "MID", "item_type": "SINGLE", "architecture": "BOTH", "status": "APPROVED"}


def cfg_for(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def insert(conn, meta, table, **over):
    """리플렉션으로 필수 컬럼을 채워 한 행을 넣는다(마이그레이션 당시의 스키마 그대로)."""
    t = meta.tables[table]
    row = {}
    for c in t.columns:
        if c.name in over:
            row[c.name] = over[c.name]
        elif c.nullable or c.primary_key and isinstance(c.type, sa.Integer) and c.autoincrement:
            continue
        elif c.name in ENUMS:
            row[c.name] = ENUMS[c.name]
        elif isinstance(c.type, sa.JSON):
            row[c.name] = {} if c.name in ("options", "domain_scores") else []
        elif isinstance(c.type, sa.Boolean):
            row[c.name] = False
        elif isinstance(c.type, sa.DateTime):
            row[c.name] = NOW
        elif isinstance(c.type, sa.Float):
            row[c.name] = 1.0
        elif isinstance(c.type, sa.Integer):
            row[c.name] = 1
        else:
            row[c.name] = "x"
    res = conn.execute(t.insert().values(**row))
    return res.inserted_primary_key[0] if res.inserted_primary_key else None


@pytest.fixture()
def populated_v1(tmp_path):
    """0001 스키마로 만든 DB 에 사용자·문항·시험 기록을 채워 둔다."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(cfg_for(url), "0001")
    eng = sa.create_engine(url)
    meta = sa.MetaData()
    meta.reflect(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        dom = insert(conn, meta, "domains", code="ARCH", name="아키텍처")
        u = insert(conn, meta, "users", login_id="old.user", name="기존", role="CANDIDATE")
        it = insert(conn, meta, "items", domain_id=dom, stem="기존 문항", options={"A": "a", "B": "b"}, correct_keys=["A"],
                    variant_group="OLD-1")
        bp = insert(conn, meta, "blueprints", name="기존 기준표", is_active=True, total_items=1)
        insert(conn, meta, "blueprint_rules", blueprint_id=bp, domain_id=dom, difficulty="MID", item_count=1, points_per_item=100.0)
        a = insert(conn, meta, "exam_attempts", user_id=u, blueprint_id=bp, seed="s", status="SUBMITTED", total_score=88.5,
                   passed=True, expires_at=NOW, submitted_at=NOW)
        conn.execute(meta.tables["attempt_items"].insert().values(attempt_id=a, seq=1, item_id=it, option_order=["B", "A"],
                                                                  points=100.0, selected_keys=["A"], is_correct=True, flagged=False))
        insert(conn, meta, "audit_logs", action="OLD_ACTION", detail={"k": "v"})
        insert(conn, meta, "item_reviews", item_id=it, action="APPROVE")
        insert(conn, meta, "app_settings", key="show_explanations", value=True)
    eng.dispose()
    return url


def test_upgrade_from_v1_with_data_preserves_everything(populated_v1):
    command.upgrade(cfg_for(populated_v1), "head")
    eng = sa.create_engine(populated_v1)
    with eng.connect() as conn:
        q = lambda sql: conn.exec_driver_sql(sql).fetchall()  # noqa: E731
        assert q("select login_id, name from users") == [("old.user", "기존")]
        # 0004: 가입 승인 상태가 추가되어도 기존 계정은 그대로 로그인할 수 있어야 한다(ACTIVE)
        assert q("select status from users") == [("ACTIVE",)]
        assert q("select session_version from users") == [(0,)]  # 0005: 한 계정 한 곳 로그인
        assert q("select count(*) from password_reset_tokens") == [(0,)]
        assert q("select stem, variant_group from items") == [("기존 문항", "OLD-1")]
        assert q("select total_score, passed, status from exam_attempts") == [(88.5, 1, "SUBMITTED")]
        assert q("select seq, points, is_correct from attempt_items") == [(1, 100.0, 1)]
        assert q("select action from audit_logs") == [("OLD_ACTION",)]
        # 새 컬럼/테이블: 기존 행은 NULL, 신규 테이블은 비어 있음
        assert q("select earned_points from attempt_items") == [(None,)]
        assert q("select count(*) from item_overrides") == [(0,)]
        # 0007: 제품 추가. 기존 영역·Blueprint 는 OGG 에 연결, 문항 버전 컬럼 이름 변경(값 유지)
        assert q("select code from products order by sort_order") == [("OGG",), ("ORADB",), ("WLS",)]
        assert q("select p.code from domains d join products p on p.id = d.product_id") == [("OGG",)]
        assert q("select p.code from blueprints b join products p on p.id = b.product_id") == [("OGG",)]
        assert q("select product_version from items") == [("x",)]
        version = q("select version_num from alembic_version")
    from alembic.script import ScriptDirectory

    head = ScriptDirectory.from_config(cfg_for(populated_v1)).get_current_head()
    assert version == [(head,)]  # 새 마이그레이션이 추가돼도 이 테스트를 고칠 필요가 없게 최신 리비전과 비교
    eng.dispose()


def test_orm_can_read_migrated_rows_and_regrade_them(populated_v1):
    """마이그레이션된 DB 를 현재 코드가 그대로 읽고, 채점 보정(재채점)까지 동작해야 한다."""
    command.upgrade(cfg_for(populated_v1), "head")
    from sqlalchemy.orm import sessionmaker

    from app.models import ExamAttempt, Item, OverrideMode, Role, User
    from app.services.grade_overrides import set_override
    from app.services.security import hash_password

    eng = sa.create_engine(populated_v1)
    db = sessionmaker(bind=eng, expire_on_commit=False)()
    try:
        item = db.scalar(sa.select(Item))
        attempt = db.scalar(sa.select(ExamAttempt))
        assert attempt.status.value == "SUBMITTED" and attempt.items[0].item_id == item.id
        assert attempt.created_at if hasattr(attempt, "created_at") else True
        admin = User(login_id="adm", name="a", password_hash=hash_password("x" * 12), role=Role.ADMIN)
        db.add(admin)
        db.flush()
        res = set_override(db, item, OverrideMode.EXCLUDE, "마이그레이션 후 보정 테스트", admin)
        db.commit()
        assert res.regraded == 1 and attempt.items[0].is_correct is None
    finally:
        db.close()
        eng.dispose()


def test_single_active_attempt_index_survives_migration(populated_v1):
    command.upgrade(cfg_for(populated_v1), "head")
    eng = sa.create_engine(populated_v1)
    meta = sa.MetaData()
    meta.reflect(eng)
    with eng.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        uid = conn.exec_driver_sql("select id from users").scalar()
        bpid = conn.exec_driver_sql("select id from blueprints").scalar()
        insert(conn, meta, "exam_attempts", user_id=uid, blueprint_id=bpid, seed="s1", status="IN_PROGRESS", expires_at=NOW)
    with pytest.raises(IntegrityError):
        with eng.begin() as conn:
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
            insert(conn, meta, "exam_attempts", user_id=uid, blueprint_id=bpid, seed="s2", status="IN_PROGRESS", expires_at=NOW)
    eng.dispose()


def test_downgrade_to_v1_keeps_rows_and_upgrade_again_works(populated_v1):
    cfg = cfg_for(populated_v1)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0001")
    eng = sa.create_engine(populated_v1)
    with eng.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from attempt_items").scalar() == 1
        assert conn.exec_driver_sql("select total_score from exam_attempts").scalar() == 88.5
        cols = [r[1] for r in conn.exec_driver_sql("pragma table_info(attempt_items)").fetchall()]
        assert "earned_points" not in cols
    eng.dispose()
    command.upgrade(cfg, "head")
    eng = sa.create_engine(populated_v1)
    with eng.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from users").scalar() == 1
    eng.dispose()


def test_upgrade_is_idempotent_when_already_at_head(populated_v1):
    cfg = cfg_for(populated_v1)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # 서비스를 재시작할 때마다 실행되므로 반복해도 안전해야 한다
    eng = sa.create_engine(populated_v1)
    with eng.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from users").scalar() == 1
    eng.dispose()
